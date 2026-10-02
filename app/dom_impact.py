"""DOM 变更后的样式影响分析。

给定一棵带唯一 id 的 DOM 样本树、层顺序与结构化规则，以及一次对指定节点的
原子变更（添加 / 移除 class、设置 / 删除 attribute、修改 tag）：

1. 先按整树分析内核（与 /compute、/stylesheet-audit、补丁预演同一套层叠、
   自定义属性继承与 var() 规则）对整棵树求解基线；
2. 校验全部通过后再原子应用变更，重新求解**整棵树**；
3. 逐节点比对内核共享签名（每个普通 / 自定义属性的胜出声明定位、状态、
   取值与失效原因）与层叠候选集合，只列出 computed / custom_properties /
   invalid 实际发生变化的节点与属性，并区分四类传播原因：
     - selector_match：变更使规则选择器对该元素的命中发生变化（含祖先链
       被变更后经后代 / 子组合符传导到后代元素的情况）；
     - cascade_winner：该属性的层叠胜出声明换人（含属性消失 / 新增）；
     - inheritance：自定义属性本元素无胜出声明，变化经继承自祖先传导；
     - var_chain：胜出声明相同，取值 / 失效状态经 var() 依赖链变化。

目标不存在、重复 id、空操作或非法规则一律 422；所有校验在任何求解之前
完成，校验失败不产生部分结果。
"""
from __future__ import annotations

from .analysis import NodeAnalysis, PropSignature, RuleSet, solve_forest
from .audit import flatten_tree, validate_rules
from .models import (
    AuditElement,
    CandidateOut,
    DomImpactRequest,
    DomImpactResponse,
    DomMutation,
    MutationOut,
    NodeImpactOut,
    PropertyImpactOut,
    PropertyValueOut,
)

# 原因在响应中的规范次序
_REASON_ORDER = ("selector_match", "cascade_winner", "inheritance", "var_chain")


class DomImpactValidationError(ValueError):
    """树 / 规则 / 变更非法或变更为空操作（整次请求 422，不产生部分结果）。"""


# ---------- 校验 ----------


def _collect_nodes(root: AuditElement) -> dict[object, AuditElement]:
    nodes: dict[object, AuditElement] = {}

    def walk(node: AuditElement) -> None:
        nodes[node.id] = node
        for child in node.children:
            walk(child)

    walk(root)
    return nodes


def _require_field(m: DomMutation, field: str, label: str) -> str:
    if field not in m.model_fields_set or getattr(m, field) is None:
        raise DomImpactValidationError(f"{m.type} 操作必须给出 {label}")
    return getattr(m, field)


def validate_mutation(mutation: DomMutation, nodes: dict[object, AuditElement]) -> MutationOut:
    """校验变更目标存在、操作字段齐全且非空操作。所有检查先于任何求解。"""
    target = nodes.get(mutation.node_id)
    if target is None:
        raise DomImpactValidationError(
            f"变更目标节点 id {mutation.node_id!r} 不存在于 DOM 树中"
        )

    t = mutation.type
    if t == "add_class":
        cls = _require_field(mutation, "class_name", "class_name")
        if not cls.strip():
            raise DomImpactValidationError("class_name 不能为空白字符串")
        if cls in target.classes:
            raise DomImpactValidationError(
                f"空操作：节点 {mutation.node_id!r} 已含有 class {cls!r}"
            )
    elif t == "remove_class":
        cls = _require_field(mutation, "class_name", "class_name")
        if not cls.strip():
            raise DomImpactValidationError("class_name 不能为空白字符串")
        if cls not in target.classes:
            raise DomImpactValidationError(
                f"空操作：节点 {mutation.node_id!r} 不含有 class {cls!r}，无可移除项"
            )
    elif t == "set_attribute":
        name = _require_field(mutation, "name", "name")
        value = _require_field(mutation, "value", "value")
        if not name.strip():
            raise DomImpactValidationError("attribute name 不能为空白字符串")
        if name == "id":
            raise DomImpactValidationError(
                "不允许通过 set_attribute 修改 id（id 是节点标识字段）"
            )
        if name == "class":
            raise DomImpactValidationError(
                "不允许通过 set_attribute 修改 class，请改用 add_class / remove_class"
            )
        if target.attributes.get(name) == value:
            raise DomImpactValidationError(
                f"空操作：节点 {mutation.node_id!r} 的属性 {name!r} 已经是 {value!r}"
            )
    elif t == "remove_attribute":
        name = _require_field(mutation, "name", "name")
        if not name.strip():
            raise DomImpactValidationError("attribute name 不能为空白字符串")
        if name == "id":
            raise DomImpactValidationError("不允许通过 remove_attribute 删除 id")
        if name == "class":
            raise DomImpactValidationError(
                "不允许通过 remove_attribute 删除 class，请改用 remove_class"
            )
        if name not in target.attributes:
            raise DomImpactValidationError(
                f"空操作：节点 {mutation.node_id!r} 不存在属性 {name!r}，无可删除项"
            )
    else:  # set_tag
        tag = _require_field(mutation, "tag", "tag")
        if not tag.strip():
            raise DomImpactValidationError("tag 不能为空白字符串")
        if tag.strip().lower() == target.tag.lower():
            raise DomImpactValidationError(
                f"空操作：节点 {mutation.node_id!r} 的 tag 已经是 {tag!r}"
            )

    return MutationOut(
        type=t,
        node_id=mutation.node_id,
        class_name=mutation.class_name,
        name=mutation.name,
        value=mutation.value,
        tag=mutation.tag,
    )


