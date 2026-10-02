"""样式变更预演：在既有元素链 / 层 / 规则上叠加定位式补丁，对比基线与补丁后
的计算结果，给出逐项差异、因果轨迹与未生效（shadowed）补丁。

所有求解都走不可变整树内核（kernel.analyze_chain），补丁世界复用基线
世界已解析的选择器（RuleSet.with_changes），两个世界的节点签名与
/stylesheet-audit、/dom-impact 是同一个 kernel.NodeAnalysis.signature。

补丁只允许修改既有声明的 value / important / layer，不增删声明、不改
属性名与选择器，因此两个世界中参与层叠的属性集合不变，胜出声明可以用
(rule_index, declaration_index) 唯一比对。

归因采用留一法（leave-one-out）：逐个把某条补丁从补丁集中拿掉复算，若目标
元素结果与全量补丁一致，则该补丁未影响结果（shadowed）；否则它对相应的
差异负责。因果轨迹在两个世界的数据依赖并集图上做最短路：

- 声明节点 ("decl", rule_index, decl_index)
- 自定义属性节点 ("custom_at", depth, name)（目标元素 depth 为链末）
- 普通属性节点 ("normal", property)

边方向为「消费者 → 取值来源」（var 引用、继承、层叠胜出声明、循环失效），
从被改声明反向搜索到最终属性；跨世界变化的边以【基线】/【补丁后】对照渲染。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass

from .kernel import NodeAnalysis, RuleSet, analyze_chain
from .models import (
    ComputePreview,
    ComputePreviewResponse,
    ComputeRequest,
    InvalidDeclOut,
    PatchRefOut,
    PreviewSnapshotOut,
    PropertyChangeOut,
    ShadowedPatchOut,
)
from .variables import VarEvent


class PatchError(ValueError):
    """补丁定位或内容非法（整次请求 422，不产生部分结果）。"""


# ---------- 校验与应用 ----------


def _validate_patches(num_rules: int, rules, patches) -> None:
    seen: set[tuple[int, int]] = set()
    for i, p in enumerate(patches):
        where = f"patches[{i}]（rule_index={p.rule_index}, declaration_index={p.declaration_index}）"
        ri, di = p.rule_index, p.declaration_index
        if ri < 0 or ri >= num_rules:
            raise PatchError(
                f"{where} 定位非法：rule_index {ri} 越界，rules 共 {num_rules} 条"
            )
        decls = rules[ri].declarations
        if di < 0 or di >= len(decls):
            raise PatchError(
                f"{where} 定位非法：规则 {ri} 只有 {len(decls)} 条声明，"
                f"declaration_index 应在 [0, {len(decls) - 1}] 内"
            )
        if (ri, di) in seen:
            raise PatchError(f"{where} 与前一条补丁定位重复，同一声明只能携带一条补丁")
        seen.add((ri, di))
        changed = [f for f in ("value", "important") if f in p.model_fields_set]
        if p.layer_explicitly_set():
            changed.append("layer")
        if not changed:
            raise PatchError(f"{where} 未携带任何修改：value / important / layer 至少给一个")


def apply_patches(rules, patches):
    """深拷贝规则并整体应用 value / important 补丁；layer 补丁不在 Rule 上
    修改（layer 是规则级字段，而补丁按声明定位），而是返回
    (patched_rules, layer_overrides)，由内核按声明覆盖层。

    调用前应已通过 _validate_patches。
    """
    patched = [r.model_copy(deep=True) for r in rules]
    layer_overrides: dict[tuple[int, int], str | None] = {}
    for p in patches:
        decl = patched[p.rule_index].declarations[p.declaration_index]
        if "value" in p.model_fields_set:
            decl.value = p.value
        if "important" in p.model_fields_set:
            decl.important = p.important
        if p.layer_explicitly_set():
            layer_overrides[(p.rule_index, p.declaration_index)] = p.layer
    return patched, layer_overrides


@dataclass(frozen=True)
class World:
    """一个求解世界：不可变规则表 + 不可变目标节点分析（内核对象）。"""

    node: NodeAnalysis

    @property
    def rules(self):
        return self.node.ruleset.rules

    @property
    def layer_overrides(self):
        return self.node.ruleset.layer_overrides

    @property
    def chain(self):
        return [d.element for d in self.node.depth_info]

    @property
    def depths(self):
        return self.node.depth_info

    @property
    def element_custom(self):
        return self.node.custom

    @property
    def normals(self):
        return self.node.normals

    @property
    def computed(self):
        return self.node.computed

    @property
    def custom_properties(self):
        return self.node.custom_properties

    @property
    def invalid(self):
        return self.node.invalid

    @property
    def custom_winners(self):
        if not self.node.depth_info:
            return {}
        return self.node.depth_info[-1].winners


def _solve_world(element, ancestors, layers, rules, layer_overrides=None, disabled=None, parsed=None) -> World:
    if parsed is not None:
        ruleset = RuleSet.parse(
            rules,
            layers,
            layer_overrides=layer_overrides,
            disabled=disabled,
            parsed=parsed,
        )
    else:
        ruleset = RuleSet.parse(
            rules, layers, layer_overrides=layer_overrides, disabled=disabled
        )
    return World(analyze_chain(element, ancestors, ruleset))


def _effective_layer(world: World, ri: int, di: int):
    if (ri, di) in world.layer_overrides:
        return world.layer_overrides[(ri, di)]
    return world.rules[ri].layer


# ---------- 快照与签名 ----------


def _snapshot(world: World) -> PreviewSnapshotOut:
    return PreviewSnapshotOut(
        computed=dict(world.computed),
        custom_properties=dict(world.custom_properties),
        invalid=[
            InvalidDeclOut(property=i.property, value=i.value, reason=i.reason)
            for i in world.invalid
        ],
    )


def _normal_sig(world: World, prop: str):
    info = world.normals[prop]
    return (info.status, info.value)


def _custom_sig(world: World, name: str):
    entry = world.element_custom.get(name)
    if entry is None:
        return ("missing", None)
    return entry.status, entry.value


def _world_signature(world: World):
    """目标元素最终结果签名：直接复用三个入口共享的内核节点签名
    （胜出定位 + 状态 + 取值 + 失效原因，普通 + 自定义属性）。"""
    return tuple(sorted(world.node.signature().items()))


# ---------- 因果依赖图 ----------


def _decl_node(winner) -> tuple:
    return ("decl", winner.rule_index, winner.decl_index)


def _custom_node(depth: int, name: str) -> tuple:
    return ("custom_at", depth, name)


def _origin_of_custom(world: World, depth: int, name: str):
    """name 在 depth 层解析值的声明来源：取不晚于 depth 的最近一层本元素胜出声明。"""
    for d in range(depth, -1, -1):
        if name in world.depths[d].winners:
            return _custom_node(d, name)
    return None


def _is_cyclic_at(world: World, depth: int, name: str) -> bool:
    return name in world.depths[depth].cyclic


def _invalid_word(world: World, depth: int, ref_status: str, name: str) -> str:
    if ref_status == "missing":
        return "未定义"
    if _is_cyclic_at(world, depth, name):
        return "因循环依赖而失效"
    return "已失效"


def _event_edges(
    world: World,
    depth: int,
    consumer: tuple,
    events: list[VarEvent],
    edges: list[tuple],
    prefix: str = "",
) -> None:
    for ev in events:
        v = _origin_of_custom(world, depth, ev.name)
        ref_depth = v[1] if v is not None else depth
        if ev.kind == "ok":
            if v is not None:
                edges.append((consumer, v, f"{prefix}var({ev.name}) 直接替换为 {ev.value!r}"))
        elif ev.kind == "fallback":
            word = _invalid_word(world, ref_depth, ev.ref_status, ev.name)
            if v is not None:
                edges.append(
                    (
                        consumer,
                        v,
                        f"{prefix}var({ev.name}) 引用的变量{word}，fallback 改道 → {ev.value!r}",
                    )
                )
                # fallback 分支内部引用同样可能是取值来源
                _event_edges(world, depth, consumer, ev.child_events, edges)
            else:
                # 引用未定义变量：改道原因挂到 fallback 实际取值的边上
                _event_edges(
                    world,
                    depth,
                    consumer,
                    ev.child_events,
                    edges,
                    prefix=f"{prefix}var({ev.name}) 未定义触发 fallback；",
                )
        else:  # fail
            if v is not None:
                word = _invalid_word(world, ref_depth, ev.ref_status, ev.name)
                edges.append(
                    (
                        consumer,
                        v,
                        f"{prefix}var({ev.name}) 引用的变量{word}且无可用 fallback，声明失效",
                    )
                )


def _build_edges(world: World) -> list[tuple[tuple, tuple, str]]:
    edges: list[tuple[tuple, tuple, str]] = []
    target_depth = len(world.chain) - 1

    for d, dinfo in enumerate(world.depths):
        # 层叠胜出声明边
        for name, (winner, _elim) in dinfo.winners.items():
            edges.append(
                (
                    _custom_node(d, name),
                    _decl_node(winner),
                    f"该声明在 {dinfo.label} 上为 {name} 的层叠胜出者",
                )
            )
        # 循环失效边（环内双向，保证任一端都能被追溯到）
        for name in dinfo.cyclic:
            self_loop = name in dinfo.deps.get(name, [])
            if self_loop:
                label = (
                    f"{dinfo.label} 上 {name} 自引用 var({name})，"
                    "形成单节点循环依赖而失效"
                )
                node = _custom_node(d, name)
                edges.append((node, node, label))
            for dep in dinfo.deps.get(name, []):
                v = _origin_of_custom(world, d, dep)
                if v is None:
                    continue
                if v == _custom_node(d, name):
                    # 自引用已在上面记录
                    continue
                pair = " ↔ ".join(sorted({name, dep}))
                label = f"{dinfo.label} 上 {pair} 形成循环依赖，相关自定义属性同时失效"
                edges.append((_custom_node(d, name), v, label))
                edges.append((v, _custom_node(d, name), label))
        # var() 取值边
        for name, evs in dinfo.events.items():
            _event_edges(world, d, _custom_node(d, name), evs, edges)
        # 非循环但自身解析失败（引用缺失 / 失效变量且无 fallback）：
        # 以其声明中每个失败 var() 为因果边，保证“变量失效”也可从声明追溯到
        for name, entry in dinfo.resolved.items():
            if name in dinfo.cyclic or entry.winner is None:
                continue
            if entry.status != "invalid":
                continue
            for ev in dinfo.events.get(name, []):
                for sub in ev.iter_all():
                    v = _origin_of_custom(world, d, sub.name)
                    if v is not None:
                        word = _invalid_word(world, v[1], sub.ref_status, sub.name)
                        edges.append(
                            (
                                _custom_node(d, name),
                                v,
                                f"var({sub.name}) 引用的变量{word}且无可用 fallback，"
                                f"{name} 在 {dinfo.label} 上失效",
                            )
                        )

    # 目标元素的继承边（本元素未声明、沿链取到祖先值）
    target_winners = world.depths[target_depth].winners if world.depths else {}
    for name, entry in world.element_custom.items():
        if name in target_winners:
            continue
        if entry.inherited_from is None:
            continue
        origin = _custom_node(entry.inherited_from, name)
        edges.append(
            (
                _custom_node(target_depth, name),
                origin,
                f"目标元素未声明 {name}，沿祖先链继承自 {world.depths[entry.inherited_from].label}",
            )
        )

    # 普通属性：胜出声明边 + var() 取值边
    for prop, info in world.normals.items():
        u = ("normal", prop)
        edges.append((u, _decl_node(info.winner), f"该声明为 {prop} 在目标元素的层叠胜出者"))
        _event_edges(world, target_depth, u, list(info.var_events), edges)

    return edges


def _merge_edges(baseline: World, patched: World):
    """合并两个世界的边。返回 rev 邻接（来源 -> [(消费者, 边信息)]）与全部边信息。

    每条 (u, v) 边记录它在基线 / 补丁后世界中的标签集合。
    """
    combined: dict[tuple, dict[str, list[str]]] = {}

    def add(world_name: str, edges):
        for u, v, label in edges:
            slot = combined.setdefault((u, v), {"baseline": [], "patched": []})
            if label not in slot[world_name]:
                slot[world_name].append(label)

    add("baseline", _build_edges(baseline))
    add("patched", _build_edges(patched))

    rev: dict[tuple, list[tuple[tuple, dict]]] = {}
    for (u, v), slot in combined.items():
        rev.setdefault(v, []).append((u, slot))
    return rev, combined


def _edge_cost(slot) -> tuple[int, int, int]:
    """边代价（越小越优先）：

    1. 长度：两世界共有且标签一致的稳定边 0，跨世界变化边 1；
    2. 变化优先：同等长度下，尽量多经过发生变化的边（能解释循环形成 / 解除、
       fallback 改道等），故变化边该维记 0、稳定边记 1；
    3. 世界偏好：同为变化边时优先补丁后边（叙述与补丁后结果一致），
       再优先基线边。
    """
    bl, pl = slot["baseline"], slot["patched"]
    if bl and pl and set(bl) == set(pl):
        return (0, 1, 0)
    if pl and not bl:
        return (1, 0, 0)
    if bl and not pl:
        return (1, 0, 1)
    return (1, 0, 2)  # 两世界都在但标签发生变化


def _add_cost(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _search_trail(rev, start: tuple, goal: tuple):
    """按 _edge_cost 的三维字典序找最短路（长度优先、变化边次之、世界偏好最后）。"""
    if start == goal:
        return []
    zero = (0, 0, 0)
    pq: list = [(zero, 0, start)]
    best = {start: zero}
    parent: dict[tuple, tuple] = {}
    counter = 0
    while pq:
        cost, _, node = heapq.heappop(pq)
        if cost != best.get(node):
            continue
        if node == goal:
            break
        for consumer, slot in rev.get(node, []):
            ncost = _add_cost(cost, _edge_cost(slot))
            if consumer == node:
                # 自环（单节点循环依赖）：仅用于在到达该节点后追加说明，
                # 不参与最短路比较与父子链
                continue
            if consumer not in best or ncost < best[consumer]:
                best[consumer] = ncost
                parent[consumer] = node
                counter += 1
                heapq.heappush(pq, (ncost, counter, consumer))
    if goal not in parent:
        return None
    # 回溯得到 source -> goal 序列及所用边
    path_nodes = [goal]
    while path_nodes[-1] != start:
        path_nodes.append(parent[path_nodes[-1]])
    path_nodes.reverse()

    def slot_between(src, dst):
        for consumer, slot in rev.get(src, []):
            if consumer == dst:
                return slot
        raise KeyError((src, dst))

    edges_used = []
    for src, dst in zip(path_nodes, path_nodes[1:]):
        edges_used.append((dst, src, slot_between(src, dst)))
    # 终点上的自环（自引用循环）作为最后一跳追加
    for consumer, slot in rev.get(goal, []):
        if consumer == goal:
            edges_used.append((goal, goal, slot))
            break
    return edges_used


def _render_hop(dst, src, slot) -> list[str]:
    """渲染一段 src（取值来源）-> dst（消费者）的轨迹行。"""
    bl, pl = slot["baseline"], slot["patched"]
    if bl and pl:
        if set(bl) == set(pl):
            return [bl[0]]
        return [f"【基线】{bl[0]}", f"【补丁后】{pl[0]}"]
    if pl:
        return [f"【补丁后】{pl[0]}"]
    return [f"【基线】{bl[0]}"]


def _node_context(node: tuple, world: World) -> str:
    if node[0] == "normal":
        return f"最终普通属性 {node[1]}"
    if node[0] == "custom_at":
        _, d, name = node
        return f"{world.depths[d].label} 上的自定义属性 {name}"
    _, ri, di = node
    prop = world.rules[ri].declarations[di].property
    return f"规则 {ri} 第 {di} 条声明（{prop}）"


def _build_trail(rev, baseline, patched, patch, goal) -> list[str]:
    start = ("decl", patch.rule_index, patch.declaration_index)
    used = _search_trail(rev, start, goal)
    if used is None:
        return []
    before_decl = baseline.rules[patch.rule_index].declarations[patch.declaration_index]
    after_decl = patched.rules[patch.rule_index].declarations[patch.declaration_index]
    before_layer = _effective_layer(baseline, patch.rule_index, patch.declaration_index)
    after_layer = _effective_layer(patched, patch.rule_index, patch.declaration_index)
    changes = []
    if before_decl.value != after_decl.value:
        changes.append(f"value {before_decl.value!r} → {after_decl.value!r}")
    if before_decl.important != after_decl.important:
        changes.append(f"important {before_decl.important} → {after_decl.important}")
    if before_layer != after_layer:
        changes.append(f"layer {before_layer!r} → {after_layer!r}")
    lines = [
        f"补丁作用于 {_node_context(start, patched)}：{'; '.join(changes)}",
    ]
    for dst, src, slot in used:
        consumer_world = patched if slot["patched"] else baseline
        ctx = _node_context(dst, consumer_world)
        for line in _render_hop(dst, src, slot):
            lines.append(f"{ctx}：{line}")
    lines.extend(_winner_handoff_lines(rev, patched, goal, used))
    return lines


def _winner_handoff_lines(rev, patched, goal, used):
    """胜出者发生交接而路径终点未体现新胜出者时，补一行改由谁胜出的说明。"""
    bl_src = pl_src = None
    for src, consumers in rev.items():
        if src[0] != "decl":
            continue
        for consumer, slot in consumers:
            if consumer != goal:
                continue
            if slot["baseline"]:
                bl_src = src
            if slot["patched"]:
                pl_src = src
    if bl_src == pl_src or pl_src is None:
        return []
    # 路径已含补丁后的胜出者边（最后一跳或任一跳）时无需补充
    handoff = f"【补丁后】层叠胜出者改由 {_node_context(pl_src, patched)}"
    for _dst, _src, slot in used:
        if slot["patched"] and _src == pl_src:
            return []
        for raw in slot["patched"]:
            if raw == handoff:
                return []
    ctx = _node_context(goal, patched)
    return [f"{ctx}：{handoff}"]


# ---------- 差异构建 ----------


def _winner_loc(winner) -> tuple[int, int]:
    return (winner.rule_index, winner.decl_index)


def _same_winner_kind(patched_rules, loc, patches):
    """胜出声明不变时区分 edited / var_changed：

    - edited：补丁直接把胜出声明的 value 改为不含 var() 的字面值，
      变化与变量解析无关；
    - var_changed：新值仍依赖 var()（可能因循环 / fallback / 依赖值变化而
      改值或失效），或补丁只动了 important / layer 却经由层叠链影响取值。
    """
    patch_at_loc = next(
        (p for p in patches if (p.rule_index, p.declaration_index) == loc),
        None,
    )
    decl = patched_rules[loc[0]].declarations[loc[1]]
    if (
        patch_at_loc is not None
        and "value" in patch_at_loc.model_fields_set
        and "var(" not in decl.value
    ):
        return "edited"
    return "var_changed"


def _normal_changes(
    baseline: World,
    patched: World,
    patched_rules,
    patches,
    loo_worlds,
    rev,
) -> list[PropertyChangeOut]:
    changes = []
    invalid_map_b = {i.property: i.reason for i in baseline.invalid}
    invalid_map_p = {i.property: i.reason for i in patched.invalid}
    for prop in sorted(baseline.normals):
        b, a = baseline.normals[prop], patched.normals[prop]
        loc_b, loc_a = _winner_loc(b.winner), _winner_loc(a.winner)
        winner_changed = loc_b != loc_a
        if not winner_changed and (b.status, b.value) == (a.status, a.value):
            continue
        if winner_changed:
            kind = "winner_changed"
        else:
            kind = _same_winner_kind(patched.rules, loc_a, patches)
        responsible = [
            i
            for i, w in enumerate(loo_worlds)
            if _normal_sig(w, prop) != _normal_sig(patched, prop)
        ]
        goal = ("normal", prop)
        trails: list[str] = []
        for i in responsible:
            one = _build_trail(rev, baseline, patched, patches[i], goal)
            if one and trails:
                trails.append("")
            trails.extend(one)
        changes.append(
            PropertyChangeOut(
                property=prop,
                kind=kind,
                is_custom=False,
                status_changed=b.status != a.status,
                before_status=b.status,
                after_status=a.status,
                before_value=b.value,
                after_value=a.value,
                before_winner=b.winner.to_out(),
                after_winner=a.winner.to_out(),
                before_reason=invalid_map_b.get(prop),
                after_reason=invalid_map_p.get(prop),
                responsible_patches=[
                    PatchRefOut(
                        index=i,
                        rule_index=patches[i].rule_index,
                        declaration_index=patches[i].declaration_index,
                    )
                    for i in responsible
                ],
                trail=trails,
            )
        )
    return changes


def _custom_changes(
    baseline: World,
    patched: World,
    patched_rules,
    patches,
    loo_worlds,
    rev,
) -> list[PropertyChangeOut]:
    changes = []
    names = sorted(set(baseline.element_custom) | set(patched.element_custom))
    target_depth = len(baseline.chain) - 1
    for name in names:
        sb, vb = _custom_sig(baseline, name)
        sa, va = _custom_sig(patched, name)
        wb = baseline.node.custom_winner_candidate(name)
        wa = patched.node.custom_winner_candidate(name)
        loc_b = _winner_loc(wb) if wb else None
        loc_a = _winner_loc(wa) if wa else None
        winner_changed = loc_b != loc_a
        if not winner_changed and (sb, vb) == (sa, va):
            continue
        if winner_changed:
            kind = "winner_changed"
        elif loc_a is None:
            # 纯继承的自定义属性，本元素无胜出声明，变化必来自变量链
            kind = "var_changed"
        else:
            kind = _same_winner_kind(patched_rules, loc_a, patches)
        responsible = [
            i
            for i, w in enumerate(loo_worlds)
            if _custom_sig(w, name) != _custom_sig(patched, name)
        ]
        goal = _custom_node(target_depth, name)
        trails: list[str] = []
        for i in responsible:
            one = _build_trail(rev, baseline, patched, patches[i], goal)
            if one and trails:
                trails.append("")
            trails.extend(one)
        changes.append(
            PropertyChangeOut(
                property=name,
                kind=kind,
                is_custom=True,
                status_changed=sb != sa,
                before_status=sb,
                after_status=sa,
                before_value=vb,
                after_value=va,
                before_winner=wb.to_out() if wb else None,
                after_winner=wa.to_out() if wa else None,
                responsible_patches=[
                    PatchRefOut(
                        index=i,
                        rule_index=patches[i].rule_index,
                        declaration_index=patches[i].declaration_index,
                    )
                    for i in responsible
                ],
                trail=trails,
            )
        )
    return changes


def _declaration_is_candidate(world: World, ri: int, di: int, is_custom: bool) -> bool:
    """该声明是否在某元素的层叠候选集合中（胜出或落选）。"""
    pools = [d.winners for d in world.depths] if is_custom else []
    if not is_custom:
        pools = [
            {prop: (info.winner, info.eliminated)}
            for prop, info in world.normals.items()
        ]
    for winners in pools:
        for _name, (winner, eliminated) in winners.items():
            if (winner.rule_index, winner.decl_index) == (ri, di):
                return True
            if any(
                (e.candidate.rule_index, e.candidate.declaration_index) == (ri, di)
                for e in eliminated
            ):
                return True
    return False


def _declaration_wins_relevant(world: World, ri: int, di: int, is_custom: bool) -> bool:
    """该声明是否在决定目标结果的位置胜出：普通属性看目标元素；自定义属性
    看它是否在链上任一层胜出（中间层胜出后被更深层覆盖也算“曾经胜出”）。"""
    if is_custom:
        return any(
            (winner.rule_index, winner.decl_index) == (ri, di)
            for d in world.depths
            for winner, _elim in d.winners.values()
        )
    return any(
        (info.winner.rule_index, info.winner.decl_index) == (ri, di)
        for info in world.normals.values()
    )


def _shadowed_reason(patch, baseline: World, patched: World) -> str:
    ri, di = patch.rule_index, patch.declaration_index
    is_custom = patched.rules[ri].declarations[di].property.startswith("--")
    candidate = (
        _declaration_is_candidate(baseline, ri, di, is_custom)
        or _declaration_is_candidate(patched, ri, di, is_custom)
    )
    if not candidate:
        if is_custom:
            return "规则选择器未命中元素链上任何元素，该自定义属性声明不参与任何元素的层叠，补丁无效"
        return "规则选择器未命中目标元素，声明不参与目标属性的层叠，补丁无效"
    wins = _declaration_wins_relevant(
        baseline, ri, di, is_custom
    ) or _declaration_wins_relevant(patched, ri, di, is_custom)
    if not wins:
        return "声明在层叠竞争中落选（来源 / 层 / 重要度 / 特异性 / 源码次序均未胜出），补丁无效"
    if _declaration_wins_relevant(patched, ri, di, is_custom):
        return (
            "补丁作用的声明在补丁后仍是胜出者，但对它的修改既未改变其胜出地位，"
            "也未传导为最终取值变化（它本就凭其他层叠维度胜出，或改动未触及取值依赖）"
        )
    return (
        "补丁作用的声明在基线上胜出，但移除该补丁后目标元素复算结果与全量补丁一致："
        "其他补丁已使胜出地位转移，该补丁不构成结果差异"
    )


# ---------- 入口 ----------


def compute_with_preview(req: ComputeRequest) -> ComputePreviewResponse:
    patches = req.patches
    _validate_patches(len(req.rules), req.rules, patches)
    patched_rules, layer_overrides = apply_patches(req.rules, patches)

    # 规则只解析一次：补丁不改选择器，基线 / 补丁后 / 留一世界共享 parsed
    parsed = RuleSet.parse(req.rules).parsed
    baseline = _solve_world(
        req.element, req.ancestors, req.layers, req.rules, parsed=parsed
    )
    patched = _solve_world(
        req.element,
        req.ancestors,
        req.layers,
        patched_rules,
        layer_overrides=layer_overrides,
        parsed=parsed,
    )
    loo_worlds = []
    for i in range(len(patches)):
        loo_rules, loo_overrides = apply_patches(
            req.rules, [p for j, p in enumerate(patches) if j != i]
        )
        loo_worlds.append(
            _solve_world(
                req.element,
                req.ancestors,
                req.layers,
                loo_rules,
                layer_overrides=loo_overrides,
                parsed=parsed,
            )
        )

    rev, _combined = _merge_edges(baseline, patched)

    changes = _normal_changes(baseline, patched, patched_rules, patches, loo_worlds, rev)
    changes += _custom_changes(baseline, patched, patched_rules, patches, loo_worlds, rev)
    changes.sort(key=lambda c: c.property)

    patched_sig = _world_signature(patched)
    shadowed_idx = [
        i for i, w in enumerate(loo_worlds) if _world_signature(w) == patched_sig
    ]
    shadowed = [
        ShadowedPatchOut(
            patch=PatchRefOut(
                index=i,
                rule_index=patches[i].rule_index,
                declaration_index=patches[i].declaration_index,
            ),
            reason=_shadowed_reason(patches[i], baseline, patched),
        )
        for i in shadowed_idx
    ]

    preview = ComputePreview(
        baseline=_snapshot(baseline),
        patched=_snapshot(patched),
        changes=changes,
        shadowed_patches=shadowed,
        applied_patches=[
            PatchRefOut(index=i, rule_index=p.rule_index, declaration_index=p.declaration_index)
            for i, p in enumerate(patches)
        ],
    )
    return ComputePreviewResponse(
        computed=baseline.computed,
        custom_properties=baseline.custom_properties,
        invalid=baseline.invalid,
        evidence=baseline.node.evidence,
        custom_property_trace=baseline.node.custom_property_trace,
        preview=preview,
    )
