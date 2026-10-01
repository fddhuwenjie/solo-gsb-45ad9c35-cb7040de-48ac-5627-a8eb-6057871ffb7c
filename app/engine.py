"""计算引擎：收集候选声明 -> 层叠 -> 自定义属性继承与 var() 解析 -> 生成证据。

compute() 返回旧版 ComputeResponse（基线计算）；_solve() 返回带完整内部
轨迹的 _World，供样式变更预演（preview.py）做世界对比与因果归因。
"""
from __future__ import annotations

from dataclasses import dataclass

from .cascade import Candidate, cascade
from .models import (
    ComputeRequest,
    ComputeResponse,
    CustomPropTraceOut,
    ElementInfo,
    InvalidDeclOut,
    PropertyEvidenceOut,
)
from .selector import parse_selector, selector_matches, selector_specificity
from .variables import VarEvent, Unresolvable, resolve_custom_properties, substitute_vars


def _resolve_layer_order(
    declared: list[str],
    rules,
    overrides: dict[tuple[int, int], str | None] | None = None,
) -> dict[str, int]:
    """层声明顺序；规则中引用但未声明的层按首次出现顺序追加到末尾。

    overrides 为声明级层覆盖（样式变更预演用）：(规则下标, 声明下标) -> 层名；
    值为 None 表示把该声明改为未分层（不进入层序表）。
    """
    order = list(declared)
    for rule in rules:
        if rule.layer is not None and rule.layer not in order:
            order.append(rule.layer)
    if overrides:
        for layer in overrides.values():
            if layer is not None and layer not in order:
                order.append(layer)
    return {name: i for i, name in enumerate(order)}


def _element_label(el: ElementInfo) -> str:
    label = el.tag or "*"
    if el.id:
        label += f"#{el.id}"
    for cls in el.classes:
        label += f".{cls}"
    return label


def invalid_reason(exc: Unresolvable) -> str:
    if exc.status == "missing":
        return f"var() 引用的自定义属性 {exc.name} 未定义，且声明没有 fallback"
    if exc.status == "syntax":
        return "var() 语法错误，且声明没有可用 fallback"
    return (
        f"var() 引用的自定义属性 {exc.name} 已失效（循环依赖或自身解析失败），"
        "且声明没有 fallback"
    )


@dataclass
class _DepthInfo:
    """祖先链上一个元素（depth 0 起）的自定义属性解析记录。"""

    depth: int
    label: str
    element: ElementInfo
    ancestors: list[ElementInfo]
    winners: dict  # {prop: (Candidate, [EliminatedOut])}
    resolved: dict  # {prop: (status, value)}，解析后（含继承）
    notes: dict
    cyclic: set
    events: dict  # {本元素属性名: [VarEvent]}
    deps: dict  # {本元素属性名: [引用名]}


@dataclass
class _NormalInfo:
    """目标元素上一个普通属性的层叠与 var() 解析记录。"""

    winner: Candidate
    eliminated: list
    status: str  # computed | invalid
    value: str | None
    reason: str | None
    var_steps: list
    var_events: list[VarEvent]


@dataclass
class _World:
    rules: list
    layer_index: dict[str, int]
    layer_overrides: dict  # (rule_index, decl_index) -> layer 名或 None
    chain: list[ElementInfo]
    depths: list[_DepthInfo]
    element_custom: dict  # 目标元素最终 {prop: (status, value)}
    normals: dict[str, _NormalInfo]
    computed: dict
    custom_properties: dict
    invalid: list[InvalidDeclOut]
    evidence: list[PropertyEvidenceOut]
    custom_trace: list[CustomPropTraceOut]
    custom_winners: dict  # 目标元素本元素胜出的自定义属性 {prop: Candidate}


def _parse_rules(rules):
    """预解析所有规则的选择器并计算特异性 / 源码次序。

    同一份 rules 被多元素 / 多次试删复算复用时，解析结果可复用
    （声明删除只影响候选收集，不改变选择器与规则次序）。
    """
    parsed = []
    for idx, rule in enumerate(rules):
        compounds, combinators = parse_selector(rule.selector)
        spec = selector_specificity(compounds)
        order = rule.order if rule.order is not None else idx
        parsed.append((rule, order, compounds, combinators, spec))
    return parsed