def apply_mutation(root: AuditElement, mutation: DomMutation) -> AuditElement:
    """深拷贝原树并原子应用变更。调用前 validate_mutation 已保证操作非空。"""
    new_root = root.model_copy(deep=True)
    target = _collect_nodes(new_root)[mutation.node_id]
    t = mutation.type
    if t == "add_class":
        target.classes.append(mutation.class_name)
    elif t == "remove_class":
        target.classes.remove(mutation.class_name)
    elif t == "set_attribute":
        target.attributes[mutation.name] = mutation.value
    elif t == "remove_attribute":
        del target.attributes[mutation.name]
    else:  # set_tag
        target.tag = mutation.tag.strip()
    return new_root


# ---------- 节点状态抽取（基于内核共享签名） ----------


def _chains_of(flat):
    """展平树 -> 内核求解所需的 (目标元素, 祖先链) 先序序列。"""
    return [(node.element, node.ancestors) for node in flat]


def _signatures(node: NodeAnalysis, is_custom: bool) -> dict[str, PropSignature]:
    """节点上普通 / 自定义属性的共享签名表（未出现的属性即 absent / missing）。"""
    sig = node.signature()
    return dict(sig.custom if is_custom else sig.normal)


def _candidate_locs(node: NodeAnalysis, is_custom: bool) -> dict[str, set[tuple[int, int]]]:
    """各属性命中该元素的候选声明定位（胜出 + 落选；自定义属性只含本元素）。"""
    if is_custom:
        return {name: set(locs) for name, locs in node.custom_candidate_locs().items()}
    return {prop: set(info.candidate_locs) for prop, info in node.normals.items()}


# ---------- 原因归因 ----------


def _normal_reasons(b_locs, a_locs, b_loc, a_loc, b_state, a_state) -> list[str]:
    reasons: list[str] = []
    # 规则集不变，选择器命中变化只有在驱动胜出者换人时才是该差异的原因；
    # 若仅多 / 少了落选候选、胜出者仍是同一条，则真正原因是 var() 链
    if b_locs != a_locs and b_loc != a_loc:
        reasons.append("selector_match")
    if b_loc != a_loc:
        reasons.append("cascade_winner")
    elif b_state != a_state:
        # 胜出声明是同一条，取值 / 失效变化只能来自 var() 依赖链
        reasons.append("var_chain")
    return reasons


def _custom_reasons(b_entry, a_entry, b_locs, a_locs, b_loc, a_loc, b_state, a_state) -> list[str]:
    reasons: list[str] = []
    if b_locs != a_locs and b_loc != a_loc:
        reasons.append("selector_match")
    if b_loc != a_loc:
        reasons.append("cascade_winner")
    inherited_side = (b_entry and b_loc is None) or (a_entry and a_loc is None)
    if inherited_side and (b_loc != a_loc or b_state != a_state):
        reasons.append("inheritance")
    elif not inherited_side and b_loc == a_loc and b_state != a_state:
        # 本元素胜出声明不变，变化来自其 var() 依赖（经祖先继承传导）
        reasons.append("var_chain")
    return reasons


