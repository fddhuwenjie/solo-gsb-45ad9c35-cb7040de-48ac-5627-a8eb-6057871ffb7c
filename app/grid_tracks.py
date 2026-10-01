"""单行 CSS Grid 列宽求解。

按 CSS Grid sizing algorithm 的简化版本求解一组列轨道的最终尺寸：

1. 解析固定轨道：px / 百分比直接换算为 px 基础尺寸（base）；
   minmax(下限, 上限) 取区间 [base, cap]，上限 fr 表示该轨道可按 fr 增长
   （cap 为 None，即无增长上限）；纯 fr 轨道 base=0；
2. 处理项目最小内容需求：项目按跨度从短到长、同跨度按输入顺序排列；
   对每个跨多列的项目，当前跨列尺寸之和不足 min_content 时，把缺口
   等额分给跨列中的可增长轨道（尺寸 < cap）；触及 cap 的份额回流给
   其余可增长轨道迭代重分，直至满足或全部触顶（缺口计入 overflow）；
3. 分配剩余空间给 fr 轨道：剩余空间除以 fr 总和得每 fr 份额，份额低于
   某轨道基础尺寸时先把该轨道按基础尺寸固定，再在其余 fr 轨道中重算。

负值、非法跨度、越界项目与错误轨道语法均抛 GridTracksError（映射 422）。
"""
from __future__ import annotations

import math
import re

from .models import GridTracksRequest, GridTracksResponse

# CSS number 记号（含科学计数法），后接单位
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_TOKEN_RE = re.compile(rf"^\s*({_NUMBER})(px|%|fr)?\s*$", re.IGNORECASE)
_MINMAX_RE = re.compile(r"^\s*minmax\(\s*(.+?)\s*,\s*(.+?)\s*\)\s*$", re.IGNORECASE)

# 浮点比较容差，用于触顶 / 份额不足判定
_EPS = 1e-9


class GridTracksError(ValueError):
    """请求不满足求解前提（映射为 HTTP 422）。"""


# ------------------------------------------------------------- 解析与校验


def _finite(value: float, what: str) -> float:
    if not math.isfinite(value):
        raise GridTracksError(f"{what} 必须为有限数值，收到 {value!r}")
    return value


def _parse_token(text: str, what: str, allow_fr: bool) -> tuple[float, str]:
    """解析「数字 + px|%|fr」记号，返回 (数值, 归一化单位)。单位必须显式给出。"""
    match = _TOKEN_RE.match(text)
    if match is None or match.group(2) is None:
        raise GridTracksError(
            f"{what} {text.strip()!r} 不是合法轨道尺寸（数字后必须显式给出 "
            + ("px / % / fr" if allow_fr else "px / %")
            + " 单位）"
        )
    number = _finite(float(match.group(1)), f"{what} {text.strip()!r} 的数值")
    if number < 0:
        raise GridTracksError(f"{what} {text.strip()!r} 不能为负值")
    unit = match.group(2).lower()
    if unit == "fr" and not allow_fr:
        raise GridTracksError(f"{what} {text.strip()!r}：minmax() 下限不允许使用 fr")
    return number, unit


def _parse_track(text: str, index: int, container_width: float) -> dict:
    """解析一条列轨道定义，返回 {kind, base, cap, fr}。

    cap 为 None 表示无增长上限（纯 fr 轨道或 minmax 上限为 fr）。
    """
    if not isinstance(text, str) or not text.strip():
        raise GridTracksError(f"第 {index} 条轨道定义为空或不是字符串")

    minmax_match = _MINMAX_RE.match(text)
    if minmax_match is not None:
        low_raw, high_raw = minmax_match.group(1), minmax_match.group(2)
        # 只允许恰好一个逗号；嵌套函数 / 多余逗号均为语法错误
        if "," in low_raw or "," in high_raw or "(" in low_raw or "(" in high_raw:
            raise GridTracksError(f"第 {index} 条轨道 {text.strip()!r} 语法错误")
        low_value, low_unit = _parse_token(
            low_raw, f"第 {index} 条轨道的 minmax 下限", allow_fr=False
        )
        high_value, high_unit = _parse_token(
            high_raw, f"第 {index} 条轨道的 minmax 上限", allow_fr=True
        )
        base = low_value if low_unit == "px" else low_value * container_width / 100.0
        if high_unit == "fr":
            if high_value <= 0:
                raise GridTracksError(
                    f"第 {index} 条轨道 {text.strip()!r} 的 fr 上限必须为正数"
                )
            return {"kind": "minmax", "base": base, "cap": None, "fr": high_value}
        cap = (
            high_value
            if high_unit == "px"
            else high_value * container_width / 100.0
        )
        # CSS 语义：minmax 下限大于上限时下限收缩为上限，区间退化为固定值
        if cap < base:
            base = cap
        return {"kind": "minmax", "base": base, "cap": cap, "fr": 0.0}

    # 带 minmax 前缀但整体不匹配，统一按语法错误处理
    if text.strip().lower().startswith("minmax"):
        raise GridTracksError(f"第 {index} 条轨道 {text.strip()!r} 语法错误")

    value, unit = _parse_token(text, f"第 {index} 条轨道", allow_fr=True)
    if unit == "fr":
        if value <= 0:
            # 0fr 退化为固定 0 轨道：base/cap 均为 0，不参与 fr 分配
            return {"kind": "fr", "base": 0.0, "cap": 0.0, "fr": 0.0}
        return {"kind": "fr", "base": 0.0, "cap": None, "fr": value}
    size = value if unit == "px" else value * container_width / 100.0
    return {"kind": unit, "base": size, "cap": size, "fr": 0.0}