def _solve(
    element: ElementInfo,
    ancestors: list[ElementInfo],
    layers: list[str],
    rules: list,
    layer_overrides: dict[tuple[int, int], str | None] | None = None,
    disabled: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None,
    parsed: list | None = None,
) -> _World:
    layer_overrides = layer_overrides or {}
    layer_index = _resolve_layer_order(layers, rules, layer_overrides)

    # 预解析所有规则的选择器（可由调用方传入以在整树复算间复用）
    if parsed is None:
        parsed = _parse_rules(rules)

    def candidates_for(el: ElementInfo, el_ancestors: list[ElementInfo], custom: bool):
        result: list[Candidate] = []
        for ri, (rule, order, compounds, combinators, spec) in enumerate(parsed):
            if not selector_matches(compounds, combinators, el, el_ancestors):
                continue
            for di, decl in enumerate(rule.declarations):
                # 样式表清理审计：已纳入删除批次的声明不参与层叠
                if disabled is not None and (ri, di) in disabled:
                    continue
                is_custom = decl.property.startswith("--")
                if is_custom != custom:
                    continue
                eff_layer = layer_overrides.get((ri, di), rule.layer)
                result.append(
                    Candidate(
                        selector=rule.selector,
                        origin=rule.origin,
                        layer=eff_layer,
                        layer_index=layer_index.get(eff_layer) if eff_layer is not None else None,
                        important=decl.important,
                        specificity=spec,
                        source_order=order,
                        rule_index=ri,
                        decl_index=di,
                        property=decl.property if is_custom else decl.property.lower(),
                        value=decl.value,
                    )
                )
        return result

    # ---- 自定义属性：沿祖先链逐元素层叠、继承并解析 var() ----
    chain = list(ancestors) + [element]
    inherited: dict[str, tuple[str, str | None]] = {}
    depths: list[_DepthInfo] = []
    custom_trace: list[CustomPropTraceOut] = []
    for depth, el in enumerate(chain):
        el_ancestors = chain[:depth]
        winners = cascade(candidates_for(el, el_ancestors, custom=True))
        own = {prop: winner.value for prop, (winner, _) in winners.items()}
        resolved, notes, info = resolve_custom_properties(own, inherited, trace=True)
        label = _element_label(el)
        for prop in sorted(resolved):
            if prop in own:
                winner, eliminated = winners[prop]
                status, value = resolved[prop]
                custom_trace.append(
                    CustomPropTraceOut(
                        element=label,
                        property=prop,
                        status="ok" if status == "ok" else "invalid",
                        declared_value=winner.value,
                        resolved=value if status == "ok" else None,
                        note=notes.get(prop, "本元素层叠胜出"),
                        winner=winner.to_out(),
                        eliminated=eliminated,
                    )
                )
            else:
                status, value = resolved[prop]
                custom_trace.append(
                    CustomPropTraceOut(
                        element=label,
                        property=prop,
                        status="inherited",
                        resolved=value if status == "ok" else None,
                        note="继承自祖先" if status == "ok" else "继承自祖先（已失效）",
                    )
                )
        depths.append(
            _DepthInfo(
                depth=depth,
                label=label,
                element=el,
                ancestors=list(el_ancestors),
                winners=winners,
                resolved=resolved,
                notes=notes,
                cyclic=info["cyclic"],
                events=info["events"],
                deps=info["deps"],
            )
        )
        inherited = resolved

    element_custom = inherited
    custom_winners = depths[-1].winners if depths else {}

    def lookup(name: str):
        return element_custom.get(name, ("missing", None))

    # ---- 普通属性：仅在目标元素上层叠，再做 var() 替换 ----
    winners = cascade(candidates_for(element, ancestors, custom=False))
    normals: dict[str, _NormalInfo] = {}
    computed: dict[str, str | None] = {}
    invalid: list[InvalidDeclOut] = []
    evidence: list[PropertyEvidenceOut] = []
    for prop in sorted(winners):
        winner, eliminated = winners[prop]
        steps: list[str] = []
        events: list[VarEvent] = []
        try:
            value = (
                substitute_vars(winner.value, lookup, steps, events)
                if "var(" in winner.value
                else winner.value
            )
            info = _NormalInfo(
                winner=winner,
                eliminated=eliminated,
                status="computed",
                value=value,
                reason=None,
                var_steps=steps,
                var_events=events,
            )
            computed[prop] = value
            evidence.append(
                PropertyEvidenceOut(
                    property=prop,
                    status="computed",
                    computed_value=value,
                    winner=winner.to_out(),
                    var_steps=steps,
                    eliminated=eliminated,
                )
            )
        except Unresolvable as exc:
            reason = invalid_reason(exc)
            info = _NormalInfo(
                winner=winner,
                eliminated=eliminated,
                status="invalid",
                value=None,
                reason=reason,
                var_steps=steps,
                var_events=events,
            )
            computed[prop] = None
            invalid.append(InvalidDeclOut(property=prop, value=winner.value, reason=reason))
            evidence.append(
                PropertyEvidenceOut(
                    property=prop,
                    status="invalid",
                    computed_value=None,
                    winner=winner.to_out(),
                    var_steps=steps,
                    eliminated=eliminated,
                )
            )
        normals[prop] = info

    return _World(
        rules=rules,
        layer_index=layer_index,
        layer_overrides=layer_overrides,
        chain=chain,
        depths=depths,
        element_custom=element_custom,
        normals=normals,
        computed=computed,
        custom_properties={
            name: value for name, (status, value) in element_custom.items() if status == "ok"
        },
        invalid=invalid,
        evidence=evidence,
        custom_trace=custom_trace,
        custom_winners=custom_winners,
    )


def compute(req: ComputeRequest) -> ComputeResponse:
    world = _solve(req.element, req.ancestors, req.layers, req.rules)
    return ComputeResponse(
        computed=world.computed,
        custom_properties=world.custom_properties,
        invalid=world.invalid,
        evidence=world.evidence,
        custom_property_trace=world.custom_trace,
    )
