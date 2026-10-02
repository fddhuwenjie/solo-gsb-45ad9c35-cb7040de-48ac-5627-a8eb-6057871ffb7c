"""不可变的整树 CSS 层叠分析内核。

本模块是 /compute、/stylesheet-audit、/dom-impact 与 /compute 补丁预演
四个入口共享的**唯一**状态来源：规则只解析一次（选择器、特异性、源码
次序），随后按**先序**遍历（祖先链或整棵样本树），为每个节点产出一份
不可变的 :class:`NodeAnalysis`：

- 每个普通属性的层叠胜出者、落选证据、computed/invalid 状态、取值、
  失效原因、var() 替换步骤与事件、全部候选定位；
- 每个自定义属性在祖先链每一层的胜出者、解析状态（ok/invalid）、
  解析值、失效原因、var() 事件与依赖、循环集合；
- 本节点最终自定义属性（含纯继承）及其胜出定位。

三个比对入口（删除审计、DOM 变更前后对比、补丁预演）共享同一套节点
签名 :meth:`NodeAnalysis.signature`：属性 -> (胜出定位, 状态, 取值,
失效原因)。因此「胜出声明换人但值相同」必然被识别为差异，而四个分量
全同的节点绝不会出现在任何差异结果中。

内核不修改入参（Pydantic 模型）；补丁 / 删除 / DOM 变更都由调用方先
深拷贝输入再求解，结果对象本身是 frozen dataclass，只可读取。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .cascade import Candidate, cascade
from .models import (
    CustomPropTraceOut,
    ElementInfo,
    InvalidDeclOut,
    PropertyEvidenceOut,
)
from .selector import parse_selector, selector_matches, selector_specificity
from .variables import (
    VarEvent,
    Unresolvable,
    extract_var_names,
    find_cyclic,
    substitute_vars,
)

Loc = tuple[int, int]
CustomStatus = str  # ok | invalid
NormalStatus = str  # computed | invalid

_MISSING = ("missing", None)


# ---------- 输入：只解析一次的规则 ----------


@dataclass(frozen=True)
class ParsedRule:
    """一条规则的解析结果：选择器复合段 / 组合符 / 特异性 / 源码次序。

    解析只依赖选择器与 order，删除声明、补丁改 value / important / layer
    都不使其失效，因此基线与所有复算共享同一份 ParsedRule 列表。
    """

    rule: object
    order: int
    compounds: tuple
    combinators: tuple
    specificity: tuple[int, int, int]


@dataclass(frozen=True)
class RuleSet:
    """不可变规则表：解析结果 + 层顺序 + 声明级层覆盖 + 禁用声明集合。

    layer_overrides：(规则下标, 声明下标) -> 层名或 None（补丁把声明改为
    未分层）；disabled：不参与层叠的声明定位集合（审计删除批次）。
    """

    rules: tuple
    parsed: tuple[ParsedRule, ...]
    layer_index: dict[str, int]
    layer_overrides: Mapping[tuple[int, int], str | None] = field(default_factory=dict)
    disabled: frozenset[Loc] = frozenset()

    @staticmethod
    def parse(
        rules,
        layers: list[str] | None = None,
        layer_overrides: dict[tuple[int, int], str | None] | None = None,
        disabled: frozenset[Loc] | set[Loc] | None = None,
        parsed: tuple[ParsedRule, ...] | list[ParsedRule] | None = None,
    ) -> "RuleSet":
        layer_overrides = dict(layer_overrides or {})
        if parsed is None:
            parsed_rules = []
            for idx, rule in enumerate(rules):
                compounds, combinators = parse_selector(rule.selector)
                parsed_rules.append(
                    ParsedRule(
                        rule=rule,
                        order=rule.order if rule.order is not None else idx,
                        compounds=compounds,
                        combinators=combinators,
                        specificity=selector_specificity(compounds),
                    )
                )
            parsed = parsed_rules
        return RuleSet(
            rules=tuple(rules),
            parsed=tuple(parsed),
            layer_index=resolve_layer_order(layers or [], rules, layer_overrides),
            layer_overrides=layer_overrides,
            disabled=frozenset(disabled or frozenset()),
        )

    def with_changes(
        self,
        rules=None,
        layer_overrides: dict[tuple[int, int], str | None] | None = None,
        disabled: frozenset[Loc] | set[Loc] | None = None,
    ) -> "RuleSet":
        """复用已解析选择器，构造改了规则值 / 层覆盖 / 禁用集合的新规则表。

        补丁复算传 rules（深拷贝后的规则）与 layer_overrides；审计试删只
        传 disabled；二者层顺序都需按新输入重新解析。
        """
        new_rules = tuple(rules if rules is not None else self.rules)
        overrides = self.layer_overrides if layer_overrides is None else dict(layer_overrides)
        return RuleSet(
            rules=new_rules,
            parsed=self.parsed,
            layer_index=resolve_layer_order(
                [
                    name
                    for name, _i in sorted(self.layer_index.items(), key=lambda kv: kv[1])
                ],
                new_rules,
                overrides,
            ),
            layer_overrides=overrides,
            disabled=self.disabled if disabled is None else frozenset(disabled),
        )

    def candidates(
        self,
        el: ElementInfo,
        ancestors: list[ElementInfo],
        *,
        custom: bool,
    ) -> list[Candidate]:
        """收集在 el（祖先链 ancestors）上命中、参与 custom/普通层叠的候选。"""
        result: list[Candidate] = []
        for ri, pr in enumerate(self.parsed):
            if not selector_matches(pr.compounds, pr.combinators, el, ancestors):
                continue
            # 声明取值始终来自当前规则表（补丁世界里是深拷贝后的新规则）；
            # 选择器 / 特异性 / order 只解析一次，存在 parsed 中跨世界共享。
            rule = self.rules[ri]
            for di, decl in enumerate(rule.declarations):
                if (ri, di) in self.disabled:
                    continue
                is_custom = decl.property.startswith("--")
                if is_custom != custom:
                    continue
                eff_layer = self.layer_overrides.get((ri, di), rule.layer)
                result.append(
                    Candidate(
                        selector=rule.selector,
                        origin=rule.origin,
                        layer=eff_layer,
                        layer_index=(
                            self.layer_index.get(eff_layer)
                            if eff_layer is not None
                            else None
                        ),
                        important=decl.important,
                        specificity=pr.specificity,
                        source_order=pr.order,
                        rule_index=ri,
                        decl_index=di,
                        property=decl.property if is_custom else decl.property.lower(),
                        value=decl.value,
                    )
                )
        return result


def resolve_layer_order(
    declared: list[str],
    rules,
    overrides: dict[tuple[int, int], str | None] | None = None,
) -> dict[str, int]:
    """层声明顺序；规则中引用但未声明的层按首次出现顺序追加到末尾。

    overrides 为声明级层覆盖（补丁预演用），覆盖引入的新层同样追加末尾。
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


