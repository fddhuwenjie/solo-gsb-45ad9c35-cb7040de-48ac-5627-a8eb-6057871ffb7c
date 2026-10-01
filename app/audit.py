"""样式表清理审计：在一棵 DOM 样本树上找出可同时删除而不改变任何节点
computed、custom_properties 或 invalid 状态的声明批次。

判定方式：

1. 先序展平样本树，逐节点以「根 -> ... -> 父」祖先链复用 /compute 同款
   引擎（层叠、层顺序、自定义属性继承、循环失效、var() fallback）求解基线；
2. 按 rule_index、declaration_index 顺序逐条把声明加入删除候选，**重新计算
   整棵树**并逐节点比对签名——签名包含每个普通属性的 (胜出声明定位,
   computed/invalid 状态, 取值) 与每个自定义属性的 (本元素胜出声明定位,
   ok/invalid/missing 状态, 解析值，含纯继承)；
3. 全树签名一致才真正纳入删除批次（贪心累积，后续试删在已删批次之上进行），
   否则保留并记录按先序遍历找到的首个冲突节点、冲突属性与阻断原因。

因此以下情况都不会被纳入批次：删除后出现新的失效（包括自定义属性循环 /
fallback 依赖被破坏）、原本 invalid 被解除、层叠胜出者换人（即便取值恰好
相同）、或经继承 / var() 链传导的任何取值变化。
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine import _solve, _parse_rules
from .models import (
    AuditElement,
    DeletedDeclarationOut,
    ElementInfo,
    RetainedDeclarationOut,
    StylesheetAuditRequest,
    StylesheetAuditResponse,
)


class AuditValidationError(ValueError):
    """样本树 / 规则结构非法（整次请求 422）。"""


# ---------- 校验与展平 ----------


def _to_element_info(node: AuditElement) -> ElementInfo:
    return ElementInfo(
        tag=node.tag,
        id=str(node.id),
        classes=list(node.classes),
        attributes=dict(node.attributes),
    )


@dataclass
class FlatNode:
    """先序展平后的一个节点及其祖先链（ElementInfo，根 -> 父）。"""

    raw_id: object
    element: ElementInfo
    ancestors: list[ElementInfo]


def flatten_tree(root: AuditElement) -> list[FlatNode]:
    """先序遍历样本树；校验 id 全树唯一（id 类型在 Pydantic 层已约束）。"""
    flat: list[FlatNode] = []
    seen: set[object] = set()

    def walk(node: AuditElement, ancestors: list[ElementInfo]) -> None:
        if node.id in seen:
            raise AuditValidationError(f"DOM 样本树存在重复 id：{node.id!r}")
        seen.add(node.id)
        info = _to_element_info(node)
        flat.append(FlatNode(raw_id=node.id, element=info, ancestors=list(ancestors)))
        child_ancestors = ancestors + [info]
        for child in node.children:
            walk(child, child_ancestors)

    walk(root, [])
    return flat


def validate_rules(rules) -> None:
    """结构化规则的补充校验：声明属性名不能为空。

    选择器语法由引擎解析时抛出 SelectorSyntaxError（422），层重复声明等
    与 /compute 保持同一宽容度，不额外拒绝。
    """
    for ri, rule in enumerate(rules):
        for di, decl in enumerate(rule.declarations):
            if not decl.property or not decl.property.strip():
                raise AuditValidationError(
                    f"rules[{ri}].declarations[{di}] 的 property 不能为空"
                )


# ---------- 整树求解与签名 ----------


def solve_tree(flat: list[FlatNode], layers, rules, disabled, parsed):
    """对展平树逐节点求解，返回 {先序下标: _World}。

    disabled 为已纳入删除批次的 (rule_index, declaration_index) 集合；
    parsed 为只解析一次、所有复算共享的选择器解析结果。
    """
    worlds = []
    for node in flat:
        worlds.append(
            _solve(
                node.element,
                node.ancestors,
                layers,
                rules,
                disabled=disabled,
                parsed=parsed,
            )
        )
    return worlds


def normal_signature(world) -> dict[str, tuple]:
    """目标元素普通属性签名：属性 -> (胜出定位, 状态, 取值)。"""
    return {
        prop: ((info.winner.rule_index, info.winner.decl_index), info.status, info.value)
        for prop, info in world.normals.items()
    }


def custom_signature(world) -> dict[str, tuple]:
    """目标元素自定义属性签名（含纯继承得到的）：

    name -> (本元素胜出定位或 None, 状态 ok/invalid/missing, 解析值)。
    """
    sig = {}
    for name, (status, value) in world.element_custom.items():
        winner = world.custom_winners.get(name)
        loc = (winner[0].rule_index, winner[0].decl_index) if winner else None
        sig[name] = (loc, status, value)
    return sig


def tree_signature(worlds):
    """整棵树的结果签名：[(普通签名, 自定义签名), ...]（按先序）。"""
    return [(normal_signature(w), custom_signature(w)) for w in worlds]


# ---------- 冲突诊断 ----------


def _first_difference(baseline_sigs, trial_sigs):
    """返回 (先序下标, 属性名, 是否自定义属性, 基线项, 试删项)。

    属性遍历顺序：普通属性与自定义属性合并按属性名排序
    （自定义属性以 -- 开头，与普通属性不会同名）。
    """
    for index, ((b_norm, b_cust), (t_norm, t_cust)) in enumerate(
        zip(baseline_sigs, trial_sigs)
    ):
        names = sorted(set(b_norm) | set(t_norm) | set(b_cust) | set(t_cust))
        for name in names:
            is_custom = name.startswith("--")
            b = (b_cust if is_custom else b_norm).get(name)
            t = (t_cust if is_custom else t_norm).get(name)
            if b != t:
                return index, name, is_custom, b, t
    return None


def _describe_normal(prop, base, trial) -> tuple[str, str]:
    """普通属性差异 -> (reason, 中文说明)。签名项为 (胜出定位, 状态, 取值)。"""
    if base is None:
        loc, status, value = trial
        return (
            "winner_changed",
            f"删除后 {prop} 新增了层叠胜出声明（规则 {loc[0]} 第 {loc[1]} 条），"
            f"计算值为 {value!r}；删除前该属性不存在胜出声明",
        )
    if trial is None:
        loc, status, value = base
        return (
            "winner_changed",
            f"{prop} 的胜出声明（规则 {loc[0]} 第 {loc[1]} 条，原值 {value!r}）"
            "被删除后该属性不再有任何声明胜出",
        )
    (b_loc, b_status, b_value), (t_loc, t_status, t_value) = base, trial
    if b_loc != t_loc:
        if t_loc is None:
            return (
                "winner_changed",
                f"删除后 {prop} 不再有层叠胜出声明（原胜出者为规则 "
                f"{b_loc[0]} 第 {b_loc[1]} 条）",
            )
        return (
            "winner_changed",
            f"{prop} 的层叠胜出声明由规则 {b_loc[0]} 第 {b_loc[1]} 条"
            f"（{b_value!r}）改由规则 {t_loc[0]} 第 {t_loc[1]} 条"
            f"（{t_value!r}）胜出",
        )
    if b_status != t_status:
        if t_status == "invalid":
            return (
                "new_invalid",
                f"{prop} 的胜出声明不变，但删除后该属性由 computed（{b_value!r}）"
                "变为 invalid（var() 依赖缺失 / 失效且无可用 fallback）",
            )
        return (
            "invalid_cleared",
            f"{prop} 的胜出声明不变，但删除后原本 invalid 的失效被解除，"
            f"新计算值为 {t_value!r}",
        )
    return (
        "value_changed",
        f"{prop} 的胜出声明不变，但计算值由 {b_value!r} 变为 {t_value!r}"
        "（var() / 继承依赖变化）",
    )


def _describe_custom(prop, base, trial) -> tuple[str, str]:
    """自定义属性差异 -> (reason, 中文说明)。签名项为 (本元素胜出定位或 None,
    状态, 解析值)；base/trial 为 None 表示该侧无此属性。"""
    if base is None:
        loc, status, value = trial
        where = f"规则 {loc[0]} 第 {loc[1]} 条" if loc else "继承"
        return (
            "winner_changed",
            f"删除后自定义属性 {prop} 新增（来源：{where}，状态 {status}，"
            f"值 {value!r}）；删除前该节点链上不存在此属性",
        )
    if trial is None:
        loc, status, value = base
        where = f"规则 {loc[0]} 第 {loc[1]} 条" if loc else "继承自祖先"
        return (
            "winner_changed",
            f"自定义属性 {prop}（原来源：{where}，值 {value!r}）"
            "在删除后于该节点链上彻底消失（missing）",
        )
    (b_loc, b_status, b_value), (t_loc, t_status, t_value) = base, trial
    if b_loc != t_loc:
        b_where = (
            f"规则 {b_loc[0]} 第 {b_loc[1]} 条" if b_loc else "纯继承（本元素无胜出声明）"
        )
        t_where = (
            f"规则 {t_loc[0]} 第 {t_loc[1]} 条" if t_loc else "纯继承（本元素无胜出声明）"
        )
        if t_loc is None:
            return (
                "winner_changed",
                f"删除后本元素对 {prop} 的胜出声明消失，由本元素声明"
                f"（{b_where}，值 {b_value!r}）退化为继承 / 缺失",
            )
        if b_loc is None:
            return (
                "winner_changed",
                f"删除后 {prop} 在本元素的取值来源由继承改由 {t_where} 胜出"
                f"（值 {t_value!r}）",
            )
        return (
            "winner_changed",
            f"{prop} 在本元素的层叠胜出声明由 {b_where} 改由 {t_where} 胜出"
            f"（值 {b_value!r} → {t_value!r}）",
        )
    if b_status != t_status:
        if t_status == "invalid":
            return (
                "new_invalid",
                f"{prop} 的本元素胜出声明不变，但删除后由 ok（{b_value!r}）"
                "变为 invalid（循环依赖或 var() 解析失败且无 fallback）",
            )
        if b_status == "invalid":
            return (
                "invalid_cleared",
                f"{prop} 的本元素胜出声明不变，但删除后原本 invalid 的失效"
                f"被解除，新值为 {t_value!r}",
            )
        return (
            "status_changed",
            f"{prop} 状态由 {b_status} 变为 {t_status}",
        )
    return (
        "value_changed",
        f"{prop} 的胜出声明与状态不变，但解析值由 {b_value!r} 变为 {t_value!r}"
        "（自定义属性继承 / var() 依赖链变化）",
    )


# ---------- 入口 ----------


def audit_stylesheet(req: StylesheetAuditRequest) -> StylesheetAuditResponse:
    flat = flatten_tree(req.root)
    validate_rules(req.rules)

    # 选择器只解析一次：删除声明不改变选择器，非法选择器在此即抛
    # SelectorSyntaxError（由端点层转 422）
    parsed = _parse_rules(req.rules)

    disabled: set[tuple[int, int]] = set()
    baseline_worlds = solve_tree(flat, req.layers, req.rules, disabled, parsed)
    baseline_sigs = tree_signature(baseline_worlds)

    deleted: list[DeletedDeclarationOut] = []
    retained: list[RetainedDeclarationOut] = []

    for ri, rule in enumerate(req.rules):
        for di, decl in enumerate(rule.declarations):
            trial_disabled = disabled | {(ri, di)}
            trial_worlds = solve_tree(
                flat, req.layers, req.rules, frozenset(trial_disabled), parsed
            )
            trial_sigs = tree_signature(trial_worlds)
            if trial_sigs == baseline_sigs:
                disabled = trial_disabled
                deleted.append(
                    DeletedDeclarationOut(
                        rule_index=ri,
                        declaration_index=di,
                        property=decl.property,
                        selector=rule.selector,
                    )
                )
                continue

            diff = _first_difference(baseline_sigs, trial_sigs)
            assert diff is not None
            index, prop, is_custom, base, trial = diff
            if is_custom:
                reason, detail = _describe_custom(prop, base, trial)
            else:
                reason, detail = _describe_normal(prop, base, trial)
            retained.append(
                RetainedDeclarationOut(
                    rule_index=ri,
                    declaration_index=di,
                    property=decl.property,
                    selector=rule.selector,
                    conflict_node=flat[index].raw_id,
                    conflict_property=prop,
                    reason=reason,
                    detail=detail,
                )
            )

    return StylesheetAuditResponse(
        nodes_checked=len(flat),
        deleted=deleted,
        retained=retained,
    )
