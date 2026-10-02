"""样式表清理审计：在一棵 DOM 样本树上找出可同时删除而不改变任何节点
computed、custom_properties 或 invalid 状态的声明批次。

判定方式：

1. 先序展平样本树，逐节点以「根 -> ... -> 父」祖先链交给整树分析内核
   （一次解析规则，层叠、层顺序、自定义属性继承、循环失效、var()
   fallback 与 /compute 完全同款）求解基线；
2. 按 rule_index、declaration_index 顺序逐条把声明加入删除候选，**重新计算
   整棵树**并逐节点比对内核共享签名——每个普通 / 自定义属性的
   （胜出声明定位, 状态, 取值, 失效原因）；
3. 全树签名一致才真正纳入删除批次（贪心累积，后续试删在已删批次之上进行），
   否则保留并记录按先序遍历找到的首个冲突节点、冲突属性与阻断原因。

因此以下情况都不会被纳入批次：删除后出现新的失效（包括自定义属性循环 /
fallback 依赖被破坏）、原本 invalid 被解除、层叠胜出者换人（即便取值恰好
相同）、失效原因变化，或经继承 / var() 链传导的任何取值变化。
"""
from __future__ import annotations

from dataclasses import dataclass

from .analysis import PropSignature, RuleSet, SolveOptions, solve_forest
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

    选择器语法由内核解析时抛出 SelectorSyntaxError（422），层重复声明等
    与 /compute 保持同一宽容度，不额外拒绝。
    """
    for ri, rule in enumerate(rules):
        for di, decl in enumerate(rule.declarations):
            if not decl.property or not decl.property.strip():
                raise AuditValidationError(
                    f"rules[{ri}].declarations[{di}] 的 property 不能为空"
                )


def _chains(flat: list[FlatNode]):
    """展平树 -> 内核求解所需的 (目标元素, 祖先链) 先序序列。"""
    return [(node.element, node.ancestors) for node in flat]


# ---------- 冲突诊断 ----------


def _first_difference(baseline_sigs, trial_sigs):
    """返回 (先序下标, 属性名, 是否自定义属性, 基线签名, 试删签名)。

    属性遍历顺序：普通属性与自定义属性合并按属性名排序
    （自定义属性以 -- 开头，与普通属性不会同名）。
    """
    for index, (b_sig, t_sig) in enumerate(zip(baseline_sigs, trial_sigs)):
        b_norm, t_norm = dict(b_sig.normal), dict(t_sig.normal)
        b_cust, t_cust = dict(b_sig.custom), dict(t_sig.custom)
        names = sorted(set(b_norm) | set(t_norm) | set(b_cust) | set(t_cust))
        for name in names:
            is_custom = name.startswith("--")
            b = (b_cust if is_custom else b_norm).get(name)
            t = (t_cust if is_custom else t_norm).get(name)
            if b != t:
                return index, name, is_custom, b, t
    return None


def _describe_normal(
    prop: str, base: PropSignature | None, trial: PropSignature | None
) -> tuple[str, str]:
    """普通属性差异 -> (reason, 中文说明)。签名为 (胜出定位, 状态, 取值, 原因)。"""
    if base is None:
        return (
            "winner_changed",
            f"删除后 {prop} 新增了层叠胜出声明（规则 {trial.winner[0]} 第 "
            f"{trial.winner[1]} 条），计算值为 {trial.value!r}；"
            "删除前该属性不存在胜出声明",
        )
    if trial is None:
        return (
            "winner_changed",
            f"{prop} 的胜出声明（规则 {base.winner[0]} 第 {base.winner[1]} 条，"
            f"原值 {base.value!r}）被删除后该属性不再有任何声明胜出",
        )
    if base.winner != trial.winner:
        return (
            "winner_changed",
            f"{prop} 的层叠胜出声明由规则 {base.winner[0]} 第 {base.winner[1]} 条"
            f"（{base.value!r}）改由规则 {trial.winner[0]} 第 {trial.winner[1]} 条"
            f"（{trial.value!r}）胜出",
        )
    if base.status != trial.status:
        if trial.status == "invalid":
            return (
                "new_invalid",
                f"{prop} 的胜出声明不变，但删除后该属性由 computed（{base.value!r}）"
                "变为 invalid（var() 依赖缺失 / 失效且无可用 fallback）",
            )
        return (
            "invalid_cleared",
            f"{prop} 的胜出声明不变，但删除后原本 invalid 的失效被解除，"
            f"新计算值为 {trial.value!r}",
        )
    if base.value != trial.value:
        return (
            "value_changed",
            f"{prop} 的胜出声明不变，但计算值由 {base.value!r} 变为 {trial.value!r}"
            "（var() / 继承依赖变化）",
        )
    return (
        "reason_changed",
        f"{prop} 的胜出声明、状态与取值不变，但失效原因由 {base.reason!r} "
        f"变为 {trial.reason!r}",
    )


def _describe_custom(
    prop: str, base: PropSignature | None, trial: PropSignature | None
) -> tuple[str, str]:
    """自定义属性差异 -> (reason, 中文说明)。签名为 (本元素胜出定位或 None,
    状态, 解析值, 失效原因)；base/trial 为 None 表示该侧无此属性。"""
    if base is None:
        where = (
            f"规则 {trial.winner[0]} 第 {trial.winner[1]} 条"
            if trial.winner
            else "继承"
        )
        return (
            "winner_changed",
            f"删除后自定义属性 {prop} 新增（来源：{where}，状态 {trial.status}，"
            f"值 {trial.value!r}）；删除前该节点链上不存在此属性",
        )
    if trial is None:
        where = (
            f"规则 {base.winner[0]} 第 {base.winner[1]} 条"
            if base.winner
            else "继承自祖先"
        )
        return (
            "winner_changed",
            f"自定义属性 {prop}（原来源：{where}，值 {base.value!r}）"
            "在删除后于该节点链上彻底消失（missing）",
        )
    if base.winner != trial.winner:
        b_where = (
            f"规则 {base.winner[0]} 第 {base.winner[1]} 条"
            if base.winner
            else "纯继承（本元素无胜出声明）"
        )
        t_where = (
            f"规则 {trial.winner[0]} 第 {trial.winner[1]} 条"
            if trial.winner
            else "纯继承（本元素无胜出声明）"
        )
        if trial.winner is None:
            return (
                "winner_changed",
                f"删除后本元素对 {prop} 的胜出声明消失，由本元素声明"
                f"（{b_where}，值 {base.value!r}）退化为继承 / 缺失",
            )
        if base.winner is None:
            return (
                "winner_changed",
                f"删除后 {prop} 在本元素的取值来源由继承改由 {t_where} 胜出"
                f"（值 {trial.value!r}）",
            )
        return (
            "winner_changed",
            f"{prop} 在本元素的层叠胜出声明由 {b_where} 改由 {t_where} 胜出"
            f"（值 {base.value!r} → {trial.value!r}）",
        )
    if base.status != trial.status:
        if trial.status == "invalid":
            return (
                "new_invalid",
                f"{prop} 的本元素胜出声明不变，但删除后由 ok（{base.value!r}）"
                "变为 invalid（循环依赖或 var() 解析失败且无 fallback）",
            )
        if base.status == "invalid":
            return (
                "invalid_cleared",
                f"{prop} 的本元素胜出声明不变，但删除后原本 invalid 的失效"
                f"被解除，新值为 {trial.value!r}",
            )
        return (
            "status_changed",
            f"{prop} 状态由 {base.status} 变为 {trial.status}",
        )
    if base.value != trial.value:
        return (
            "value_changed",
            f"{prop} 的胜出声明与状态不变，但解析值由 {base.value!r} 变为 "
            f"{trial.value!r}（自定义属性继承 / var() 依赖链变化）",
        )
    return (
        "reason_changed",
        f"{prop} 的胜出声明、状态与解析值不变，但失效原因由 {base.reason!r} "
        f"变为 {trial.reason!r}",
    )


# ---------- 入口 ----------


def audit_stylesheet(req: StylesheetAuditRequest) -> StylesheetAuditResponse:
    flat = flatten_tree(req.root)
    validate_rules(req.rules)

    # 规则只解析一次：删除声明不改变选择器，非法选择器在此即抛
    # SelectorSyntaxError（由端点层转 422）；全部试删复算共享同一规则集
    ruleset = RuleSet.build(req.rules, req.layers)
    chains = _chains(flat)

    baseline = solve_forest(ruleset, chains)
    baseline_sigs = baseline.signature()

    disabled: set[tuple[int, int]] = set()
    deleted: list[DeletedDeclarationOut] = []
    retained: list[RetainedDeclarationOut] = []

    for ri, rule in enumerate(req.rules):
        for di, decl in enumerate(rule.declarations):
            trial_disabled = disabled | {(ri, di)}
            trial = solve_forest(
                ruleset, chains, SolveOptions(disabled=frozenset(trial_disabled))
            )
            if trial.signature() == baseline_sigs:
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

            diff = _first_difference(baseline_sigs, trial.signature())
            assert diff is not None
            index, prop, is_custom, base, trial_sig = diff
            if is_custom:
                reason, detail = _describe_custom(prop, base, trial_sig)
            else:
                reason, detail = _describe_normal(prop, base, trial_sig)
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