def element_label(el: ElementInfo) -> str:
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


# ---------- 单元素分析结果（不可变） ----------


@dataclass(frozen=True)
class CustomEntry:
    """一个自定义属性在本元素解析后的最终条目（含纯继承）。"""

    name: str
    status: CustomStatus  # ok | invalid
    value: str | None
    reason: str | None  # invalid 时的失效原因（继承失效时沿用祖先原因）
    winner: Candidate | None  # 本元素胜出声明；纯继承为 None
    inherited_from: int | None  # 值来源层（depth）；本元素胜出为 None


@dataclass(frozen=True)
class DepthCustomInfo:
    """祖先链上一层（depth 0 起）的自定义属性解析记录。"""

    depth: int
    label: str
    element: ElementInfo
    winners: dict  # {name: (Candidate, [EliminatedOut])}，本元素胜出
    resolved: dict  # {name: CustomEntry}，解析后（含继承）
    notes: dict  # {name: 失效原因}，本元素解析时产生（仅本元素声明）
    cyclic: frozenset  # 本元素处于循环中的属性名
    events: dict  # {本元素属性名: [VarEvent]}
    deps: dict  # {本元素属性名: [引用名（含 fallback 内部）]}


@dataclass(frozen=True)
class NormalEntry:
    """目标元素上一个普通属性的层叠与 var() 解析记录。"""

    property: str
    winner: Candidate
    eliminated: tuple
    status: NormalStatus  # computed | invalid
    value: str | None
    reason: str | None
    var_steps: tuple
    var_events: tuple


