"""样式变更预演：在既有元素链 / 层 / 规则上叠加定位式补丁，对比基线与补丁后
的计算结果，给出逐项差异、因果轨迹与未生效（shadowed）补丁。

补丁只允许修改既有声明的 value / important / layer，不增删声明、不改属性名
与选择器，因此两个世界中参与层叠的属性集合不变，胜出声明可以用
(rule_index, declaration_index) 唯一比对。

基线、补丁后与留一法复算都建立在整树分析内核（analysis.py）之上：规则骨架
只解析一次（RuleSet），补丁后的声明表经 with_declarations 复用骨架；逐项
差异与 shadowed 判定使用与 /stylesheet-audit、/dom-impact 完全相同的节点
状态签名（胜出声明定位 + 状态 + 取值 + 失效原因），因此“胜出者改变但取值
相同”的补丁不会被误判为未生效。

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

from .analysis import NodeAnalysis, PropSignature, RuleSet, SolveOptions, solve_chain
from .models import (
    ComputePreview,
    ComputePreviewResponse,
    ComputeRequest,
    PatchRefOut,
    PreviewSnapshotOut,
    PropertyChangeOut,
    ShadowedPatchOut,
)
from .variables import VarEvent


class PatchError(ValueError):
    """补丁定位或内容非法（整次请求 422，不产生部分结果）。"""


# 自定义属性在该侧链上不存在时的占位签名（普通属性在预演两世界中集合不变，
# 不需要 absent 占位）
_MISSING_SIG = PropSignature(winner=None, status="missing", value=None, reason=None)


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


def _patched_view(base_ruleset: RuleSet, rules, patches):
    """应用一组补丁，返回 (补丁后规则集, 求解变体)；规则骨架复用不重新解析。"""
    patched_rules, layer_overrides = apply_patches(rules, patches)
    patched_ruleset = base_ruleset.with_declarations(
        tuple(tuple(r.declarations) for r in patched_rules)
    )
    return patched_ruleset, SolveOptions(layer_overrides=layer_overrides)


# ---------- 快照与签名 ----------


def _snapshot(world: NodeAnalysis) -> PreviewSnapshotOut:
    return PreviewSnapshotOut(
        computed=world.computed(),
        custom_properties=world.custom_properties(),
        invalid=world.invalid(),
    )


def _normal_sig(world: NodeAnalysis, prop: str) -> PropSignature:
    return world.normals[prop].signature()


def _custom_sig(world: NodeAnalysis, name: str) -> PropSignature:
    state = world.customs.get(name)
    return state.signature() if state else _MISSING_SIG


# ---------- 因果依赖图 ----------


def _decl_node(winner) -> tuple:
    return ("decl", winner.rule_index, winner.decl_index)


def _custom_node(depth: int, name: str) -> tuple:
    return ("custom_at", depth, name)


def _origin_of_custom(world: NodeAnalysis, depth: int, name: str):
    """name 在 depth 层解析值的声明来源：取不晚于 depth 的最近一层本元素胜出声明。"""
    for d in range(depth, -1, -1):
        if name in world.depths[d].winners:
            return _custom_node(d, name)
    return None


def _is_cyclic_at(world: NodeAnalysis, depth: int, name: str) -> bool:
    return name in world.depths[depth].cyclic


def _invalid_word(world: NodeAnalysis, depth: int, ref_status: str, name: str) -> str:
    if ref_status == "missing":
        return "未定义"
    if _is_cyclic_at(world, depth, name):
        return "因循环依赖而失效"
    return "已失效"


def _event_edges(
    world: NodeAnalysis,
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


def _build_edges(world: NodeAnalysis) -> list[tuple[tuple, tuple, str]]:
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
            self_loop = name in dinfo.deps.get(name, ())
            if self_loop:
                label = (
                    f"{dinfo.label} 上 {name} 自引用 var({name})，"
                    "形成单节点循环依赖而失效"
                )
                node = _custom_node(d, name)
                edges.append((node, node, label))
            for dep in dinfo.deps.get(name, ()):
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
        for name, status in dinfo.resolved.items():
            if name in dinfo.cyclic or name not in dinfo.winners:
                continue
            if status[0] != "invalid":
                continue
            for ev in dinfo.events.get(name, ()):
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
    for name in world.customs:
        if name in target_winners:
            continue
        origin = _origin_of_custom(world, target_depth - 1, name)
        if origin is not None:
            d = origin[1]
            edges.append(
                (
                    _custom_node(target_depth, name),
                    origin,
                    f"目标元素未声明 {name}，沿祖先链继承自 {world.depths[d].label}",
                )
            )

    # 普通属性：胜出声明边 + var() 取值边
    for prop, info in world.normals.items():
        u = ("normal", prop)
        edges.append((u, _decl_node(info.winner), f"该声明为 {prop} 在目标元素的层叠胜出者"))
        _event_edges(world, target_depth, u, info.var_events, edges)

    return edges


def _merge_edges(baseline: NodeAnalysis, patched: NodeAnalysis):
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


def _node_context(node: tuple, world: NodeAnalysis) -> str:
    if node[0] == "normal":
        return f"最终普通属性 {node[1]}"
    if node[0] == "custom_at":
        _, d, name = node
        return f"{world.depths[d].label} 上的自定义属性 {name}"
    _, ri, di = node
    prop = world.declaration(ri, di).property
    return f"规则 {ri} 第 {di} 条声明（{prop}）"


def _build_trail(rev, baseline, patched, patch, goal) -> list[str]:
    start = ("decl", patch.rule_index, patch.declaration_index)
    used = _search_trail(rev, start, goal)
    if used is None:
        return []
    before_decl = baseline.declaration(patch.rule_index, patch.declaration_index)
    after_decl = patched.declaration(patch.rule_index, patch.declaration_index)
    before_layer = baseline.effective_layer(patch.rule_index, patch.declaration_index)
    after_layer = patched.effective_layer(patch.rule_index, patch.declaration_index)
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


def _same_winner_kind(patched: NodeAnalysis, loc, patches):
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
    decl = patched.declaration(loc[0], loc[1])
    if (
        patch_at_loc is not None
        and "value" in patch_at_loc.model_fields_set
        and "var(" not in decl.value
    ):
        return "edited"
    return "var_changed"


def _normal_changes(
    baseline: NodeAnalysis,
    patched: NodeAnalysis,
    patches,
    loo_worlds,
    rev,
) -> list[PropertyChangeOut]:
    changes = []
    for prop in sorted(baseline.normals):
        b, a = baseline.normals[prop], patched.normals[prop]
        sig_b, sig_a = b.signature(), a.signature()
        # 与审计 / DOM 变更同一签名：胜出定位 + 状态 + 取值 + 失效原因
        if sig_b == sig_a:
            continue
        winner_changed = sig_b.winner != sig_a.winner
        if winner_changed:
            kind = "winner_changed"
        else:
            kind = _same_winner_kind(patched, sig_a.winner, patches)
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
                before_reason=b.reason,
                after_reason=a.reason,
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
    baseline: NodeAnalysis,
    patched: NodeAnalysis,
    patches,
    loo_worlds,
    rev,
) -> list[PropertyChangeOut]:
    changes = []
    names = sorted(set(baseline.customs) | set(patched.customs))
    target_depth = len(baseline.chain) - 1
    for name in names:
        sig_b = _custom_sig(baseline, name)
        sig_a = _custom_sig(patched, name)
        if sig_b == sig_a:
            continue
        state_b = baseline.customs.get(name)
        state_a = patched.customs.get(name)
        wb = state_b.winner if state_b else None
        wa = state_a.winner if state_a else None
        winner_changed = sig_b.winner != sig_a.winner
        if winner_changed:
            kind = "winner_changed"
        elif sig_a.winner is None:
            # 纯继承的自定义属性，本元素无胜出声明，变化必来自变量链
            kind = "var_changed"
        else:
            kind = _same_winner_kind(patched, sig_a.winner, patches)
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
                status_changed=sig_b.status != sig_a.status,
                before_status=sig_b.status,
                after_status=sig_a.status,
                before_value=sig_b.value,
                after_value=sig_a.value,
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


def _declaration_is_candidate(world: NodeAnalysis, ri: int, di: int, is_custom: bool) -> bool:
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


def _declaration_wins_relevant(world: NodeAnalysis, ri: int, di: int, is_custom: bool) -> bool:
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


def _shadowed_reason(patch, baseline: NodeAnalysis, patched: NodeAnalysis) -> str:
    ri, di = patch.rule_index, patch.declaration_index
    is_custom = patched.declaration(ri, di).property.startswith("--")
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

    # 规则骨架只解析一次；基线 / 补丁后 / 留一法复算共享同一 RuleSet 骨架
    base_ruleset = RuleSet.build(req.rules, req.layers)
    baseline = solve_chain(base_ruleset, req.element, req.ancestors)

    patched_ruleset, patched_options = _patched_view(base_ruleset, req.rules, patches)
    patched = solve_chain(patched_ruleset, req.element, req.ancestors, patched_options)

    loo_worlds = []
    for i in range(len(patches)):
        loo_ruleset, loo_options = _patched_view(
            base_ruleset, req.rules, [p for j, p in enumerate(patches) if j != i]
        )
        loo_worlds.append(
            solve_chain(loo_ruleset, req.element, req.ancestors, loo_options)
        )

    rev, _combined = _merge_edges(baseline, patched)

    changes = _normal_changes(baseline, patched, patches, loo_worlds, rev)
    changes += _custom_changes(baseline, patched, patches, loo_worlds, rev)
    changes.sort(key=lambda c: c.property)

    patched_sig = patched.signature()
    shadowed_idx = [
        i for i, w in enumerate(loo_worlds) if w.signature() == patched_sig
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
        computed=baseline.computed(),
        custom_properties=baseline.custom_properties(),
        invalid=baseline.invalid(),
        evidence=baseline.evidence(),
        custom_property_trace=baseline.custom_trace(),
        preview=preview,
    )
