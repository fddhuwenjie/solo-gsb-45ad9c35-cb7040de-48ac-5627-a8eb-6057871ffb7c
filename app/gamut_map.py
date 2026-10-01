"""OKLCH -> sRGB 色域映射（gamut mapping）。

设计系统常以 OKLCH 描述颜色，但 sRGB 屏幕无法显示超出其色域的
高色度颜色；把越界通道逐通道硬裁剪会改变通道间比例、造成色相
偏移。本模块按「固定 L / h / alpha，仅降低 C」的策略把颜色安全
落入 sRGB 色域：

1. 解析 CSS oklch()：L 为 0..1 数值或百分比（100% = 1），C 为
   数值或百分比（100% = 0.4），H 为角度（deg/rad/grad/turn，
   裸数字按 deg）或 none，可带斜杠 alpha（0..1 或百分比）；
2. 原色已在 sRGB 色域内时原样返回（不做任何映射）；
3. 否则固定 L、h 与 alpha，在 [0, 原 C] 上二分查找仍处于色域内
   的最大 C，区间宽度不大于 tolerance 时结束，逐轮记录上下界。

校验（均映射为 HTTP 422）：非法语法、非有限值、L / alpha 越出
[0, 1]、负 C、tolerance 不在 [1e-7, 1e-3]。
"""
from __future__ import annotations

import math
import re

from .color_mix import (
    _ANGLE_RE,
    _TOKEN_RE,
    _alpha_css,
    _clamp01,
    _to_byte,
    oklab_to_srgb,
)
from .models import GamutMapRequest, GamutMapResponse, GamutMapRoundOut, OklchOut

# C 通道百分比基准：CSS Color 4 规定 oklch 的 100% 对应 0.4
_C_PERCENT_REF = 0.4
# 色域判定容差：吸收 OKLab <-> sRGB 矩阵往返的浮点噪声
_GAMUT_EPS = 1e-9

_TOLERANCE_MIN = 1e-7
_TOLERANCE_MAX = 1e-3

_OKLCH_RE = re.compile(r"^oklch\(\s*(?P<body>.*?)\s*\)$", re.IGNORECASE | re.DOTALL)


class GamutMapError(ValueError):
    """请求不满足色域映射前提（映射为 HTTP 422）。"""


# ------------------------------------------------------------- oklch() 解析


def _parse_lightness(token: str) -> float:
    """L 通道：0..1 数值或百分比（100% = 1），越界为 422。"""
    m = _TOKEN_RE.match(token)
    if m is None:
        raise GamutMapError(f"L 通道 {token!r} 不是合法数值或百分比")
    value = float(m.group(1))
    if not math.isfinite(value):
        raise GamutMapError("L 通道必须为有限数值")
    lightness = value / 100.0 if m.group(2) else value
    if not 0.0 <= lightness <= 1.0:
        raise GamutMapError(f"L 越出 [0, 1]（当前 {lightness:g}）")
    return lightness


def _parse_chroma(token: str) -> float:
    """C 通道：数值或百分比（100% = 0.4），负值为 422。"""
    m = _TOKEN_RE.match(token)
    if m is None:
        raise GamutMapError(f"C 通道 {token!r} 不是合法数值或百分比")
    value = float(m.group(1))
    if not math.isfinite(value):
        raise GamutMapError("C 通道必须为有限数值")
    chroma = value * _C_PERCENT_REF / 100.0 if m.group(2) else value
    if chroma < 0.0:
        raise GamutMapError(f"C 不能为负（当前 {chroma:g}）")
    return chroma


