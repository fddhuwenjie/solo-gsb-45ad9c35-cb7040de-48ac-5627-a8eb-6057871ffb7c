"""不可变的整树分析内核。

一次解析规则（RuleSet），按先序为每个节点求解完整层叠状态（NodeAnalysis）：
普通属性与自定义属性的层叠胜出者与全部候选定位、computed /
custom_properties / invalid（含失效原因）、自定义属性沿祖先链的继承与
var() 依赖记录（解析事件、依赖图、循环集合）。

/compute、/stylesheet-audit、/dom-impact 与 /compute 预演（patches）都构建
在同一内核之上：

- RuleSet：一次解析的规则集（选择器、特异性、源码次序、层声明顺序），
  审计的逐条试删与预演的基线 / 补丁后 / 留一法复算共享同一份解析结果；
- SolveOptions：一次求解的变体参数（声明删除集合、声明级层覆盖），
  不改变已解析的规则骨架；
- solve_chain / solve_forest：对一条元素链 / 一片先序链森林求解，
  产出不可变的 NodeAnalysis / TreeAnalysis；
- NodeAnalysis.signature()：三处前后对比共享的节点状态签名——每个属性
  （普通与自定义）都记录「胜出声明定位 + 状态 + 取值 + 失效原因」。
  因此“胜出者改变但取值相同”、失效原因变化都计为变化，而完全未变的
  节点不会进入任何一方的结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .cascade import Candidate, cascade
from .models import (
    CustomPropTraceOut,
    Declaration,
    ElementInfo,
    InvalidDeclOut,
    PropertyEvidenceOut,
    Rule,
)
from .selector import Compound, parse_selector, selector_matches, selector_specificity
from .variables import Unresolvable, VarEvent, resolve_custom_properties, substitute_vars

# 自定义属性经继承传到后代时已失效的通用原因（本元素无更具体的解析记录）
INHERITED_INVALID_REASON = "继承自祖先的自定义属性已失效（循环依赖或 var() 解析失败）"


# ---------- 规则集：一次解析 ----------


@dataclass(frozen=True)
class ParsedRule:
    """一次解析后不再变化的规则骨架（补丁只改声明，不改这些字段）。"""

    index: int  # 规则在请求 rules 数组中的下标
    selector: str
    origin: str
    layer: str | None
    order: int  # 源码次序（缺省取数组下标）
    specificity: tuple[int, int, int]
    compounds: tuple[Compound, ...]
    combinators: tuple[str, ...]

    def matches(self, element: ElementInfo, ancestors: list[ElementInfo]) -> bool:
        return selector_matches(self.compounds, self.combinators, element, ancestors)


@dataclass(frozen=True)
class RuleSet:
    """不可变规则集：解析一次，供整树求解与所有复算变体共享。"""

    parsed: tuple[ParsedRule, ...]
    declarations: tuple[tuple[Declaration, ...], ...]  # 每条规则的声明表
    layers: tuple[str, ...]  # 层声明顺序：早 -> 晚

    @classmethod
    def build(cls, rules: list[Rule], layers: list[str]) -> "RuleSet":
        """解析全部规则的选择器并计算特异性 / 源码次序（非法选择器在此抛出）。"""
        parsed = []
        declarations = []
        for idx, rule in enumerate(rules):
            compounds, combinators = parse_selector(rule.selector)
            parsed.append(
                ParsedRule(
                    index=idx,
                    selector=rule.selector,
                    origin=rule.origin,
                    layer=rule.layer,
                    order=rule.order if rule.order is not None else idx,
                    specificity=selector_specificity(compounds),
                    compounds=tuple(compounds),
                    combinators=tuple(combinators),
                )
            )
            declarations.append(tuple(rule.declarations))
        return cls(
            parsed=tuple(parsed),
            declarations=tuple(declarations),
            layers=tuple(layers),
        )

    def with_declarations(
        self, declarations: tuple[tuple[Declaration, ...], ...]
    ) -> "RuleSet":
        """替换声明表、复用已解析骨架。

        样式变更预演用：补丁只改声明的 value / important（层走 SolveOptions
        覆盖），选择器、来源、规则级层与源码次序不变，骨架可安全共享。
        """
        declarations = tuple(tuple(decls) for decls in declarations)
        if len(declarations) != len(self.parsed):
            raise ValueError("声明表的规则数与规则集不一致")
        return RuleSet(
            parsed=self.parsed, declarations=declarations, layers=self.layers
        )

    def layer_index(
        self, overrides: Mapping[tuple[int, int], str | None] | None = None
    ) -> dict[str, int]:
        """层声明顺序；规则中引用但未声明的层按首次出现顺序追加到末尾。

        overrides 为声明级层覆盖（样式变更预演用）：(规则下标, 声明下标) ->
        层名；值为 None 表示把该声明改为未分层（不进入层序表）。
        """
        order = list(self.layers)
        for rule in self.parsed:
            if rule.layer is not None and rule.layer not in order:
                order.append(rule.layer)
        if overrides:
            for layer in overrides.values():
                if layer is not None and layer not in order:
                    order.append(layer)
        return {name: i for i, name in enumerate(order)}


@dataclass(frozen=True)
class SolveOptions:
    """一次求解的变体参数（字段在构造后被冻结为不可变）。

    - disabled：样式表清理审计已纳入删除批次的 (rule_index, decl_index)；
    - layer_overrides：样式变更预演的声明级层覆盖，值为 None 表示未分层。
    """

    disabled: frozenset[tuple[int, int]] = frozenset()
    layer_overrides: Mapping[tuple[int, int], str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "disabled", frozenset(self.disabled))
        object.__setattr__(
            self, "layer_overrides", MappingProxyType(dict(self.layer_overrides))
        )


# ---------- 节点分析结果（不可变） ----------


@dataclass(frozen=True)
class DepthAnalysis:
    """祖先链上一层元素的自定义属性解析记录。"""

    depth: int
    label: str
    element: ElementInfo
    winners: Mapping[str, tuple[Candidate, tuple]]  # {prop: (胜出, 淘汰证据)}
    resolved: Mapping[str, tuple[str, str | None]]  # {prop: (状态, 解析值)}，含继承
    notes: Mapping[str, str]  # 失效原因（循环 / var() 解析失败）
    cyclic: frozenset[str]  # 处于循环依赖中的变量名
    events: Mapping[str, tuple[VarEvent, ...]]  # 本元素属性名 -> var() 解析事件
    deps: Mapping[str, tuple[str, ...]]  # 本元素属性名 -> 引用的变量名（含 fallback）


@dataclass(frozen=True)
class PropSignature:
    """单个属性的共享状态签名：胜出定位 + 状态 + 取值 + 失效原因。

    胜出定位为 (rule_index, declaration_index)；自定义属性纯继承（本元素
    无胜出声明）时为 None。签名包含胜出定位，因此“胜出者改变但取值相同”
    也算变化。
    """

    winner: tuple[int, int] | None
    status: str
    value: str | None
    reason: str | None


@dataclass(frozen=True)
class NodeSignature:
    """一个节点的完整状态签名（普通属性与自定义属性各自按属性名排序）。"""

    normal: tuple[tuple[str, PropSignature], ...]
    custom: tuple[tuple[str, PropSignature], ...]


@dataclass(frozen=True)
class NormalPropAnalysis:
    """目标元素一个普通属性的层叠与 var() 解析记录。"""

    property: str
    winner: Candidate
    eliminated: tuple  # tuple[EliminatedOut, ...]
    status: str  # computed | invalid
    value: str | None
    reason: str | None  # 失效原因（computed 时为 None）
    var_steps: tuple[str, ...]
    var_events: tuple[VarEvent, ...]

    @property
    def winner_loc(self) -> tuple[int, int]:
        return (self.winner.rule_index, self.winner.decl_index)

    @property
    def candidate_locs(self) -> frozenset[tuple[int, int]]:
        """命中该元素的全部候选声明定位（胜出 + 落选）。"""
        locs = {self.winner_loc}
        locs.update(
            (e.candidate.rule_index, e.candidate.declaration_index)
            for e in self.eliminated
        )
        return frozenset(locs)

    def signature(self) -> PropSignature:
        return PropSignature(self.winner_loc, self.status, self.value, self.reason)


@dataclass(frozen=True)
class CustomPropState:
    """目标元素一个自定义属性的最终状态（含纯继承得到的）。"""

    name: str
    status: str  # ok | invalid
    value: str | None
    reason: str | None  # 失效原因（ok 时为 None）
    winner: Candidate | None  # 本元素胜出声明；纯继承为 None

    @property
    def winner_loc(self) -> tuple[int, int] | None:
        if self.winner is None:
            return None
        return (self.winner.rule_index, self.winner.decl_index)

    def signature(self) -> PropSignature:
        return PropSignature(self.winner_loc, self.status, self.value, self.reason)


def _element_label(el: ElementInfo) -> str:
    label = el.tag or "*"
    if el.id:
        label += f"#{el.id}"
    for cls in el.classes:
        label += f".{cls}"
    return label


def _invalid_reason(exc: Unresolvable) -> str:
    if exc.status == "missing":
        return f"var() 引用的自定义属性 {exc.name} 未定义，且声明没有 fallback"
    if exc.status == "syntax":
        return "var() 语法错误，且声明没有可用 fallback"
    return (
        f"var() 引用的自定义属性 {exc.name} 已失效（循环依赖或自身解析失败），"
        "且声明没有 fallback"
    )


@dataclass(frozen=True)
class NodeAnalysis:
    """单节点（目标元素 + 祖先链）的完整不可变分析结果。"""

    ruleset: RuleSet
    options: SolveOptions
    layer_index: Mapping[str, int]
    chain: tuple[ElementInfo, ...]  # 根 -> ... -> 目标元素
    depths: tuple[DepthAnalysis, ...]  # 沿链逐元素的自定义属性解析
    normals: Mapping[str, NormalPropAnalysis]  # 目标元素普通属性
    customs: Mapping[str, CustomPropState]  # 目标元素自定义属性（含纯继承）

    # ---- 定位与变体信息 ----

    def declaration(self, rule_index: int, decl_index: int) -> Declaration:
        return self.ruleset.declarations[rule_index][decl_index]

    def effective_layer(self, rule_index: int, decl_index: int) -> str | None:
        """声明的实际层：变体覆盖优先，否则取规则级层。"""
        return self.options.layer_overrides.get(
            (rule_index, decl_index), self.ruleset.parsed[rule_index].layer
        )

    def custom_candidate_locs(self) -> dict[str, frozenset[tuple[int, int]]]:
        """自定义属性：name -> 在目标元素本元素上参与层叠的候选声明定位。

        只含本元素命中的候选（不含纯继承），用以判断选择器命中是否变化。
        """
        out: dict[str, frozenset[tuple[int, int]]] = {}
        if not self.depths:
            return out
        for name, (winner, eliminated) in self.depths[-1].winners.items():
            locs = {(winner.rule_index, winner.decl_index)}
            locs.update(
                (e.candidate.rule_index, e.candidate.declaration_index)
                for e in eliminated
            )
            out[name] = frozenset(locs)
        return out

    # ---- 共享签名 ----

    def signature(self) -> NodeSignature:
        """节点状态签名：三处前后对比（审计试删、DOM 变更、补丁预演）共用。"""
        return NodeSignature(
            normal=tuple(
                (prop, info.signature()) for prop, info in sorted(self.normals.items())
            ),
            custom=tuple(
                (name, state.signature())
                for name, state in sorted(self.customs.items())
            ),
        )

    # ---- 响应视图（/compute 渲染用） ----

    def computed(self) -> dict[str, str | None]:
        return {prop: info.value for prop, info in sorted(self.normals.items())}

    def custom_properties(self) -> dict[str, str]:
        return {
            name: state.value
            for name, state in self.customs.items()
            if state.status == "ok"
        }

    def invalid(self) -> list[InvalidDeclOut]:
        return [
            InvalidDeclOut(property=prop, value=info.winner.value, reason=info.reason)
            for prop, info in sorted(self.normals.items())
            if info.status == "invalid"
        ]

    def evidence(self) -> list[PropertyEvidenceOut]:
        return [
            PropertyEvidenceOut(
                property=prop,
                status=info.status,
                computed_value=info.value if info.status == "computed" else None,
                winner=info.winner.to_out(),
                var_steps=list(info.var_steps),
                eliminated=list(info.eliminated),
            )
            for prop, info in sorted(self.normals.items())
        ]

    def custom_trace(self) -> list[CustomPropTraceOut]:
        """自定义属性沿祖先链逐元素的解析轨迹（先序深度、层内按属性名排序）。"""
        trace: list[CustomPropTraceOut] = []
        for d in self.depths:
            for prop in sorted(d.resolved):
                status, value = d.resolved[prop]
                if prop in d.winners:
                    winner, eliminated = d.winners[prop]
                    trace.append(
                        CustomPropTraceOut(
                            element=d.label,
                            property=prop,
                            status="ok" if status == "ok" else "invalid",
                            declared_value=winner.value,
                            resolved=value if status == "ok" else None,
                            note=d.notes.get(prop, "本元素层叠胜出"),
                            winner=winner.to_out(),
                            eliminated=list(eliminated),
                        )
                    )
                else:
                    trace.append(
                        CustomPropTraceOut(
                            element=d.label,
                            property=prop,
                            status="inherited",
                            resolved=value if status == "ok" else None,
                            note="继承自祖先" if status == "ok" else "继承自祖先（已失效）",
                        )
                    )
        return trace


@dataclass(frozen=True)
class TreeAnalysis:
    """整棵树（先序）的不可变分析结果；nodes[i] 对应先序第 i 条链。"""

    ruleset: RuleSet
    options: SolveOptions
    nodes: tuple[NodeAnalysis, ...]

    def signature(self) -> tuple[NodeSignature, ...]:
        """整树签名：审计试删与 DOM 变更前后对比的判定依据。"""
        return tuple(node.signature() for node in self.nodes)


# ---------- 求解 ----------


def _solve_node(
    ruleset: RuleSet,
    options: SolveOptions,
    layer_index: Mapping[str, int],
    element: ElementInfo,
    ancestors: list[ElementInfo],
) -> NodeAnalysis:
    def candidates_for(el: ElementInfo, el_ancestors: list[ElementInfo], custom: bool):
        result: list[Candidate] = []
        for pr in ruleset.parsed:
            if not pr.matches(el, el_ancestors):
                continue
            for di, decl in enumerate(ruleset.declarations[pr.index]):
                # 样式表清理审计：已纳入删除批次的声明不参与层叠
                if (pr.index, di) in options.disabled:
                    continue
                is_custom = decl.property.startswith("--")
                if is_custom != custom:
                    continue
                eff_layer = options.layer_overrides.get((pr.index, di), pr.layer)
                result.append(
                    Candidate(
                        selector=pr.selector,
                        origin=pr.origin,
                        layer=eff_layer,
                        layer_index=(
                            layer_index.get(eff_layer) if eff_layer is not None else None
                        ),
                        important=decl.important,
                        specificity=pr.specificity,
                        source_order=pr.order,
                        rule_index=pr.index,
                        decl_index=di,
                        property=decl.property if is_custom else decl.property.lower(),
                        value=decl.value,
                    )
                )
        return result

    # ---- 自定义属性：沿祖先链逐元素层叠、继承并解析 var() ----
    chain = tuple(ancestors) + (element,)
    inherited: dict[str, tuple[str, str | None]] = {}
    depths: list[DepthAnalysis] = []
    for depth, el in enumerate(chain):
        el_ancestors = list(chain[:depth])
        winners = cascade(candidates_for(el, el_ancestors, custom=True))
        own = {prop: winner.value for prop, (winner, _) in winners.items()}
        resolved, notes, info = resolve_custom_properties(own, inherited, trace=True)
        depths.append(
            DepthAnalysis(
                depth=depth,
                label=_element_label(el),
                element=el,
                winners=MappingProxyType(
                    {prop: (winner, tuple(eliminated))
                     for prop, (winner, eliminated) in winners.items()}
                ),
                resolved=MappingProxyType(resolved),
                notes=MappingProxyType(notes),
                cyclic=frozenset(info["cyclic"]),
                events=MappingProxyType(
                    {name: tuple(evs) for name, evs in info["events"].items()}
                ),
                deps=MappingProxyType(
                    {name: tuple(names) for name, names in info["deps"].items()}
                ),
            )
        )
        inherited = resolved

    # ---- 目标元素最终自定义属性状态（含纯继承） ----
    final = depths[-1].resolved
    target_winners = depths[-1].winners
    target_notes = depths[-1].notes
    customs: dict[str, CustomPropState] = {}
    for name, (status, value) in final.items():
        if status == "invalid":
            reason = target_notes.get(name) or INHERITED_INVALID_REASON
        else:
            reason = None
        pair = target_winners.get(name)
        customs[name] = CustomPropState(
            name=name,
            status=status,
            value=value,
            reason=reason,
            winner=pair[0] if pair else None,
        )

    def lookup(name: str):
        state = customs.get(name)
        return (state.status, state.value) if state else ("missing", None)

    # ---- 普通属性：仅在目标元素上层叠，再做 var() 替换 ----
    winners = cascade(candidates_for(element, list(chain[:-1]), custom=False))
    normals: dict[str, NormalPropAnalysis] = {}
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
            normals[prop] = NormalPropAnalysis(
                property=prop,
                winner=winner,
                eliminated=tuple(eliminated),
                status="computed",
                value=value,
                reason=None,
                var_steps=tuple(steps),
                var_events=tuple(events),
            )
        except Unresolvable as exc:
            normals[prop] = NormalPropAnalysis(
                property=prop,
                winner=winner,
                eliminated=tuple(eliminated),
                status="invalid",
                value=None,
                reason=_invalid_reason(exc),
                var_steps=tuple(steps),
                var_events=tuple(events),
            )

    return NodeAnalysis(
        ruleset=ruleset,
        options=options,
        layer_index=MappingProxyType(dict(layer_index)),
        chain=chain,
        depths=tuple(depths),
        normals=MappingProxyType(normals),
        customs=MappingProxyType(customs),
    )


def solve_chain(
    ruleset: RuleSet,
    element: ElementInfo,
    ancestors: list[ElementInfo],
    options: SolveOptions | None = None,
) -> NodeAnalysis:
    """求解一条元素链（/compute 与补丁预演的目标元素视角）。"""
    options = options or SolveOptions()
    return _solve_node(
        ruleset, options, ruleset.layer_index(options.layer_overrides), element, ancestors
    )


def solve_forest(
    ruleset: RuleSet,
    chains,
    options: SolveOptions | None = None,
) -> TreeAnalysis:
    """按先序求解一片元素链森林（审计 / DOM 变更的整树视角）。

    chains 为 (目标元素, 祖先链) 序列，顺序即先序；层序表在同一变体下
    只解析一次，全部节点共享。
    """
    options = options or SolveOptions()
    layer_index = ruleset.layer_index(options.layer_overrides)
    nodes = tuple(
        _solve_node(ruleset, options, layer_index, element, list(ancestors))
        for element, ancestors in chains
    )
    return TreeAnalysis(ruleset=ruleset, options=options, nodes=nodes)