# ------------------------------------------------------------- 数值工具


def _r(value: float | None) -> float | None:
    """输出修约：消除浮点尾巴，None（无上限）原样透传。"""
    if value is None:
        return None
    if abs(value) < _EPS:
        return 0.0
    return round(value, 6)


# ------------------------------------------------------------- 求解


def solve_grid_tracks(req: GridTracksRequest) -> GridTracksResponse:
    container_width = _finite(req.container_width, "container_width")
    if container_width < 0:
        raise GridTracksError(f"container_width 不能为负值，收到 {container_width}")
    gap = _finite(req.gap, "gap")
    if gap < 0:
        raise GridTracksError(f"gap 不能为负值，收到 {gap}")
    if not req.columns:
        raise GridTracksError("至少需要定义一条列轨道")

    n = len(req.columns)
    tracks = [_parse_track(text, i, container_width) for i, text in enumerate(req.columns)]
    base = [t["base"] for t in tracks]
    cap: list[float | None] = [t["cap"] for t in tracks]
    fr = [t["fr"] for t in tracks]
    size = list(base)

    total_gap = gap * (n - 1)
    if total_gap > container_width + _EPS:
        raise GridTracksError(
            f"gap 合计 {_r(total_gap)}px 超出容器宽度 {_r(container_width)}px"
        )

    # 校验项目：start >= 1、span >= 1、不越界、min_content 非负有限
    items = []
    for idx, item in enumerate(req.items):
        if isinstance(item.start, bool) or not isinstance(item.start, int):
            raise GridTracksError(f"第 {idx} 个项目的 start 必须为整数")
        if isinstance(item.span, bool) or not isinstance(item.span, int):
            raise GridTracksError(f"第 {idx} 个项目的 span 必须为整数")
        if item.start < 1:
            raise GridTracksError(
                f"第 {idx} 个项目的 start={item.start} 非法，列从 1 起编号"
            )
        if item.span < 1:
            raise GridTracksError(
                f"第 {idx} 个项目的 span={item.span} 非法，必须为正整数"
            )
        end = item.start + item.span - 1
        if end > n:
            raise GridTracksError(
                f"第 {idx} 个项目跨越第 {item.start}–{end} 列，"
                f"超出共 {n} 列的轨道范围"
            )
        min_content = _finite(item.min_content, f"第 {idx} 个项目的 min_content")
        if min_content < 0:
            raise GridTracksError(f"第 {idx} 个项目的 min_content 不能为负值")
        items.append(
            {
                "index": idx,
                "name": item.name,
                "start": item.start,
                "span": item.span,
                "min_content": min_content,
            }
        )

    # 处理顺序：跨度从短到长，同跨度按输入顺序
    order = sorted(range(len(items)), key=lambda i: (items[i]["span"], i))

    trace: list[dict] = []
    overflow: list[dict] = []

    # ---------- 阶段一：项目最小内容缺口 ----------
    for i in order:
        item = items[i]
        columns = list(range(item["start"] - 1, item["start"] - 1 + item["span"]))
        rounds: list[dict] = []
        candidates = set(columns)

        def available_sum() -> float:
            return sum(size[c] for c in columns)

        if available_sum() + _EPS >= item["min_content"]:
            rounds.append(
                {
                    "shortfall": _r(max(0.0, item["min_content"] - available_sum())),
                    "growable": columns,
                    "share": 0.0,
                    "capped": [],
                    "redistributed": [],
                }
            )
        else:
            while True:
                growable = [
                    c
                    for c in columns
                    if c in candidates and (cap[c] is None or size[c] < cap[c] - _EPS)
                ]
                if not growable:
                    break
                shortfall = item["min_content"] - available_sum()
                if shortfall <= _EPS:
                    break
                start_size = {c: size[c] for c in growable}
                share = shortfall / len(growable)
                capped_now = [
                    c
                    for c in growable
                    if cap[c] is not None and share > cap[c] - size[c] + _EPS
                ]
                if not capped_now:
                    # 无人触顶：等额分摊，缺口消除
                    for c in growable:
                        size[c] += share
                    rounds.append(
                        {
                            "shortfall": _r(shortfall),
                            "growable": growable,
                            "share": _r(share),
                            "capped": [],
                            "redistributed": [],
                        }
                    )
                    break
                # 触顶轨道补足到 cap，其份额回流给其余候选轨道迭代重分
                for c in capped_now:
                    size[c] = cap[c]  # type: ignore[assignment]
                    candidates.discard(c)
                absorbed = sum(cap[c] - start_size[c] for c in capped_now)
                rounds.append(
                    {
                        "shortfall": _r(shortfall),
                        "growable": growable,
                        "share": _r(share),
                        "capped": sorted(capped_now),
                        "redistributed": [
                            c for c in columns if c in candidates and c not in capped_now
                        ],
                    }
                )
                if absorbed <= _EPS:  # 防御：触顶轨道已无空间时避免死循环
                    break

        item_overflow = max(0.0, item["min_content"] - available_sum())
        if item_overflow > _EPS:
            overflow.append(
                {
                    "item_index": i,
                    "name": item["name"],
                    "start": item["start"],
                    "span": item["span"],
                    "min_content": _r(item["min_content"]),
                    "available": _r(available_sum()),
                    "gap": _r(item_overflow),
                }
            )
        trace.append(
            {
                "phase": "min_content",
                "item_index": i,
                "name": item["name"],
                "start": item["start"],
                "span": item["span"],
                "min_content": _r(item["min_content"]),
                "rounds": rounds,
                "unmet": _r(item_overflow),
            }
        )

    # ---------- 阶段二：剩余空间按 fr 迭代分配 ----------
    # 按 CSS Grid 「Expand flexible tracks」：剩余空间只扣除间距与非弹性
    # 轨道（px / % / 无 fr 的 minmax，尺寸在阶段一已定）；每 fr 份额
    # used_fr = free_space / fr 总和，弹性轨道取 fr * used_fr（不叠加基础
    # 尺寸）。若某活动轨道的份额低于其基础尺寸（可能被 min_content 撑大），
    # 把它按基础尺寸固定、移出弹性集合，剩余空间扣除其基础尺寸后重算。
    fr_base = {c: size[c] for c in range(n) if fr[c] > 0}
    inflexible_size = sum(size[c] for c in range(n) if fr[c] <= 0)
    initial_free_space = container_width - total_gap - inflexible_size

    active = set(fr_base)
    fr_rounds: list[dict] = []
    while active:
        free_space = container_width - total_gap - inflexible_size - sum(
            fr_base[c] for c in fr_base if c not in active
        )
        fr_sum = sum(fr[c] for c in active)
        fr_size = free_space / fr_sum
        targets = {c: fr[c] * fr_size for c in active}
        # 比例份额使轨道达不到基础尺寸：先固定该轨道，再对其余轨道重算
        violating = sorted(c for c in active if targets[c] < fr_base[c] - _EPS)
        fr_rounds.append(
            {
                "free_space": _r(free_space),
                "active": sorted(active),
                "fr_sum": _r(fr_sum),
                "fr_size": _r(fr_size),
                "frozen": violating,
                "targets": {str(c): _r(targets[c]) for c in sorted(active)},
            }
        )
        if not violating:
            for c in active:
                size[c] = targets[c]
            break
        # 固定轨道（可能一次固定多个）后继续迭代；剩余空间已为负时全部固定
        for c in violating:
            size[c] = fr_base[c]
            active.discard(c)

    trace.append(
        {
            "phase": "fr",
            "initial_free_space": _r(initial_free_space),
            "rounds": fr_rounds,
        }
    )

    used_width = sum(size) + total_gap
    return GridTracksResponse(
        container_width=_r(container_width),
        gap=_r(gap),
        total_gap=_r(total_gap),
        processing_order=order,
        tracks=[
            {
                "index": c,
                "definition": req.columns[c],
                "kind": tracks[c]["kind"],
                "base": _r(base[c]),
                "cap": _r(cap[c]),
                "fr": _r(fr[c]),
                "final_size": _r(size[c]),
            }
            for c in range(n)
        ],
        used_width=_r(used_width),
        container_overflow=_r(max(0.0, used_width - container_width)),
        overflow=overflow,
        trace=trace,
    )