def _parse_hue(token: str) -> float | None:
    """H 通道：角度（deg/rad/grad/turn，裸数字按 deg）或 none。

    返回归一化到 [0, 360) 的度数；none 时返回 None。
    """
    if token.strip().lower() == "none":
        return None
    m = _ANGLE_RE.match(token)
    if m is None:
        raise GamutMapError(
            f"hue 角度 {token!r} 不合法（应为数字 + deg/rad/grad/turn，或 none）"
        )
    value = float(m.group(1))
    if not math.isfinite(value):
        raise GamutMapError("hue 角度必须为有限数值")
    unit = (m.group(2) or "deg").lower()
    if unit == "rad":
        value = math.degrees(value)
    elif unit == "grad":
        value = value * 0.9
    elif unit == "turn":
        value = value * 360.0
    return value % 360.0


def _parse_alpha(token: str) -> float:
    """斜杠后的 alpha：0..1 数值或百分比，越界为 422（不裁剪）。"""
    m = _TOKEN_RE.match(token.strip())
    if m is None:
        raise GamutMapError(f"alpha {token!r} 不是合法数值或百分比")
    value = float(m.group(1))
    if not math.isfinite(value):
        raise GamutMapError("alpha 必须为有限数值")
    alpha = value / 100.0 if m.group(2) else value
    if not 0.0 <= alpha <= 1.0:
        raise GamutMapError(f"alpha 越出 [0, 1]（当前 {alpha:g}）")
    return alpha


def parse_oklch(text: str) -> tuple[float, float, float | None, float, str]:
    """解析 CSS oklch()，返回 (L, C, h 或 None, alpha, 原始串)。"""
    if not isinstance(text, str) or not text.strip():
        raise GamutMapError("颜色不能为空，应为 oklch(L C H / alpha)")
    raw = text.strip()
    m = _OKLCH_RE.match(raw.replace("\n", " "))
    if m is None:
        raise GamutMapError(f"颜色 {text!r} 不是合法的 oklch() 函数")
    body = m.group("body")
    if "," in body:
        raise GamutMapError("oklch() 使用空格分隔通道（不允许逗号）")
    parts = body.split("/")
    if len(parts) > 2:
        raise GamutMapError("oklch() 最多允许一个斜杠 alpha")
    channels = parts[0].split()
    if len(channels) != 3:
        raise GamutMapError(
            f"oklch() 需要 L、C、H 三个通道与可选 alpha，收到 {len(channels)} 个通道"
        )
    alpha = 1.0 if len(parts) == 1 else _parse_alpha(parts[1])
    lightness = _parse_lightness(channels[0])
    chroma = _parse_chroma(channels[1])
    hue = _parse_hue(channels[2])
    return lightness, chroma, hue, alpha, raw


# ------------------------------------------------------------- 色域判定


def _oklch_to_srgb(L: float, C: float, h: float) -> tuple[float, float, float]:
    """OKLCH -> sRGB（不裁剪）。

    C 极大（约 1e102 以上）时 LMS 立方会溢出 float64；此时把 a、b 按
    同比缩小到可计算范围（保持 hue 不变），结果仍是远超色域的有限值，
    色域判定结论不变。
    """
    rad = math.radians(h)
    a, b = C * math.cos(rad), C * math.sin(rad)
    try:
        return oklab_to_srgb(L, a, b)
    except OverflowError:
        scale = 1e100 / max(abs(a), abs(b))
        return oklab_to_srgb(L, a * scale, b * scale)


def _in_srgb_gamut(rgb) -> bool:
    """三个 sRGB 通道（未裁剪）都在 [0, 1] 内（含浮点噪声容差）。"""
    return all(-_GAMUT_EPS <= c <= 1.0 + _GAMUT_EPS for c in rgb)


# ------------------------------------------------------------- 输出工具


def _r9(v):
    """保留 9 位小数：tolerance 可小至 1e-7，6 位不足以分辨相邻轮次。"""
    if v is None:
        return None
    if abs(v) < 5e-14:
        v = 0.0
    return round(v, 9)


def _oklch_node(L: float, C: float, h: float | None, alpha: float) -> OklchOut:
    # C=0 时 hue 无意义，按 none（null）输出
    hue = None if h is None or C == 0.0 else _r9(h)
    return OklchOut(l=_r9(L), c=_r9(C), h=hue, alpha=_r9(alpha))


