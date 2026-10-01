"""选择器可满足性对齐：两条选择器（不提供 DOM 样本）能否命中同一目标元素。

支持的语法子集同 selector.py：标签 / * / #id / .class / [attr] / [attr=value]、
后代组合符（空格）与子组合符（">"）。

思路：把两条选择器的复合段各自看作自目标元素向根延伸的链，嵌入同一条
根 -> 目标 的见证节点链。每个深度至多承接两侧各一段；两段落在同一节点时，
其 tag / id / class / 属性约束必须可合并。">" 边要求父段恰在上一深度，
后代边允许跨越任意层。

后代边按 CSS 语义由"最近匹配祖先"锚定：某段新落到一个节点时，它与上一锚点
之间不能存在也匹配该段的节点（否则会被抢锚）。必要时插入一个不带任何约束
的空节点把两侧隔开。

搜索：DFS 自目标向根逐层枚举落位（含必要的空 blocker 层），在所有完整链中
取深度最小者，再取规范化 根 -> 目标 元素序列字典序最小者；最后用现有匹配器
selector_matches 复核两条选择器确实命中。

段下标一律按选择器书写顺序（0 起，最右段即目标段）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import ElementInfo, OverlapAlignmentRow, OverlapConflictOut, OverlapResponse
from .selector import Compound, parse_selector, selector_matches


@dataclass
class FieldConflict:
    """两段复合选择器合并时互斥的字段。"""

    field: str  # tag / id / attr:<name>
    value_a: str | None
    value_b: str | None


# ------------------------------------------------------------- 复合约束合并


def _concrete_attrs(comp: Compound) -> dict[str, str]:
    return {name: value for name, value in comp.attrs if value is not None}


def _merge_conflict(a: Compound, b: Compound) -> FieldConflict | None:
    """检查两段复合选择器能否同时约束同一节点；返回首个互斥字段。"""
    if a.tag and b.tag and a.tag != "*" and b.tag != "*" and a.tag != b.tag:
        return FieldConflict("tag", a.tag, b.tag)
    if a.id is not None and b.id is not None and a.id != b.id:
        return FieldConflict("id", a.id, b.id)
    aa, bb = _concrete_attrs(a), _concrete_attrs(b)
    for name in sorted(aa.keys() & bb.keys()):
        if aa[name] != bb[name]:
            return FieldConflict(f"attr:{name}", aa[name], bb[name])
    return None


def _self_conflict(comp: Compound) -> FieldConflict | None:
    """单侧单段内部互斥（当前语法下仅可能出现同名属性的两个不同定值）。"""
    seen: dict[str, str] = {}
    for name, value in comp.attrs:
        if value is None:
            continue
        if name in seen and seen[name] != value:
            return FieldConflict(f"attr:{name}", seen[name], value)
        seen[name] = value
    return None


def _field_label(field: str) -> str:
    if field == "tag":
        return "标签"
    if field == "id":
        return "id 属性"
    return f"属性 {field[5:]!r}"


# ------------------------------------------------------- 强制对齐冲突诊断


def _absorbed(placed: Compound, pending: Compound) -> bool:
    """弹性后代段 pending 是否会被 placed 落位形成的见证节点吸附占位。

    与搜索的"最近匹配祖先"一致：pending 若匹配 placed 的最小见证节点，
    匹配器就会把 pending 锚定到该节点，而无法再向更深层落位。
    """
    return pending.matches(ElementInfo(**_witness(placed, None)))


def _forced_conflict(comps_a, edges_a, comps_b, edges_b):
    """搜索失败时，定位被强制对齐到同一节点的首个互斥段对。

    自目标（深度 0）逐层向上模拟强制落位，与最终搜索的最近祖先匹配、
    段落位和 ">" 刚性传播保持一致：

    - ">" 刚性：本段朝目标的边为 ">" 且上一锚点恰在 depth-1 时，本段被
      压到 depth；
    - 最近祖先吸附：弹性后代段（" " 边）被对侧在本深度落位的见证节点
      匹配时，只能吸附到该深度占位，">" 刚性由此沿吸附占位逐层上传；
    - 两侧皆弹性时搜索可错层规避，不再产生强制对齐，模拟结束。

    同层两段复合约束互斥即为冲突证据。返回 (ia, ib, conflict, absorbs, depth)；
    absorbs 按层记录传播链上的吸附事件 (side, seg, depth, onto_seg)。
    """
    m, n = len(comps_a), len(comps_b)
    ia, ib = m - 2, n - 2  # 两侧下一段（自目标向上）
    da = db = 0  # 两侧最近一次落位的深度（锚点）
    depth = 0
    absorbs: list[tuple[str, int, int, int]] = []
    while ia >= 0 and ib >= 0:
        depth += 1
        place_a = edges_a[ia] == ">" and da == depth - 1
        place_b = edges_b[ib] == ">" and db == depth - 1
        # 最近祖先吸附：对侧本深度的落位节点若匹配本侧弹性段，本侧被吸附占位
        if (
            not place_a
            and place_b
            and edges_a[ia] == " "
            and _absorbed(comps_b[ib], comps_a[ia])
        ):
            place_a = True
            absorbs.append(("a", ia, depth, ib))
        if (
            not place_b
            and place_a
            and edges_b[ib] == " "
            and _absorbed(comps_a[ia], comps_b[ib])
        ):
            place_b = True
            absorbs.append(("b", ib, depth, ia))
        if not place_a and not place_b:
            return None  # 两侧皆弹性：可错层，无强制对齐
        if place_a and place_b:
            conflict = _merge_conflict(comps_a[ia], comps_b[ib])
            if conflict is not None:
                return ia, ib, conflict, absorbs, depth
        if place_a:
            da, ia = depth, ia - 1
        if place_b:
            db, ib = depth, ib - 1
    return None


# ------------------------------------------------------- 见证节点与规范化


def _witness(a: Compound | None, b: Compound | None) -> dict:
    """合并两段约束为见证节点，只保留选择器要求的字段；两侧皆 None 即空节点。"""
    tag: str | None = None
    ident: str | None = None
    classes: list[str] = []
    attrs: dict[str, str | None] = {}
    for comp in (a, b):
        if comp is None:
            continue
        if comp.tag and comp.tag != "*" and tag is None:
            tag = comp.tag
        if comp.id is not None:
            ident = comp.id
        for cls in comp.classes:
            if cls not in classes:
                classes.append(cls)
        for name, value in comp.attrs:
            # 存在性约束不得覆盖另一侧的定值
            attrs[name] = value if value is not None else attrs.get(name)
    el: dict = {}
    if tag is not None:
        el["tag"] = tag
    if ident is not None:
        el["id"] = ident
    if classes:
        el["classes"] = sorted(classes)
    if attrs:
        el["attributes"] = {
            name: ("" if attrs[name] is None else attrs[name]) for name in sorted(attrs)
        }
    return el


def _canonical(el: dict) -> tuple:
    """见证节点的规范化元组；越"空"字典序越小。"""
    return (
        el.get("tag", ""),
        el.get("id", ""),
        tuple(el.get("classes", [])),
        tuple(
            (name, el["attributes"][name])
            for name in sorted(el.get("attributes", {}))
        ),
    )


# ----------------------------------------------------------------- 搜索


def _successors(state, depth, prev_blank, comps_a, edges_a, comps_b, edges_b):
    """状态 (k, l, da, db) 在深度 depth 的候选转移。

    k/l：两侧已放置段数（目标段计 1）；da/db：两侧最近一次放置的深度。
    prev_blank：上一深度是否为空节点（禁止连续空层）。
    返回 [(placed_a, placed_b, succ, el, blank), ...]。
    """
    k, l, da, db = state
    m, n = len(comps_a), len(comps_b)
    edge_a = edges_a[m - k - 1] if k < m else None
    edge_b = edges_b[n - l - 1] if l < n else None
    # ">" 边要求父段恰在上一深度；后代边允许任意更深位置
    can_a = k < m and (edge_a == " " or da == depth - 1)
    can_b = l < n and (edge_b == " " or db == depth - 1)

    out: list[tuple] = []
    if can_a:
        out.append((1, 0, (k + 1, l, depth, db),
                    _witness(comps_a[m - k - 1], None), False))
    if can_b:
        out.append((0, 1, (k, l + 1, da, depth),
                    _witness(None, comps_b[n - l - 1]), False))
    if (
        can_a
        and can_b
        and _merge_conflict(comps_a[m - k - 1], comps_b[n - l - 1]) is None
    ):
        out.append((1, 1, (k + 1, l + 1, depth, depth),
                    _witness(comps_a[m - k - 1], comps_b[n - l - 1]), False))
    # 空层（纯后代间隙，节点不带任何约束）：只在至少一侧仍有段、且上一深度
    # 非空时才允许（连续空层对匹配没有作用）。
    if (k < m or l < n) and not prev_blank:
        out.append((0, 0, (k, l, da, db), _witness(None, None), True))
    return out


def _greedy_safe(placed_a, placed_b, state, depth, node_by_depth,
                 comps_a, comps_b) -> bool:
    """增量"最近匹配祖先"校验。

    刚放置的段位于 depth。匹配器为后代边选择"最近匹配祖先"：本段与上一同侧
    锚点（da/db）之间若存在也匹配本段的节点，就会被抢锚。开区间 (da, depth)
    内本侧没有自己的锚点（相邻锚点之间），但可能有对侧落位形成的节点。
    ">" 转移保证上一锚点恰为 depth-1，区间为空，天然安全。
    """
    k, l, da, db = state
    if placed_a and k + 1 >= 2:
        idx = len(comps_a) - (k + 1)
        comp = comps_a[idx]
        for d in range(da + 1, depth):
            info = node_by_depth.get(d)
            if info is not None and comp.matches(ElementInfo(**info)):
                return False
    if placed_b and l + 1 >= 2:
        idx = len(comps_b) - (l + 1)
        comp = comps_b[idx]
        for d in range(db + 1, depth):
            info = node_by_depth.get(d)
            if info is not None and comp.matches(ElementInfo(**info)):
                return False
    return True


def _search(comps_a, edges_a, comps_b, edges_b):
    """返回节点数最少、规范化 根 -> 目标 序列字典序最小的 matcher-valid 链。

    DFS 自深度 0（目标）向根枚举每层落位，维护当前最优完整链：深度小者优先，
    同深度按节点序列字典序（根节点起）。每个候选放置经 _greedy_safe 增量校验
    （与匹配器"最近匹配祖先"一致），终态再由 selector_matches 整体复核。
    返回 rows（深度 D -> 0，根 -> 目标）；无完整链时返回 None。
    """
    m, n = len(comps_a), len(comps_b)
    target_el = _witness(comps_a[-1], comps_b[-1])
    # best = (D, canonical_sequence, choices)；D 最小优先，再取字典序
    best: dict = {"d": None}
    # 深度上界：两侧所有段完全错开（m+n-2 层），再为每个后代间隙预留至多一个
    # 空 blocker（边数上界 (m-1)+(n-1)），足够构造 matcher-valid 见证链。
    max_depth = 2 * (m + n) - 3

    def dfs(state, depth, prev_blank, node_by_depth, choices, path_canon):
        k, l, _da, _db = state
        if k == m and l == n:
            cand = (depth - 1, path_canon, tuple(choices))
            if (
                best["d"] is None
                or cand[0] < best["d"]
                or (cand[0] == best["d"] and cand[1] < best["path"])
            ):
                best["d"], best["path"], best["choices"] = cand
            return
        if depth > max_depth:
            return
        branches = _successors(state, depth, prev_blank,
                               comps_a, edges_a, comps_b, edges_b)
        branches.sort(key=lambda br: (_canonical(br[3]), -(br[0] + br[1])))
        for placed_a, placed_b, succ, el, blank in branches:
            node_by_depth[depth] = el
            if placed_a or placed_b:
                if not _greedy_safe(placed_a, placed_b, state, depth,
                                    node_by_depth, comps_a, comps_b):
                    node_by_depth.pop(depth, None)
                    continue
            # path_canon 是根->目标；新节点在根侧，故前置拼接
            dfs(
                succ, depth + 1, blank, node_by_depth,
                choices + [(depth, placed_a, placed_b, el)],
                (_canonical(el),) + path_canon,
            )
            node_by_depth.pop(depth, None)

    dfs((1, 1, 0, 0), 1, False, {0: target_el}, [],
        (_canonical(target_el),))

    if best["d"] is None:
        return None
    rows = _materialize(list(best["choices"]),
                        comps_a, edges_a, comps_b, edges_b)
    if not _chain_matches(rows, comps_a, edges_a, comps_b, edges_b):
        return None
    return rows


def _materialize(choices, comps_a, edges_a, comps_b, edges_b):
    """把落位记录物化成 深度 D -> 0 的行（根 -> 目标）。

    choices：按深度递增，每项 (depth, placed_a, placed_b, el)。
    段原序下标由"该深度是本侧自目标起第几个锚点"反推：维护每侧已放计数。
    """
    m, n = len(comps_a), len(comps_b)
    count_a, count_b = 1, 1  # 目标段（原序 m-1 / n-1）已占
    raw: dict[int, dict] = {}
    for depth, placed_a, placed_b, el in choices:
        sa = m - 1 - count_a if placed_a else None
        sb = n - 1 - count_b if placed_b else None
        count_a += placed_a
        count_b += placed_b
        edge_a = edges_a[sa] if sa is not None and sa < m - 1 else None
        edge_b = edges_b[sb] if sb is not None and sb < n - 1 else None
        raw[depth] = {"depth": depth, "sa": sa, "sb": sb,
                      "edge_a": edge_a, "edge_b": edge_b, "el": el}
    rows = [raw[d] for d in sorted(raw, reverse=True)]
    rows.append(
        {
            "depth": 0,
            "sa": m - 1,
            "sb": n - 1,
            "edge_a": None,
            "edge_b": None,
            "el": _witness(comps_a[-1], comps_b[-1]),
        }
    )
    return rows  # 深度 D -> 0，即 根 -> 目标


def _chain_matches(rows, comps_a, edges_a, comps_b, edges_b) -> bool:
    """用现有匹配器复核见证链被两条选择器同时命中。"""
    target = ElementInfo(**rows[-1]["el"])
    ancestors = [ElementInfo(**r["el"]) for r in rows[:-1]]  # 根 -> 父
    return (
        selector_matches(comps_a, edges_a, target, ancestors)
        and selector_matches(comps_b, edges_b, target, ancestors)
    )


# ------------------------------------------------------------------ 总入口


def selector_overlap(selector_a: str, selector_b: str) -> OverlapResponse:
    comps_a, edges_a = parse_selector(selector_a)
    comps_b, edges_b = parse_selector(selector_b)

    def unsatisfied(conflict: OverlapConflictOut) -> OverlapResponse:
        return OverlapResponse(
            satisfiable=False,
            selector_a=selector_a,
            selector_b=selector_b,
            conflict=conflict,
        )

    # 1) 单侧段内自相矛盾
    for side, comps in (("a", comps_a), ("b", comps_b)):
        for idx, comp in enumerate(comps):
            conflict = _self_conflict(comp)
            if conflict is not None:
                label = _field_label(conflict.field)
                which = "selector_a" if side == "a" else "selector_b"
                return unsatisfied(
                    OverlapConflictOut(
                        kind="compound",
                        segment_a=idx if side == "a" else None,
                        segment_b=idx if side == "b" else None,
                        field=conflict.field,
                        value_a=conflict.value_a if side == "a" else None,
                        value_b=conflict.value_b if side == "a" else None,
                        reason=(
                            f"{which} 第 {idx} 段在同一节点上要求{label}同时为 "
                            f"{conflict.value_a!r} 与 {conflict.value_b!r}，"
                            f"复合约束自相矛盾"
                        ),
                    )
                )

    # 2) 目标段必然对齐到深度 0：互斥即复合约束冲突
    target_conflict = _merge_conflict(comps_a[-1], comps_b[-1])
    if target_conflict is not None:
        ia, ib = len(comps_a) - 1, len(comps_b) - 1
        label = _field_label(target_conflict.field)
        return unsatisfied(
            OverlapConflictOut(
                kind="compound",
                segment_a=ia,
                segment_b=ib,
                field=target_conflict.field,
                value_a=target_conflict.value_a,
                value_b=target_conflict.value_b,
                reason=(
                    f"目标段 selector_a[{ia}] 要求{label}为 "
                    f"{target_conflict.value_a!r}，selector_b[{ib}] 要求{label}为 "
                    f"{target_conflict.value_b!r}；命中同一元素时两侧目标段必须同时"
                    f"约束该元素，复合约束冲突"
                ),
            )
        )

    # 3) 搜索节点数最少、字典序唯一的 matcher-valid 见证链
    rows = _search(comps_a, edges_a, comps_b, edges_b)
    if rows is None:
        forced = _forced_conflict(comps_a, edges_a, comps_b, edges_b)
        if forced is not None:
            ia, ib, conflict, absorbs, depth = forced
            label = _field_label(conflict.field)
            if absorbs:
                # 传播型：最近祖先吸附使弹性后代段占位，">" 刚性沿占位逐层上传
                steps = "；".join(
                    f"selector_{side}[{seg}] 为弹性后代段，最近祖先吸附使其在 "
                    f"selector_{'b' if side == 'a' else 'a'}[{onto}] 所在的"
                    f"深度 {d} 占位"
                    for side, seg, d, onto in absorbs
                )
                reason = (
                    f"{steps}；子组合符 '>' 随之把 selector_a[{ia}] 与 "
                    f"selector_b[{ib}] 推到同一层（深度 {depth}）强制对齐，"
                    f"但前者要求{label}为 {conflict.value_a!r}、后者要求为 "
                    f"{conflict.value_b!r}，子关系强制对齐冲突（无后代间隙可规避）"
                )
            else:
                reason = (
                    f"自目标起两侧连续的子组合符 '>' 把 selector_a[{ia}] 与 "
                    f"selector_b[{ib}] 强制对齐到同一节点，但前者要求{label}为 "
                    f"{conflict.value_a!r}、后者要求为 {conflict.value_b!r}，"
                    f"子关系强制对齐冲突（无后代间隙可规避）"
                )
            return unsatisfied(
                OverlapConflictOut(
                    kind="child",
                    segment_a=ia,
                    segment_b=ib,
                    field=conflict.field,
                    value_a=conflict.value_a,
                    value_b=conflict.value_b,
                    reason=reason,
                )
            )
        # 理论不可达：目标段相容时，搜索失败必由强制对齐冲突导致
        return unsatisfied(
            OverlapConflictOut(
                kind="child",
                reason="子组合符强制对齐导致两条选择器无法共享祖先链",
            )
        )

    # 4) 组装响应（_search 已用现有匹配器复核两条选择器均命中）
    alignment = [
        OverlapAlignmentRow(
            depth=r["depth"],
            segment_a=r["sa"],
            segment_b=r["sb"],
            combinator_a=r["edge_a"],
            combinator_b=r["edge_b"],
            element=r["el"],
        )
        for r in rows
    ]
    element = rows[-1]["el"]
    ancestors = [r["el"] for r in rows[:-1]]  # 根 -> 父

    return OverlapResponse(
        satisfiable=True,
        selector_a=selector_a,
        selector_b=selector_b,
        element=element,
        ancestors=ancestors,
        alignment=alignment,
    )
