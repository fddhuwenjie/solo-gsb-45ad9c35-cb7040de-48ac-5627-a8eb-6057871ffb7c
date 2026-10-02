"""DOM 变更后的样式影响分析。

给定一棵带唯一 id 的 DOM 样本树、层顺序与结构化规则，以及一次对指定节点的
原子变更（添加 / 移除 class、设置 / 删除 attribute、修改 tag）：

1. 先把原树交给不可变整树内核 kernel.analyze_tree 求基线（与 /compute、
   /stylesheet-audit、补丁预演共享同一份规则解析与节点签名）；
2. 校验全部通过后再原子应用变更，重新求解**整棵树**；
3. 用 **kernel.NodeAnalysis.signature()** 的统一四元组
   (胜出定位, 状态, 取值, 失效原因) 逐节点判变，只列出签名发生变化的
   节点与属性，并区分四类传播原因：
     - selector_match：变更使规则选择器对该元素的命中发生变化（含祖先链
       被变更后经后代 / 子组合符传导到后代元素的情况）；
     - cascade_winner：该属性的层叠胜出声明换人（含属性消失 / 新增）；
     - inheritance：自定义属性本元素无胜出声明，变化经继承自祖先传导；
     - var_chain：胜出声明相同，取值 / 失效状态经 var() 依赖链变化。

因为胜出定位本身是签名分量，「胜出者改变但值相同」一定会列出；四个
分量全同的节点（含未改变的失效原因）绝不列出。目标不存在、重复 id、
空操作或非法规则一律 422；所有校验在任何求解之前完成。
"""
from __future__ import annotations

from .audit import flatten_tree, solve_tree, validate_rules
from .engine import parse_rules
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


# ---------- 节点状态抽取（全部取自内核 NodeAnalysis） ----------


def _normal_states(node) -> dict[str, tuple]:
    """普通属性：name -> (status, value, invalid_reason)。"""
    return {
        prop: (info.status, info.value, info.reason)
        for prop, info in node.normals.items()
    }


def _custom_states(node) -> dict[str, tuple]:
    """自定义属性：name -> (status, value, invalid_reason)，含纯继承得到的。

    失效原因由内核沿继承链给出（循环 / var() 失败的具体原因）；内核未
    记录原因时退回通用说明。
    """
    out: dict[str, tuple] = {}
    for name, entry in node.custom.items():
        reason = entry.reason or (
            "继承自祖先的自定义属性已失效（循环依赖或 var() 解析失败）"
            if entry.status == "invalid"
            else None
        )
        out[name] = (entry.status, entry.value, reason)
    return out


def _winner_loc(node, prop: str, is_custom: bool):
    if is_custom:
        winner = node.custom_winner_candidate(prop)
    else:
        info = node.normals.get(prop)
        winner = info.winner if info else None
    if winner is None:
        return None
    return winner.rule_index, winner.decl_index


def _signature_tuple(node, prop: str, is_custom: bool):
    """内核统一签名分量 (定位, 状态, 取值, 原因)，缺失侧补哨兵。"""
    sig = node.signature().get(prop)
    if sig is not None:
        return sig
    return (None, "missing" if is_custom else "absent", None, None)


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
    # b_state / a_state 为 (status, value) 元组；与统一签名同口径：
    # 胜出声明不变时，状态翻转或取值变化（含两侧都 invalid 但失效原因不同）
    # 都来自其 var() 依赖
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