# ------------------------------------------------------------- 总入口


def gamut_map(req: GamutMapRequest) -> GamutMapResponse:
    tolerance = req.tolerance
    if isinstance(tolerance, bool) or not math.isfinite(tolerance):
        raise GamutMapError("tolerance 必须为有限数值")
    if not _TOLERANCE_MIN <= tolerance <= _TOLERANCE_MAX:
        raise GamutMapError(
            f"tolerance 需在 [{_TOLERANCE_MIN:g}, {_TOLERANCE_MAX:g}] 内"
            f"（当前 {tolerance:g}）"
        )

    L, C, h, alpha, raw = parse_oklch(req.color)
    h_eff = h if h is not None else 0.0  # 缺失 hue 按 0° 参与转换
    trace: list[str] = [
        f"解析 {raw!r}：L={L:g}，C={C:g}，"
        f"h={('none（缺失，按 0° 参与转换）' if h is None else f'{h:g}°')}，"
        f"alpha={alpha:g}",
        f"二分终止条件：区间宽度 ≤ tolerance {tolerance:g}",
    ]

    original = _oklch_node(L, C, h, alpha)
    srgb_unclamped = _oklch_to_srgb(L, C, h_eff)

    if _in_srgb_gamut(srgb_unclamped):
        trace.append(
            f"原色转 sRGB 得 {[_r9(c) for c in srgb_unclamped]}，"
            "各通道均在 [0, 1] 内：已在色域内，原样返回"
        )
        mapped = original
        c_mapped = C
        rounds: list[GamutMapRoundOut] = []
        was_mapped = False
    else:
        out = [_r9(c) for c in srgb_unclamped]
        trace.append(
            f"原色转 sRGB 得 {out}，存在越界通道："
            f"固定 L={L:g}、h、alpha={alpha:g}，在 [0, {C:g}] 上二分最大色域内 C"
        )
        lo, hi = 0.0, C
        rounds = []
        while hi - lo > tolerance:
            mid = (lo + hi) / 2.0
            ok = _in_srgb_gamut(_oklch_to_srgb(L, mid, h_eff))
            rounds.append(
                GamutMapRoundOut(lo=_r9(lo), hi=_r9(hi), mid=_r9(mid), in_gamut=ok)
            )
            if ok:
                lo = mid
            else:
                hi = mid
        c_mapped = lo
        was_mapped = True
        trace.append(
            f"二分 {len(rounds)} 轮后区间宽度 {hi - lo:g} ≤ {tolerance:g}："
            f"C 从 {C:g} 降到 {c_mapped:g}"
        )
        mapped = _oklch_node(L, c_mapped, h, alpha)

    chroma_loss = C - c_mapped
    srgb_final = [_clamp01(c) for c in _oklch_to_srgb(L, c_mapped, h_eff)]
    if was_mapped:
        trace.append(
            f"映射后 OKLCH：L={L:g}，C={c_mapped:g}，h 与 alpha 不变；"
            f"色度损失 {chroma_loss:g}；最终 sRGB={[_r9(c) for c in srgb_final]}"
        )

    r8, g8, b8 = (_to_byte(c) for c in srgb_final)
    alpha_final = _clamp01(alpha)
    return GamutMapResponse(
        input=raw,
        tolerance=tolerance,
        original=original,
        mapped=mapped,
        was_mapped=was_mapped,
        chroma_loss=_r9(chroma_loss),
        srgb_unclamped=[_r9(c) for c in srgb_unclamped],
        srgb=[_r9(c) for c in srgb_final],
        rgba={
            "r": r8,
            "g": g8,
            "b": b8,
            "alpha": _r9(alpha_final),
            "css": f"rgba({r8}, {g8}, {b8}, {_alpha_css(alpha_final)})",
        },
        rounds=rounds,
        trace=trace,
    )