# ---------- 差异渲染 ----------


def _state_phrase(status: str, value, reason) -> str:
    if status == "absent":
        return "无胜出声明（absent）"
    if status == "missing":
        return "祖先链上不存在（missing）"
    if status == "invalid":
        return f"invalid（{reason}）"
    return f"{status} = {value!r}"


def _value_out(
    node: NodeAnalysis, prop: str, is_custom: bool, sig: PropSignature | None
) -> PropertyValueOut:
    if sig is None:
        return PropertyValueOut(status="missing" if is_custom else "absent")
    winner: CandidateOut | None
    if is_custom:
        state = node.customs[prop]
        winner = state.winner.to_out() if state.winner else None
    else:
        winner = node.normals[prop].winner.to_out()
    return PropertyValueOut(
        status=sig.status, value=sig.value, reason=sig.reason, winner=winner
    )


def _selector_match_sentence(reasons, b_locs, a_locs, rules) -> str | None:
    if "selector_match" not in reasons:
        return None
    msgs: list[str] = []
    for loc in sorted(a_locs - b_locs):
        msgs.append(
            f"规则 {loc[0]} 第 {loc[1]} 条声明（选择器 {rules[loc[0]].selector!r}）新命中该元素"
        )
    for loc in sorted(b_locs - a_locs):
        msgs.append(
            f"规则 {loc[0]} 第 {loc[1]} 条声明（选择器 {rules[loc[0]].selector!r}）不再命中该元素"
        )
    return "选择器命中发生变化：" + "；".join(msgs)


def _cascade_sentence(before: PropertyValueOut, after: PropertyValueOut) -> str | None:
    bw, aw = before.winner, after.winner
    if bw and aw:
        return (
            f"层叠胜出声明由规则 {bw.rule_index} 第 {bw.declaration_index} 条"
            f"（{bw.value!r}）改由规则 {aw.rule_index} 第 {aw.declaration_index} 条"
            f"（{aw.value!r}）胜出"
        )
    if aw:
        return f"变更后该属性新由规则 {aw.rule_index} 第 {aw.declaration_index} 条（{aw.value!r}）胜出"
    if bw:
        return (
            f"原层叠胜出声明规则 {bw.rule_index} 第 {bw.declaration_index} 条"
            f"（{bw.value!r}）落选，该属性不再有胜出声明"
        )
    return None


def _inheritance_sentence(before: PropertyValueOut, after: PropertyValueOut) -> str | None:
    if before.winner is None and after.winner is None:
        return "本元素未声明该自定义属性，变化经继承自祖先传导"
    if before.winner is None:
        return "变更前本元素无胜出声明、取值继承自祖先，变更后改由本元素声明胜出"
    return "变更后本元素胜出声明消失，取值退化为沿祖先链继承（或缺失）"


def _var_chain_sentence(before: PropertyValueOut, after: PropertyValueOut) -> str:
    return (
        "层叠胜出声明相同，但 var() 依赖的自定义属性取值 / 失效状态变化，"
        f"传导为 {_state_phrase(before.status, before.value, before.reason)} → "
        f"{_state_phrase(after.status, after.value, after.reason)}"
    )


def _describe(
    prop: str,
    is_custom: bool,
    reasons: list[str],
    before: PropertyValueOut,
    after: PropertyValueOut,
    b_locs: set,
    a_locs: set,
    rules,
) -> str:
    kind = "自定义属性" if is_custom else "普通属性"
    parts = [
        f"{kind} {prop}：{_state_phrase(before.status, before.value, before.reason)} → "
        f"{_state_phrase(after.status, after.value, after.reason)}"
    ]
    sentence = _selector_match_sentence(reasons, b_locs, a_locs, rules)
    if sentence:
        parts.append(sentence)
    if "cascade_winner" in reasons:
        sentence = _cascade_sentence(before, after)
        if sentence:
            parts.append(sentence)
    if "inheritance" in reasons:
        sentence = _inheritance_sentence(before, after)
        if sentence:
            parts.append(sentence)
    if "var_chain" in reasons:
        parts.append(_var_chain_sentence(before, after))
    return "；".join(parts)


# ---------- 逐节点差异 ----------


