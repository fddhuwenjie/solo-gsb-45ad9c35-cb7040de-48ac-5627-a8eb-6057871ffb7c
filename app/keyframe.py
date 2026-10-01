"""CSS 数值关键帧采样：在给定查询时刻对单个数值属性求值。

输入为覆盖偏移 0、1 且严格递增、单位一致的关键帧，按 Web Animations 语义：

1. 由 delay / duration / iteration_count 把查询时刻划分为 before / active / after；
2. active 内按 floor 定位轮次（内部边界归下一轮，active 终点取末轮终点），
   再由 direction（normal / reverse / alternate / alternate-reverse）求定向进度；
3. before / after 依 fill_mode 取首、末轮端点（backwards / forwards / both），
   fill 为 none 时除阶段外一律返回 None；
4. 按定向进度用 bisect 定位帧段（帧边界归入以该帧为起点的下一段），反解
   cubic-bezier 的 x 求缓动进度，最后在相邻关键帧间线性插值。
"""
from __future__ import annotations

import bisect
import math
import re

from .models import KeyframeSampleRequest, KeyframeSampleResponse

# CSS number 记号（含科学计数法），后接单位（空 / 字母 / %）
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_VALUE_RE = re.compile(rf"^\s*({_NUMBER})([a-zA-Z%]*)\s*$")
_EASING_RE = re.compile(
    rf"^cubic-bezier\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,"
    rf"\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)$"
)


class KeyframeValidationError(ValueError):
    """请求不满足采样前提（映射为 HTTP 422）。"""


# ------------------------------------------------------------- 输入校验


def _require_finite(value: float, what: str) -> float:
    if not math.isfinite(value):
        raise KeyframeValidationError(f"{what} 必须为有限数值，收到 {value!r}")
    return value


def _parse_keyframes(req: KeyframeSampleRequest) -> tuple[list[float], list[float], str]:
    """校验偏移与取值，返回 (偏移序列, 数值序列, 归一化单位)。"""
    frames = req.keyframes
    if len(frames) < 2:
        raise KeyframeValidationError("关键帧至少需要 2 个，且偏移必须覆盖 0 与 1")

    offsets = [_require_finite(f.offset, "关键帧偏移") for f in frames]
    for i in range(1, len(offsets)):
        if offsets[i] == offsets[i - 1]:
            raise KeyframeValidationError(
                f"关键帧偏移重复：第 {i - 1} 帧与第 {i} 帧均为 {offsets[i]}"
            )
        if offsets[i] < offsets[i - 1]:
            raise KeyframeValidationError(
                f"关键帧偏移必须严格递增：第 {i - 1} 帧偏移 {offsets[i - 1]} "
                f"大于第 {i} 帧偏移 {offsets[i]}"
            )
    if offsets[0] != 0.0 or offsets[-1] != 1.0:
        raise KeyframeValidationError(
            f"关键帧偏移必须覆盖 0 与 1（当前首偏移 {offsets[0]}、末偏移 {offsets[-1]}）"
        )

    numbers: list[float] = []
    unit: str | None = None
    for i, frame in enumerate(frames):
        match = _VALUE_RE.match(frame.value)
        if match is None:
            raise KeyframeValidationError(
                f"第 {i} 帧的值 {frame.value!r} 不是单个 CSS 数值"
                "（应为「数字 + 单位」，如 12px / 50% / 0）"
            )
        number = _require_finite(float(match.group(1)), f"第 {i} 帧的值")
        frame_unit = match.group(2).lower()
        if unit is None:
            unit = frame_unit
        elif frame_unit != unit:
            raise KeyframeValidationError(
                f"关键帧单位不一致：第 0 帧单位为 {_unit_label(unit)}，"
                f"第 {i} 帧为 {_unit_label(frame_unit)}"
            )
        numbers.append(number)

    return offsets, numbers, unit or ""


def _unit_label(unit: str) -> str:
    return f"{unit!r}" if unit else "（无单位）"


def _parse_easing(easing: str):
    """返回 None（linear）或 (x1, y1, x2, y2)。"""
    if easing == "linear":
        return None
    match = _EASING_RE.match(easing)
    if match is None:
        raise KeyframeValidationError(
            f"不支持的 easing {easing!r}：仅支持 linear 与 cubic-bezier(x1,y1,x2,y2)"
        )
    x1, y1, x2, y2 = (
        _require_finite(float(g), "cubic-bezier 控制点") for g in match.groups()
    )
    if not 0.0 <= x1 <= 1.0 or not 0.0 <= x2 <= 1.0:
        raise KeyframeValidationError(
            f"cubic-bezier 的 x 控制点必须位于 [0, 1] 内（收到 x1={x1}, x2={x2}）"
        )
    return x1, y1, x2, y2