@dataclass(frozen=True)
class NodeAnalysis:
    """一个节点（或 /compute 的目标元素链）的完整、不可变层叠分析。"""

    element: ElementInfo
    raw_id: object  # 样本树节点的业务 id；祖先 / /compute 链上为 None
    ancestors: tuple[ElementInfo, ...]  # 根 -> 父
    depth_info: tuple[DepthCustomInfo, ...]  # 根 -> 本元素逐层自定义属性记录
    custom: dict  # {name: CustomEntry}，本元素最终自定义属性（含纯继承）
    normals: dict  # {prop: NormalEntry}
    ruleset: RuleSet

    # ----- 结果视图：与 /compute 响应字段一一对应 -----

    @property
    def computed(self) -> dict[str, str | None]:
        return {prop: info.value for prop, info in self.normals.items()}

    @property
    def custom_properties(self) -> dict[str, str]:
        return {
            name: entry.value
            for name, entry in self.custom.items()
            if entry.status == "ok"
        }

    @property
    def invalid(self) -> list[InvalidDeclOut]:
        return [
            InvalidDeclOut(
                property=info.property, value=info.winner.value, reason=info.reason
            )
            for info in self.normals.values()
            if info.status == "invalid"
        ]

    @property
    def evidence(self) -> list[PropertyEvidenceOut]:
        return [
            PropertyEvidenceOut(
                property=info.property,
                status=info.status,
                computed_value=info.value,
                winner=info.winner.to_out(),
                var_steps=list(info.var_steps),
                eliminated=list(info.eliminated),
            )
            for info in self.normals.values()
        ]

    @property
    def custom_property_trace(self) -> list[CustomPropTraceOut]:
        return render_custom_trace(self.depth_info)

    # ----- 候选定位（dom-impact 的选择器命中归因用） -----

    def normal_candidate_locs(self, prop: str) -> frozenset[Loc]:
        info = self.normals.get(prop)
        if info is None:
            return frozenset()
        locs = {(info.winner.rule_index, info.winner.decl_index)}
        locs.update(
            (e.candidate.rule_index, e.candidate.declaration_index)
            for e in info.eliminated
        )
        return frozenset(locs)

    def custom_candidate_locs(self, name: str) -> frozenset[Loc]:
        winners = self.depth_info[-1].winners if self.depth_info else {}
        pair = winners.get(name)
        if pair is None:
            return frozenset()
        winner, eliminated = pair
        locs = {(winner.rule_index, winner.decl_index)}
        locs.update(
            (e.candidate.rule_index, e.candidate.declaration_index)
            for e in eliminated
        )
        return frozenset(locs)

    def custom_winner_candidate(self, name: str) -> Candidate | None:
        pair = self.depth_info[-1].winners.get(name) if self.depth_info else None
        return pair[0] if pair else None

    # ----- 统一签名：三个比对入口共享的唯一差异口径 -----

    def signature(self) -> dict[str, tuple]:
        """节点状态签名：属性名 -> (胜出定位, 状态, 取值, 失效原因)。

        普通属性无胜出声明时缺项；自定义属性链上不存在时缺项
        （dom-impact 侧再补 absent / missing 哨兵）。胜出定位为
        (rule_index, declaration_index)，纯继承的自定义属性为 None。
        四个分量任一不同即差异：因此「胜出者换人但值相同」一定算变化，
        而四量全同（含失效原因）的节点不会出现在任何差异结果里。
        """
        sig: dict[str, tuple] = {}
        for prop, info in self.normals.items():
            sig[prop] = (
                (info.winner.rule_index, info.winner.decl_index),
                info.status,
                info.value,
                info.reason,
            )
        for name, entry in self.custom.items():
            sig[name] = (
                (entry.winner.rule_index, entry.winner.decl_index)
                if entry.winner is not None
                else None,
                entry.status,
                entry.value,
                entry.reason,
            )
        return sig

    def signature_with_absent(self, *, custom: bool) -> dict[str, tuple]:
        """同 :meth:`signature`，但把该侧缺失的属性补成 absent / missing
        哨兵（胜出定位 None、取值 None、原因 None），供 DOM 变更前后
        逐节点对比直接判等。"""
        sentinel_status = "missing" if custom else "absent"
        names = self.custom.keys() if custom else self.normals.keys()
        sig = self.signature()
        return {
            name: sig.get(name, (None, sentinel_status, None, None))
            for name in names
        }


@dataclass(frozen=True)
class TreeAnalysis:
    """先序整树（或整条祖先链）分析结果。nodes 按先序排列。"""

    nodes: tuple[NodeAnalysis, ...]
    flat: tuple  # (raw_id, ElementInfo, tuple[ElementInfo,...])，与 nodes 对齐
    ruleset: RuleSet

    @property
    def target(self) -> NodeAnalysis:
        return self.nodes[-1]

    def tree_signature(self) -> tuple[dict[str, tuple], ...]:
        return tuple(node.signature() for node in self.nodes)


# ---------- 逐层自定义属性解析 ----------