def _diff_property(
    node_b: NodeAnalysis,
    node_a: NodeAnalysis,
    prop: str,
    is_custom: bool,
    sigs_b: dict[str, PropSignature],
    sigs_a: dict[str, PropSignature],
    locs_b: dict[str, set],
    locs_a: dict[str, set],
    rules,
) -> PropertyImpactOut | None:
    b_sig = sigs_b.get(prop)
    a_sig = sigs_a.get(prop)
    # 共享签名一致（胜出定位 + 状态 + 取值 + 失效原因）即未变化，不列入结果；
    # 取值 / 状态相同但层叠胜出声明换人也算变化（与 /stylesheet-audit、
    # 补丁预演同一签名口径：继任者同值仍属差异）
    if b_sig == a_sig:
        return None

    b_loc = b_sig.winner if b_sig else None
    a_loc = a_sig.winner if a_sig else None
    b_state = (b_sig.status, b_sig.value, b_sig.reason) if b_sig else None
    a_state = (a_sig.status, a_sig.value, a_sig.reason) if a_sig else None
    b_locs = locs_b.get(prop, set())
    a_locs = locs_a.get(prop, set())

    if is_custom:
        reasons = _custom_reasons(
            b_sig is not None,
            a_sig is not None,
            b_locs,
            a_locs,
            b_loc,
            a_loc,
            b_state,
            a_state,
        )
    else:
        reasons = _normal_reasons(b_locs, a_locs, b_loc, a_loc, b_state, a_state)
    reasons = [r for r in _REASON_ORDER if r in reasons]

    before = _value_out(node_b, prop, is_custom, b_sig)
    after = _value_out(node_a, prop, is_custom, a_sig)
    return PropertyImpactOut(
        property=prop,
        is_custom=is_custom,
        reasons=reasons,
        before=before,
        after=after,
        detail=_describe(
            prop, is_custom, reasons, before, after, b_locs, a_locs, rules
        ),
    )


def _diff_node(node_b: NodeAnalysis, node_a: NodeAnalysis, rules) -> list[PropertyImpactOut]:
    changes: list[PropertyImpactOut] = []

    nb, na = _signatures(node_b, False), _signatures(node_a, False)
    lb, la = _candidate_locs(node_b, False), _candidate_locs(node_a, False)
    for prop in sorted(set(nb) | set(na)):
        change = _diff_property(
            node_b, node_a, prop, False, nb, na, lb, la, rules
        )
        if change is not None:
            changes.append(change)

    cb, ca = _signatures(node_b, True), _signatures(node_a, True)
    clb, cla = _candidate_locs(node_b, True), _candidate_locs(node_a, True)
    for name in sorted(set(cb) | set(ca)):
        change = _diff_property(
            node_b, node_a, name, True, cb, ca, clb, cla, rules
        )
        if change is not None:
            changes.append(change)

    return changes


# ---------- 入口 ----------


def analyze_dom_impact(req: DomImpactRequest) -> DomImpactResponse:
    # 1) 全部结构 / 语法 / 语义校验先于任何求解，失败即整体 422
    flat = flatten_tree(req.root)  # 重复 id / 空 tag 等在此抛
    validate_rules(req.rules)  # 空白 property
    ruleset = RuleSet.build(req.rules, req.layers)  # 选择器语法非法抛 SelectorSyntaxError
    nodes = _collect_nodes(req.root)
    echo = validate_mutation(req.mutation, nodes)

    # 2) 基线求解（整树，先序）
    baseline = solve_forest(ruleset, _chains_of(flat))

    # 3) 原子应用变更并重新求解整棵树（不增删节点，先序下标与基线一一对应）
    new_root = apply_mutation(req.root, req.mutation)
    new_flat = flatten_tree(new_root)
    post = solve_forest(ruleset, _chains_of(new_flat))

    # 4) 逐节点比对共享签名，未受影响节点不列出
    affected: list[NodeImpactOut] = []
    for node, world_b, world_a in zip(flat, baseline.nodes, post.nodes):
        changes = _diff_node(world_b, world_a, req.rules)
        if changes:
            affected.append(NodeImpactOut(node_id=node.raw_id, changes=changes))

    return DomImpactResponse(
        nodes_checked=len(flat),
        target_id=req.mutation.node_id,
        mutation=echo,
        affected_nodes=affected,
    )