def _value_out(node, prop: str, is_custom: bool, state: tuple | None) -> PropertyValueOut:
    if state is None:
        return PropertyValueOut(status="missing" if is_custom else "absent")
    status, value, reason = state
    if is_custom:
        winner = node.custom_winner_candidate(prop)
        winner_out: CandidateOut | None = winner.to_out() if winner else None
    else:
        info = node.normals.get(prop)
        winner_out = info.winner.to_out() if info else None
    return PropertyValueOut(status=status, value=value, reason=reason, winner=winner_out)


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
    node_b,
    node_a,
    prop: str,
    is_custom: bool,
    states_b,
    states_a,
    rules,
) -> PropertyImpactOut | None:
    b_state = states_b.get(prop)
    a_state = states_a.get(prop)
    # 判变直接使用内核统一签名（胜出定位 + 状态 + 取值 + 失效原因）：
    # 取值 / 状态相同但层叠胜出声明换人也算变化；四分量全同则不列出
    b_cmp = _signature_tuple(node_b, prop, is_custom)
    a_cmp = _signature_tuple(node_a, prop, is_custom)
    if b_cmp == a_cmp:
        return None
    (_b_loc, b_status, _b_value, _b_reason) = b_cmp
    (_a_loc, a_status, _a_value, _a_reason) = a_cmp
    b_loc = _winner_loc(node_b, prop, is_custom)
    a_loc = _winner_loc(node_a, prop, is_custom)
    b_locs = (
        node_b.normal_candidate_locs(prop)
        if not is_custom
        else node_b.custom_candidate_locs(prop)
    )
    a_locs = (
        node_a.normal_candidate_locs(prop)
        if not is_custom
        else node_a.custom_candidate_locs(prop)
    )

    if is_custom:
        reasons = _custom_reasons(
            b_state is not None,
            a_state is not None,
            b_locs,
            a_locs,
            b_loc,
            a_loc,
            (b_status, _b_value),
            (a_status, _a_value),
        )
        # 胜出声明相同、状态与取值相同，仅失效原因不同（如循环解除后变为
        # 引用未定义变量）：差异来自 var() 依赖链，且不属于继承传导
        if (
            not reasons
            and b_loc == a_loc
            and (b_status, _b_value) == (a_status, _a_value)
            and _b_reason != _a_reason
        ):
            reasons = ["var_chain"]
    else:
        reasons = _normal_reasons(
            b_locs, a_locs, b_loc, a_loc,
            (b_status, _b_value), (a_status, _a_value),
        )
        if (
            not reasons
            and b_loc == a_loc
            and (b_status, _b_value) == (a_status, _a_value)
            and _b_reason != _a_reason
        ):
            reasons = ["var_chain"]
    reasons = [r for r in _REASON_ORDER if r in reasons]

    before = _value_out(node_b, prop, is_custom, b_state)
    after = _value_out(node_a, prop, is_custom, a_state)
    return PropertyImpactOut(
        property=prop,
        is_custom=is_custom,
        reasons=reasons,
        before=before,
        after=after,
        detail=_describe(
            prop, is_custom, reasons, before, after, set(b_locs), set(a_locs), rules
        ),
    )


def _diff_node(node_b, node_a, rules) -> list[PropertyImpactOut]:
    """普通属性按名排序后，自定义属性按名排序（保持旧响应次序）。"""
    changes: list[PropertyImpactOut] = []

    nb, na = _normal_states(node_b), _normal_states(node_a)
    for prop in sorted(set(nb) | set(na)):
        change = _diff_property(node_b, node_a, prop, False, nb, na, rules)
        if change is not None:
            changes.append(change)

    cb, ca = _custom_states(node_b), _custom_states(node_a)
    for name in sorted(set(cb) | set(ca)):
        change = _diff_property(node_b, node_a, name, True, cb, ca, rules)
        if change is not None:
            changes.append(change)

    return changes


# ---------- 入口 ----------


def analyze_dom_impact(req: DomImpactRequest) -> DomImpactResponse:
    # 1) 全部结构 / 语法 / 语义校验先于任何求解，失败即整体 422
    flat = flatten_tree(req.root)  # 重复 id / 空 tag 等在此抛
    validate_rules(req.rules)  # 空白 property
    parsed = parse_rules(req.rules)  # 选择器语法非法抛 SelectorSyntaxError
    nodes = _collect_nodes(req.root)
    echo = validate_mutation(req.mutation, nodes)

    # 2) 基线求解（不可变整树内核）
    baseline_tree = solve_tree(flat, req.layers, req.rules, frozenset(), parsed)

    # 3) 原子应用变更并重新求解整棵树（不增删节点，先序下标与基线一一对应）
    new_root = apply_mutation(req.root, req.mutation)
    new_flat = flatten_tree(new_root)
    post_tree = solve_tree(new_flat, req.layers, req.rules, frozenset(), parsed)

    # 4) 逐节点用同一套内核签名比对，未受影响节点不列出
    affected: list[NodeImpactOut] = []
    for fnode, node_b, node_a in zip(flat, baseline_tree.nodes, post_tree.nodes):
        changes = _diff_node(node_b, node_a, req.rules)
        if changes:
            affected.append(NodeImpactOut(node_id=fnode.raw_id, changes=changes))

    return DomImpactResponse(
        nodes_checked=len(flat),
        target_id=req.mutation.node_id,
        mutation=echo,
        affected_nodes=affected,
    )