def _resolve_depth(
    ruleset: RuleSet,
    el: ElementInfo,
    ancestors: list[ElementInfo],
    depth: int,
    inherited: dict[str, CustomEntry],
) -> tuple[DepthCustomInfo, dict[str, CustomEntry]]:
    """在链上第 depth 层层叠本元素自定义属性，继承 inherited 并解析 var()。

    返回 (本层记录, 解析后全量映射，供下一层继承)。失效原因随继承传播：
    后代纯继承一个 invalid 变量时沿用祖先的具体原因（循环 / 引用失败）。
    """
    winners = cascade(ruleset.candidates(el, ancestors, custom=True))
    own = {name: winner.value for name, (winner, _elim) in winners.items()}

    # 1) 循环依赖检测（仅本元素胜出声明参与，与落选声明无关）
    deps = {name: extract_var_names(value) for name, value in own.items()}
    cyclic = frozenset(find_cyclic(deps))

    resolved: dict[str, CustomEntry] = {}
    notes: dict[str, str] = {}
    for name, entry in inherited.items():
        # 不可变地继承：保留祖先的来源层与原因
        resolved[name] = entry

    if cyclic:
        cycle_note = "循环依赖失效：" + " ↔ ".join(sorted(cyclic))
        for name in cyclic:
            resolved[name] = CustomEntry(
                name=name,
                status="invalid",
                value=None,
                reason=cycle_note,
                winner=winners[name][0],
                inherited_from=None,
            )
            notes[name] = cycle_note

    # 2) 其余本元素声明做 var() 替换（此时本元素图已无环）
    events: dict[str, list[VarEvent]] = {}
    memo: dict[str, tuple[str, str | None]] = {}

    def lookup(name: str) -> tuple[str, str | None]:
        if name in resolved:
            entry = resolved[name]
            return entry.status, entry.value
        if name in own:
            return _resolve_own(name)
        return _MISSING

    def _resolve_own(name: str) -> tuple[str, str | None]:
        if name in memo:
            return memo[name]
        evs: list[VarEvent] = []
        try:
            result = ("ok", substitute_vars(own[name], lookup, events=evs))
        except Unresolvable:
            result = ("invalid", None)
        events[name] = evs
        memo[name] = result
        return result

    for name in own:
        if name in cyclic:
            continue
        status, value = _resolve_own(name)
        if status == "invalid":
            reason = "var() 解析失败：引用的自定义属性缺失或已失效，且无 fallback"
            notes[name] = reason
        else:
            reason = None
        resolved[name] = CustomEntry(
            name=name,
            status=status,
            value=value,
            reason=reason,
            winner=winners[name][0],
            inherited_from=None,
        )

    # 3) 给纯继承条目标注来源层（本元素未声明者沿用最近祖先层）
    final: dict[str, CustomEntry] = {}
    for name, entry in resolved.items():
        if name in own:
            final[name] = entry
        else:
            source = inherited.get(name)
            final[name] = CustomEntry(
                name=name,
                status=entry.status,
                value=entry.value,
                reason=entry.reason,
                winner=None,
                inherited_from=source.inherited_from if source and source.inherited_from is not None else depth - 1,
            )

    info = DepthCustomInfo(
        depth=depth,
        label=element_label(el),
        element=el,
        winners=winners,
        resolved=final,
        notes=notes,
        cyclic=cyclic,
        events=events,
        deps=deps,
    )
    return info, final


# ---------- 普通属性 ----------


