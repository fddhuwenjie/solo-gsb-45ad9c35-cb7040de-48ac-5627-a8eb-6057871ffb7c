"""样式表清理审计：在一棵 DOM 样本树上找出可同时删除而不改变任何节点
computed、custom_properties 或 invalid 状态的声明批次。

判定方式：

1. 先序展平样本树，交给不可变整树内核 kernel.analyze_tree 逐节点求解
   基线——与 /compute、/dom-impact、补丁预演共享同一份规则解析与同一套
   节点状态签名；
2. 按 rule_index、declaration_index 顺序逐条把声明加入删除候选，重新
   求解整棵树并逐节点比对 **kernel.NodeAnalysis.signature()** 的四元组
   签名 (胜出声明定位, 状态, 取值, 失效原因)——普通属性与自定义属性
   （含纯继承）用同一个签名函数；
3. 全树签名一致才真正纳入删除批次（贪心累积，后续试删在已删批次之上
   进行），否则保留并记录按先序遍历找到的首个冲突节点、冲突属性与原因。

因此以下情况都不会被纳入批次：删除后出现新的失效（包括自定义属性循环 /
fallback 依赖被破坏）、原本 invalid 被解除（含失效原因变化）、层叠胜出
者换人（即便取值恰好相同——胜出定位是签名分量）、或经继承 / var() 链
传导的任何取值变化。
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine import parse_rules
from .kernel import RuleSet, analyze_tree
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


@dataclass(frozen=True)
class FlatNode:
    """先序展平后的一个节点及其祖先链（ElementInfo，根 -> 父）。"""

    raw_id: object
    element: ElementInfo
    ancestors: tuple[ElementInfo, ...]


def flatten_tree(root: AuditElement) -> list[FlatNode]:
    """先序遍历样本树；校验 id 全树唯一（id 类型在 Pydantic 层已约束）。"""
    flat: list[FlatNode] = []
    seen: set[object] = set()

    def walk(node: AuditElement, ancestors: list[ElementInfo]) -> None:
        if node.id in seen:
            raise AuditValidationError(f"DOM 样本树存在重复 id：{node.id!r}")
        seen.add(node.id)
        info = _to_element_info(node)
        flat.append(FlatNode(raw_id=node.id, element=info, ancestors=tuple(ancestors)))
        child_ancestors = ancestors + [info]
        for child in node.children:
            walk(child, child_ancestors)

    walk(root, [])
    return flat


def validate_rules(rules) -> None:
    """结构化规则的补充校验：声明属性名不能为空。

    选择器语法由内核解析规则时抛出 SelectorSyntaxError（422），层重复
    声明等与 /compute 保持同一宽容度，不额外拒绝。
    """
    for ri, rule in enumerate(rules):
        for di, decl in enumerate(rule.declarations):
            if not decl.property or not decl.property.strip():
                raise AuditValidationError(
                    f"rules[{ri}].declarations[{di}] 的 property 不能为空"
                )


# ---------- 整树求解（统一走内核） ----------


def solve_tree(flat: list[FlatNode], layers, rules, disabled, parsed):
    """对展平树先序求解，返回内核 TreeAnalysis。

    disabled 为已纳入删除批次的 (rule_index, declaration_index) 集合；
    parsed 为只解析一次、所有复算共享的选择器解析结果。三个比对入口
    （审计 / DOM 变更 / 补丁预演）的差异判定都基于 TreeAnalysis 内
    NodeAnalysis 的同一套签名。
    """
    ruleset = RuleSet.parse(
        rules, layers, disabled=frozenset(disabled), parsed=parsed
    )
    return analyze_tree(
        [(node.raw_id, node.element, node.ancestors) for node in flat], ruleset
    )


# ---------- 冲突诊断 ----------


def _first_difference(baseline_sigs, trial_sigs):
    """返回 (先序下标, 属性名, 基线签名项, 试删签名项)。

    签名项统一为 (胜出定位, 状态, 取值, 失效原因)；普通属性与自定义属性
    合并按属性名排序（自定义属性以 -- 开头，与普通属性不会同名）。
    """
    for index, (b_sig, t_sig) in enumerate(zip(baseline_sigs, trial_sigs)):
        for name in sorted(set(b_sig) | set(t_sig)):
            b = b_sig.get(name)
            t = t_sig.get(name)
            if b != t:
                return index, name, b, t
    return None


def _describe_normal(prop, base, trial) -> tuple[str, str]:
    """普通属性差异 -> (reason, 中文说明)。签名项为 (胜出定位, 状态, 取值, 原因)。"""
    if base is None:
        loc, status, value, _reason = trial
        return (
            "winner_changed",
            f"删除后 {prop} 新增了层叠胜出声明（规则 {loc[0]} 第 {loc[1]} 条），"
            f"计算值为 {value!r}；删除前该属性不存在胜出声明",
        )
    if trial is None:
        loc, status, value, _reason = base
        return (
            "winner_changed",
            f"{prop} 的胜出声明（规则 {loc[0]} 第 {loc[1]} 条，原值 {value!r}）"
            "被删除后该属性不再有任何声明胜出",
        )
    (b_loc, b_status, b_value, _br), (t_loc, t_status, t_value, _tr) = base, trial
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
    """自定义属性差异 -> (reason, 中文说明)。签名项为 (本元素胜出定位或
    None, 状态, 解析值, 失效原因)；base/trial 为 None 表示该侧无此属性。"""
    if base is None:
        loc, status, value, _reason = trial
        where = f"规则 {loc[0]} 第 {loc[1]} 条" if loc else "继承"
        return (
            "winner_changed",
            f"删除后自定义属性 {prop} 新增（来源：{where}，状态 {status}，"
            f"值 {value!r}）；删除前该节点链上不存在此属性",
        )
    if trial is None:
        loc, status, value, _reason = base
        where = f"规则 {loc[0]} 第 {loc[1]} 条" if loc else "继承自祖先"
        return (
            "winner_changed",
            f"自定义属性 {prop}（原来源：{where}，值 {value!r}）"
            "在删除后于该节点链上彻底消失（missing）",
        )
    (b_loc, b_status, b_value, _br), (t_loc, t_status, t_value, _tr) = base, trial
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
    parsed = parse_rules(req.rules)

    disabled: set[tuple[int, int]] = set()
    baseline_tree = solve_tree(flat, req.layers, req.rules, disabled, parsed)
    baseline_sigs = baseline_tree.tree_signature()

    deleted: list[DeletedDeclarationOut] = []
    retained: list[RetainedDeclarationOut] = []

    for ri, rule in enumerate(req.rules):
        for di, decl in enumerate(rule.declarations):
            trial_disabled = disabled | {(ri, di)}
            trial_tree = solve_tree(
                flat, req.layers, req.rules, frozenset(trial_disabled), parsed
            )
            trial_sigs = trial_tree.tree_signature()
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
            index, prop, base, trial = diff
            if prop.startswith("--"):
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