# ------------------------------------------------------------- 贝塞尔缓动


def _bezier_axis(t: float, a: float, b: float) -> float:
    """三次贝塞尔在参数 t 处的单轴坐标，控制点为 (0,0)、(1,a)、(b,1)。"""
    mt = 1.0 - t
    return 3.0 * mt * mt * t * a + 3.0 * mt * t * t * b + t * t * t


def _eased_progress(p: float, bezier) -> float:
    """由 x 反解参数 t，再求 y；x 控制点在 [0,1] 内故 x(t) 单调，二分即可。"""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    x1, y1, x2, y2 = bezier
    lo, hi = 0.0, 1.0
    for _ in range(64):
        mid = (lo + hi) * 0.5
        if _bezier_axis(mid, x1, x2) < p:
            lo = mid
        else:
            hi = mid
    return _bezier_axis((lo + hi) * 0.5, y1, y2)


# ------------------------------------------------------------- 插值取值


def _format_value(value: float, unit: str) -> str:
    if abs(value) < 5e-11:  # 消掉浮点尾巴造成的 -0
        value = 0.0
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text + unit


def _sample_at(p, offsets, numbers, unit, bezier) -> tuple[int, float, float, str]:
    """在定向进度 p 处定位帧段并取值，返回 (段, 段内进度, 缓动进度, 取值)。"""
    idx = bisect.bisect_right(offsets, p) - 1
    if idx == len(offsets) - 1:  # p == 1：落在末帧，归末段终点
        idx -= 1
    o0, o1 = offsets[idx], offsets[idx + 1]
    segment_local = (p - o0) / (o1 - o0)
    # 浮点误差夹回 [0, 1]
    segment_local = min(1.0, max(0.0, segment_local))
    eased = segment_local if bezier is None else _eased_progress(segment_local, bezier)
    value = numbers[idx] + eased * (numbers[idx + 1] - numbers[idx])
    return idx, segment_local, eased, _format_value(value, unit)


def _directed_progress(local: float, iteration: int, direction: str) -> float:
    """轮次内原始进度 -> 按方向映射后的关键帧空间进度。"""
    if direction == "normal":
        return local
    if direction == "reverse":
        return 1.0 - local
    if direction == "alternate":
        return local if iteration % 2 == 0 else 1.0 - local
    # alternate-reverse：首轮反向，之后交替
    return 1.0 - local if iteration % 2 == 0 else local


# ----------------------------------------------------------------- 总入口


def sample_keyframes(req: KeyframeSampleRequest) -> KeyframeSampleResponse:
    duration = _require_finite(req.duration, "duration")
    if duration <= 0:
        raise KeyframeValidationError(f"duration 必须为正数，收到 {duration}")
    delay = _require_finite(req.delay, "delay")
    time = _require_finite(req.time, "查询时刻 time")
    if req.iteration_count <= 0:
        raise KeyframeValidationError(
            f"iteration_count 必须为正整数，收到 {req.iteration_count}"
        )

    offsets, numbers, unit = _parse_keyframes(req)
    bezier = _parse_easing(req.easing)

    active_start = delay
    active_end = delay + duration * req.iteration_count

    def empty(phase: str) -> KeyframeSampleResponse:
        return KeyframeSampleResponse(
            phase=phase, iteration=None, segment=None,
            local_progress=None, eased_progress=None, value=None,
        )

    def filled(phase: str, iteration: int, local: float) -> KeyframeSampleResponse:
        directed = _directed_progress(local, iteration, req.direction)
        segment, segment_local, eased, value = _sample_at(
            directed, offsets, numbers, unit, bezier
        )
        return KeyframeSampleResponse(
            phase=phase,
            iteration=iteration,
            segment=segment,
            local_progress=segment_local,
            eased_progress=eased,
            value=value,
        )

    # before 阶段：backwards / both 取首轮起点
    if time < active_start:
        if req.fill_mode in ("backwards", "both"):
            return filled("before", 0, 0.0)
        return empty("before")

    # after 阶段：forwards / both 取末轮终点
    if time > active_end:
        if req.fill_mode in ("forwards", "both"):
            return filled("after", req.iteration_count - 1, 1.0)
        return empty("after")

    # active：active_end 这一末边界取末轮终点；其余内部边界由 floor 归下一轮
    if time == active_end:
        return filled("active", req.iteration_count - 1, 1.0)
    position = (time - active_start) / duration
    iteration = math.floor(position)
    local = position - iteration
    return filled("active", iteration, local)