def _build_normals(
    ruleset: RuleSet,
    el: ElementInfo,
    ancestors: list[ElementInfo],
    custom: dict[str, CustomEntry],
) -> dict[str, NormalEntry]:
    winners = cascade(ruleset.candidates(el, ancestors, custom=False))

    def lookup(name: str):
        entry = custom.get(name)
        if entry is None:
            return _MISSING
        return entry.status, entry.value

    normals: dict[str, NormalEntry] = {}
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
            normals[prop] = NormalEntry(
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
            reason = invalid_reason(exc)
            normals[prop] = NormalEntry(
                property=prop,
                winner=winner,
                eliminated=tuple(eliminated),
                status="invalid",
                value=None,
                reason=reason,
                var_steps=tuple(steps),
                var_events=tuple(events),
            )
    return normals


# ---------- 求解入口：先序链 / 整树 ----------


def analyze_chain(
    element: ElementInfo,
    ancestors: list[ElementInfo],
    ruleset: RuleSet,
    raw_id: object = None,
) -> NodeAnalysis:
    """对「祖先链 + 目标元素」求解一次（/compute 与补丁预演用）。

    普通属性只在目标元素上层叠（CSS 普通属性不继承）；自定义属性沿链
    逐层继承。祖先节点只做自定义属性解析（跳过普通属性），其
    DepthCustomInfo 经 NodeAnalysis.depth_info 暴露给因果轨迹。
    """
    chain = list(ancestors) + [element]
    inherited: dict[str, CustomEntry] = {}
    depth_info: list[DepthCustomInfo] = []
    for depth, el in enumerate(chain):
        el_ancestors = chain[:depth]
        info, inherited = _resolve_depth(ruleset, el, el_ancestors, depth, inherited)
        depth_info.append(info)
    normals = _build_normals(ruleset, element, list(ancestors), inherited)
    return NodeAnalysis(
        element=element,
        raw_id=raw_id,
        ancestors=tuple(ancestors),
        depth_info=tuple(depth_info),
        custom=dict(inherited),
        normals=normals,
        ruleset=ruleset,
    )


def analyze_tree(
    flat: list,
    ruleset: RuleSet,
) -> TreeAnalysis:
    """按先序对样本树每个节点求解（审计 / DOM 变更影响用）。

    flat 为 [(raw_id, ElementInfo, 祖先 ElementInfo 列表), ...]，
    调用方（audit.flatten_tree）已保证先序、祖先齐备。每个节点独立从
    根解析自定义属性链（兄弟子树不共享变量作用域），输出与
    :func:`analyze_chain` 完全同构的 NodeAnalysis。
    """
    nodes: list[NodeAnalysis] = []
    aligned: list = []
    for raw_id, el, ancestors in flat:
        node = analyze_chain(el, list(ancestors), ruleset, raw_id=raw_id)
        nodes.append(node)
        aligned.append((raw_id, el, tuple(ancestors)))
    return TreeAnalysis(nodes=tuple(nodes), flat=tuple(aligned), ruleset=ruleset)


# ---------- 自定义属性轨迹渲染（/compute 兼容输出） ----------


def render_custom_trace(depth_info: tuple[DepthCustomInfo, ...]) -> list[CustomPropTraceOut]:
    """把逐层自定义属性记录渲染为 /compute 的 custom_property_trace。

    每层内按属性名排序；只渲染本元素胜出（ok/invalid）与纯继承条目，
    顺序与旧引擎一致（同层 sorted(property)）。
    """
    trace: list[CustomPropTraceOut] = []
    for dinfo in depth_info:
        for name in sorted(dinfo.resolved):
            entry = dinfo.resolved[name]
            if entry.winner is not None:
                winner, eliminated = dinfo.winners[name]
                trace.append(
                    CustomPropTraceOut(
                        element=dinfo.label,
                        property=name,
                        status="ok" if entry.status == "ok" else "invalid",
                        declared_value=winner.value,
                        resolved=entry.value if entry.status == "ok" else None,
                        note=dinfo.notes.get(name, "本元素层叠胜出"),
                        winner=winner.to_out(),
                        eliminated=list(eliminated),
                    )
                )
            else:
                trace.append(
                    CustomPropTraceOut(
                        element=dinfo.label,
                        property=name,
                        status="inherited",
                        resolved=entry.value if entry.status == "ok" else None,
                        note="继承自祖先" if entry.status == "ok" else "继承自祖先（已失效）",
                    )
                )
    return trace


# ---------- 整树签名比对（三个入口共享） ----------


def diff_signatures(
    before: NodeAnalysis,
    after: NodeAnalysis,
) -> list[tuple[str, tuple | None, tuple | None]]:
    """返回同一先序位置两个节点间所有签名差异。

    每项为 (属性名, 前侧四元组或 None, 后侧四元组或 None)；普通属性用
    absent 哨兵、自定义属性用 missing 哨兵补齐缺失侧。属性按名排序
    （自定义属性以 -- 开头，与普通属性不会同名，合并排序无歧义）。
    """
    sb, sa = before.signature(), after.signature()
    out = []
    for name in sorted(set(sb) | set(sa)):
        is_custom = name.startswith("--")
        sentinel = (None, "missing" if is_custom else "absent", None, None)
        b = sb.get(name)
        a = sa.get(name)
        b_cmp = b if b is not None else sentinel
        a_cmp = a if a is not None else sentinel
        if b_cmp != a_cmp:
            out.append((name, b if b is not None else sentinel, a if a is not None else sentinel))
    return out
