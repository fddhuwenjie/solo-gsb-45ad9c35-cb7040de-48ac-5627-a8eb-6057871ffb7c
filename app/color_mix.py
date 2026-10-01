"""按 CSS Color 规则混合两种颜色（color-mix 语义）。

支持的输入：hex（#rgb / #rgba / #rrggbb / #rrggbbaa）、rgb()/rgba()、
hsl()/hsla()。支持的插值空间：srgb、srgb-linear、hsl、oklab、oklch；
极坐标空间（hsl / oklch）支持 shorter / longer / increasing /
decreasing 四种 hue 弧线。

流程（与 CSS Color 4/5 的 color-mix() 一致）：

1. 权重：两侧均省略各取 50；只省略一侧时补到 100；给定权重和先归一化，
   和小于 100 时结果 alpha 同比降低；负权重或权重和为 0 为 422。
2. 把两侧颜色统一转到目标插值空间（经 sRGB / linear-sRGB 中转）；
   无彩色一侧的缺失 hue 取另一侧 hue（两侧都缺失时取 0）。
3. 颜色通道以预乘 alpha 插值（hue 通道不预乘），hue 按指定弧线调整后
   线性插值，alpha 本身按权重线性插值。
4. 还原预乘，转回 sRGB，逐通道裁剪到 [0,1] 后输出 8 位 rgba。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .models import ColorMixRequest, ColorMixResponse

# ------------------------------------------------------------- 常量


# CSS Color 4：sRGB -> linear sRGB
def _srgb_to_linear(c: float) -> float:
    c = min(1.0, max(-0.0, c))  # 允许 [0,1] 之外的中间值时不裁剪，仅去 -0
    return c / 12.92 if c < 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


# linear sRGB（D65 适配白点）-> OKLab 用 LMS 矩阵（CSS Color 4 示例代码）
_LMS_FROM_LINEAR = (
    (0.4122214708, 0.5363325363, 0.0514459929),
    (0.2119034982, 0.6806995451, 0.1073969566),
    (0.0883024619, 0.2817188376, 0.6299787005),
)
_LAB_FROM_LMS = (
    (0.2104542553, 0.7936177850, -0.0040720468),
    (1.9779984951, -2.4285922050, 0.4505937099),
    (0.0259040371, 0.7827717662, -0.8086757660),
)
_LMS_FROM_LAB = (
    (1.0, 0.3963377774, 0.2158037573),
    (1.0, -0.1055613458, -0.0638541728),
    (1.0, -0.0894841775, -1.2914855480),
)
_LINEAR_FROM_LMS = (
    (4.0767416621, -3.3077115913, 0.2309699292),
    (-1.2684380046, 2.6097574011, -0.3413193965),
    (-0.0041960863, -0.7034186147, 1.7076147010),
)

_POLAR = {"hsl", "oklch"}
_EPS = 1e-8
# 无彩色判定阈值：低于此 chroma/saturation 视为 hue 缺失（吸收转换浮点噪声）
_ACHROMA_EPS = 1e-7


class ColorMixError(ValueError):
    """请求不满足混合前提（映射为 HTTP 422）。"""


# ------------------------------------------------------------- 颜色解析


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,8})$")
_MODERN_RE = re.compile(
    rf"^(?P<fn>[a-zA-Z]+)\(\s*(?P<args>.*?)\s*/\s*(?P<alpha>{_NUMBER}%?)\s*\)$",
    re.DOTALL,
)
_LEGACY_RE = re.compile(r"^([a-zA-Z]+)\(\s*(.*)\s*\)$", re.DOTALL)
_TOKEN_RE = re.compile(rf"^\s*({_NUMBER})(%?)\s*$")
_ANGLE_RE = re.compile(rf"^\s*({_NUMBER})(deg|rad|grad|turn)?\s*$", re.IGNORECASE)


@dataclass
class ParsedColor:
    """解析后的颜色，以 sRGB 通道（0..1，未裁剪）与 alpha 表示；
    missing 记录从极坐标语法带入的缺失分量（仅 hue 可能为 true）。
    """

    syntax: str
    raw: str
    srgb: tuple[float, float, float]
    alpha: float
    # hsl 输入中 hue 缺失（none，或 s=0 / l=0%|100% 导致的无彩色）
    hue_missing: bool = False
    hsl_hint: tuple[float, float, float] | None = None  # 原始 (h,s,l)，s/l ∈ [0,1]


def _split_commas(args: str) -> list[str]:
    return [part.strip() for part in args.split(",")]


def _parse_channel(token: str, what: str) -> float:
    """解析 rgb 的 r/g/b 通道：裸数字按 0..255、百分比按 0..1 归一。"""
    m = _TOKEN_RE.match(token)
    if m is None:
        raise ColorMixError(f"{what} 通道 {token!r} 不是合法数值或百分比")
    value = float(m.group(1))
    if not math.isfinite(value):
        raise ColorMixError(f"{what} 通道必须为有限数值")
    return value / 100.0 if m.group(2) else value / 255.0


def _parse_sl(token: str, what: str) -> float:
    """解析 hsl 的 s/l 通道：只接受百分比（裸 0 也按 CSS 接受为 0）。"""
    m = _TOKEN_RE.match(token)
    if m is None:
        raise ColorMixError(f"{what} 通道 {token!r} 不是合法百分比")
    value = float(m.group(1))
    if not math.isfinite(value):
        raise ColorMixError(f"{what} 通道必须为有限数值")
    if m.group(2):
        return value / 100.0
    if value == 0.0:  # CSS 允许 sat/light 写裸 0
        return 0.0
    raise ColorMixError(f"{what} 通道必须为百分比（如 {token.strip()}%）")


def _parse_angle(token: str) -> float:
    """解析 hue 角度，返回度数（不归一化）。"""
    m = _ANGLE_RE.match(token)
    if m is None:
        raise ColorMixError(f"hue 角度 {token!r} 不合法（应为数字 + deg/rad/grad/turn）")
    value = float(m.group(1))
    if not math.isfinite(value):
        raise ColorMixError("hue 角度必须为有限数值")
    unit = (m.group(2) or "deg").lower()
    if unit == "deg":
        return value
    if unit == "rad":
        return math.degrees(value)
    if unit == "grad":
        return value * 0.9
    return value * 360.0  # turn


def _parse_alpha(token: str) -> float:
    m = _TOKEN_RE.match(token)
    if m is None:
        raise ColorMixError(f"alpha {token!r} 不是合法数值或百分比")
    value = float(m.group(1))
    if not math.isfinite(value):
        raise ColorMixError("alpha 必须为有限数值")
    a = value / 100.0 if m.group(2) else value
    return min(1.0, max(0.0, a))


def _expand_hex(digits: str) -> tuple[int, int, int, int]:
    if len(digits) in (3, 4):
        r, g, b, a = (int(ch * 2, 16) for ch in digits) if len(digits) == 4 \
            else (*(int(ch * 2, 16) for ch in digits), 255)
        return r, g, b, a
    if len(digits) in (6, 8):
        r = int(digits[0:2], 16)
        g = int(digits[2:4], 16)
        b = int(digits[4:6], 16)
        a = int(digits[6:8], 16) if len(digits) == 8 else 255
        return r, g, b, a
    raise ColorMixError(
        f"hex 颜色长度必须为 3/4/6/8 位（当前 {len(digits)} 位）"
    )


def _hsl_to_srgb(h: float, s: float, l: float) -> tuple[float, float, float]:
    """CSS Color 4 的 HSL -> sRGB（h 为度数，s/l ∈ [0,1]）。"""
    h = (h % 360.0) / 360.0
    s = min(1.0, max(0.0, s))
    l = min(1.0, max(0.0, l))

    def hue_to_rgb(t: float) -> float:
        if t < 0.0:
            t += 1.0
        if t > 1.0:
            t -= 1.0
        if t < 1 / 6:
            return m1 + (m2 - m1) * 6 * t
        if t < 1 / 2:
            return m2
        if t < 2 / 3:
            return m1 + (m2 - m1) * (2 / 3 - t) * 6
        return m1

    if l <= 0.5:
        m2 = l * (s + 1.0)
    else:
        m2 = l + s - l * s
    m1 = l * 2.0 - m2
    if s == 0.0:
        return l, l, l
    return hue_to_rgb(h + 1 / 3), hue_to_rgb(h), hue_to_rgb(h - 1 / 3)


def parse_color(text: str) -> ParsedColor:
    if not isinstance(text, str) or not text.strip():
        raise ColorMixError("颜色不能为空，应为 hex / rgb() / hsl()")
    raw = text.strip()

    if raw.startswith("#"):
        m = _HEX_RE.match(raw)
        if m is None:
            raise ColorMixError(f"hex 颜色 {text!r} 含非十六进制字符或长度非法")
        r, g, b, a = _expand_hex(m.group(1))
        return ParsedColor("hex", raw, (r / 255, g / 255, b / 255), a / 255)

    modern = _MODERN_RE.match(raw.replace("\n", " "))
    legacy = _LEGACY_RE.match(raw.replace("\n", " "))
    if modern is not None and "," not in modern.group("args"):
        return _parse_modern(modern, raw)
    if legacy is not None and "," not in legacy.group(2) and "/" not in legacy.group(2):
        # 现代语法但省略 alpha：rgb(255 0 0) / hsl(0 100% 50%)
        return _parse_modern_channels(legacy.group(1).lower(), legacy.group(2), None, raw)
    if legacy is not None and "," in legacy.group(2):
        return _parse_legacy(legacy, raw)
    raise ColorMixError(
        f"颜色 {text!r} 不是合法的 hex / rgb() / rgba() / hsl() / hsla()"
    )


def _parse_modern(m: re.Match, raw: str) -> ParsedColor:
    fn = m.group("fn").lower()
    alpha = _parse_alpha(m.group("alpha"))
    args = m.group("args")
    if "," in args:
        raise ColorMixError("现代语法（斜杠 alpha）不允许使用逗号分隔通道")
    return _parse_modern_channels(fn, args, alpha, raw)


def _parse_modern_channels(fn: str, args: str, alpha: float | None, raw: str) -> ParsedColor:
    channels = [part.strip() for part in args.split() if part.strip()]
    if fn in ("rgb", "rgba"):
        if len(channels) != 3:
            raise ColorMixError(
                f"{fn}() 需要 3 个颜色通道与可选 alpha，收到 {len(channels)} 个"
            )
        if not all(_TOKEN_RE.match(a) for a in channels):
            raise ColorMixError(f"{fn}() 通道必须为数值或百分比")
        # 现代语法同样要求通道写法一致（全数字或全百分比）
        pct = [bool(_TOKEN_RE.match(a).group(2)) for a in channels]
        if not (all(pct) or not any(pct)):
            raise ColorMixError("rgb() 的 r/g/b 通道必须同为数值或同为百分比")
        r, g, b = (_parse_channel(a, "rgb") for a in channels)
        return ParsedColor("rgb", raw, (r, g, b), 1.0 if alpha is None else alpha)

    if fn in ("hsl", "hsla"):
        if len(channels) != 3:
            raise ColorMixError(
                f"{fn}() 需要 hue、saturation、lightness 与可选 alpha，"
                f"收到 {len(channels)} 个"
            )
        return _parse_hsl_parts(raw, channels, 1.0 if alpha is None else alpha, "hsl")

    raise ColorMixError(f"不支持的颜色函数 {fn}()（仅支持 rgb / hsl）")


def _parse_legacy(m: re.Match, raw: str) -> ParsedColor:
    fn = m.group(1).lower()
    parts = _split_commas(m.group(2))
    if fn in ("rgb", "rgba"):
        if len(parts) not in (3, 4):
            raise ColorMixError(f"{fn}() 参数个数错误（应为 3 个通道 + 可选 alpha）")
        alpha = _parse_alpha(parts[3]) if len(parts) == 4 else 1.0
        channels = parts[:3]
        # 旧式语法同样要求三个通道写法一致（全数值或全百分比）
        pct = [bool(_TOKEN_RE.match(t) and _TOKEN_RE.match(t).group(2)) for t in channels]
        if not all(_TOKEN_RE.match(t) for t in channels):
            raise ColorMixError("rgb() 的 r/g/b 通道必须同为数值或同为百分比")
        if not (all(pct) or not any(pct)):
            raise ColorMixError("rgb() 的 r/g/b 通道必须同为数值或同为百分比")
        r, g, b = (_parse_channel(t, "rgb") for t in channels)
        return ParsedColor("rgb", raw, (r, g, b), alpha)

    if fn in ("hsl", "hsla"):
        if len(parts) not in (3, 4):
            raise ColorMixError(f"{fn}() 参数个数错误（应为 hue/s/l + 可选 alpha）")
        alpha = _parse_alpha(parts[3]) if len(parts) == 4 else 1.0
        return _parse_hsl_parts(raw, parts[:3], alpha, "hsl")

    raise ColorMixError(f"不支持的颜色函数 {fn}()（仅支持 rgb / hsl）")


def _parse_hsl_parts(raw: str, parts: list[str], alpha: float, syntax: str) -> ParsedColor:
    hue_token = parts[0]
    hue_missing = hue_token.strip().lower() == "none"
    h = 0.0 if hue_missing else _parse_angle(hue_token)
    s = _parse_sl(parts[1], "saturation")
    l = _parse_sl(parts[2], "lightness")
    s_clamped, l_clamped = min(1.0, max(0.0, s)), min(1.0, max(0.0, l))
    # 无彩色（饱和度为 0，或亮度为 0%/100%）时 hue 视为缺失分量
    if s_clamped == 0.0 or l_clamped in (0.0, 1.0):
        hue_missing = True
    r, g, b = _hsl_to_srgb(h, s, l)
    return ParsedColor(
        syntax, raw, (r, g, b), alpha,
        hue_missing=hue_missing, hsl_hint=(h, s, l),
    )


# ------------------------------------------------------------- 空间转换


def _mat_vec(mat, vec):
    return tuple(sum(row[j] * vec[j] for j in range(3)) for row in mat)


def srgb_to_hsl(r: float, g: float, b: float):
    """CSS Color 4 的 sRGB -> HSL；返回 (h 度数或 None, s, l)。"""
    max_c, min_c = max(r, g, b), min(r, g, b)
    l = (max_c + min_c) * 0.5
    d = max_c - min_c
    if abs(d) < _ACHROMA_EPS:
        return None, 0.0, min(1.0, max(0.0, l))
    s = d / (1.0 - abs(2.0 * l - 1.0))
    if abs(max_c - r) < _EPS:
        h = ((g - b) / d) % 6.0
    elif abs(max_c - g) < _EPS:
        h = (b - r) / d + 2.0
    else:
        h = (r - g) / d + 4.0
    h *= 60.0
    return h, min(1.0, max(0.0, s)), min(1.0, max(0.0, l))


def srgb_to_oklab(r, g, b):
    lin = (_srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b))
    lms = tuple(max(0.0, v) ** (1 / 3) for v in _mat_vec(_LMS_FROM_LINEAR, lin))
    return _mat_vec(_LAB_FROM_LMS, lms)


def oklab_to_srgb(L, a, b):
    lms_ = _mat_vec(_LMS_FROM_LAB, (L, a, b))
    lms = tuple(v ** 3 for v in lms_)
    lin = _mat_vec(_LINEAR_FROM_LMS, lms)
    return tuple(_linear_to_srgb(v) for v in lin)


def convert(coords: list, space_from: str, space_to: str, missing: list[bool]):
    """在受支持空间之间转换；坐标以列表给出（缺失分量为 None）。

    返回 (coords, missing)。极坐标缺失 hue 与直角空间的无彩色互推。
    """
    if space_from == space_to:
        return list(coords), list(missing)

    # 统一先到 srgb（hsl 的缺失 hue 以 0 参与，结果灰度不受影响）
    if space_from == "srgb":
        r, g, b = coords
    elif space_from == "srgb-linear":
        r, g, b = (_linear_to_srgb(c) for c in coords)
    elif space_from == "hsl":
        h = coords[0] if coords[0] is not None else 0.0
        s, l = coords[1], coords[2]
        r, g, b = _hsl_to_srgb(h, s, l)
    elif space_from == "oklab":
        r, g, b = oklab_to_srgb(*coords)
    else:  # oklch
        L, c, h = coords
        angle = h if h is not None else 0.0
        rad = math.radians(angle)
        r, g, b = oklab_to_srgb(L, c * math.cos(rad), c * math.sin(rad))

    return _convert_from_srgb(r, g, b, space_to, missing)


def _convert_from_srgb(r, g, b, space_to, missing_in):
    if space_to == "srgb":
        return [r, g, b], list(missing_in)
    if space_to == "srgb-linear":
        return [_srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b)], list(missing_in)
    if space_to == "hsl":
        h, s, l = srgb_to_hsl(r, g, b)
        return [h, s, l], [h is None, False, False]
    if space_to == "oklab":
        return list(srgb_to_oklab(r, g, b)), [False, False, False]
    # oklch
    L, a, b2 = srgb_to_oklab(r, g, b)
    c = math.hypot(a, b2)
    h = None if c < _ACHROMA_EPS else (math.degrees(math.atan2(b2, a)) % 360.0)
    return [L, c, h], [False, False, h is None]


# ------------------------------------------------------------- 插值


def _adjust_hue(h1: float, h2: float, arc: str) -> tuple[float, float]:
    """按 CSS Color 4 规则调整终点 hue，返回 (调整后的 h1, h2)。"""
    diff = h2 - h1
    if arc == "shorter":
        if diff > 180.0:
            h1 += 360.0
        elif diff < -180.0:
            h2 += 360.0
    elif arc == "longer":
        if 0.0 < diff < 180.0:
            h1 += 360.0
        elif -180.0 < diff < 0.0:
            h2 += 360.0
        elif diff == 0.0:
            h2 += 360.0
    elif arc == "increasing":
        if diff < 0.0:
            h2 += 360.0
    else:  # decreasing
        if diff > 0.0:
            h1 += 360.0
    return h1, h2


def _hue_note(arc: str, diff: float) -> str:
    table = {
        "shorter": "shorter：取小于 180° 的弧，必要时把一侧 hue 加 360°",
        "longer": "longer：取大于 180° 的弧，两侧同 hue 时也绕整周",
        "increasing": "increasing：终点 hue 单调不减，必要时加 360°",
        "decreasing": "decreasing：终点 hue 单调不增，必要时把起点加 360°",
    }
    return f"{table[arc]}（原始 hue 差 {diff:.3f}°）"


def _hue_index(space: str) -> int | None:
    return 0 if space == "hsl" else 2 if space == "oklch" else None


def interpolate(coords_a, missing_a, alpha_a,
                coords_b, missing_b, alpha_b,
                wa, wb, space, arc):
    """预乘 alpha 插值。返回 dict，含调整后 hue、混合前后节点坐标。"""
    ca = list(coords_a)
    cb = list(coords_b)
    ma = list(missing_a)
    mb = list(missing_b)
    hi = _hue_index(space)

    hue_note = None
    if hi is not None:
        # 缺失 hue 取另一侧；两侧都缺失时取 0 且结果仍缺失
        if ca[hi] is None and cb[hi] is not None:
            ca[hi] = cb[hi]
            hue_note = "颜色 A 的 hue 缺失，取颜色 B 的 hue"
        elif cb[hi] is None and ca[hi] is not None:
            cb[hi] = ca[hi]
            hue_note = "颜色 B 的 hue 缺失，取颜色 A 的 hue"
        elif ca[hi] is None and cb[hi] is None:
            ca[hi] = cb[hi] = 0.0
            hue_note = "两侧 hue 均缺失，按 0° 处理且结果 hue 仍缺失"
        diff = cb[hi] - ca[hi]
        h1, h2 = _adjust_hue(ca[hi], cb[hi], arc)
        ca[hi], cb[hi] = h1, h2
        hue_note = (hue_note + "；" if hue_note else "") + _hue_note(arc, diff)

    # 预乘（hue 通道不预乘）
    pre_a = [c if (hi is not None and i == hi) else _mul(c, alpha_a)
             for i, c in enumerate(ca)]
    pre_b = [c if (hi is not None and i == hi) else _mul(c, alpha_b)
             for i, c in enumerate(cb)]

    mixed = [pre_a[i] * wa + pre_b[i] * wb for i in range(3)]
    alpha_mix = alpha_a * wa + alpha_b * wb

    # 还原预乘
    result = []
    for i, c in enumerate(mixed):
        if hi is not None and i == hi:
            result.append(c % 360.0)  # hue 归一化到 [0,360)
        elif alpha_mix > 0.0:
            result.append(c / alpha_mix)
        else:
            result.append(0.0)

    result_missing = [False, False, False]
    if hi is not None and ma[hi] and mb[hi]:
        result[hi] = None
        result_missing[hi] = True

    # 互补后两侧用于插值的分量都已就位（含 hue=0 的双侧缺失情形）
    pre_missing = [[False, False, False], [False, False, False]]

    return {
        "pre_a": pre_a,
        "pre_b": pre_b,
        "pre_missing": pre_missing,
        "mixed": mixed,
        "result": result,
        "missing": result_missing,
        "alpha": alpha_mix,
        "hue_note": hue_note,
    }


def _mul(c, alpha):
    return None if c is None else c * alpha


# ------------------------------------------------------------- 输出工具


def _r6(v):
    if v is None:
        return None
    if isinstance(v, float):
        if abs(v) < 5e-12:
            v = 0.0
        return round(v, 6)
    return v


def _node(space, coords, missing, alpha, premultiplied=False):
    return {
        "space": space,
        "coords": [_r6(c) for c in coords],
        "missing": list(missing),
        "alpha": _r6(alpha),
        "premultiplied": premultiplied,
    }


def _clamp01(v):
    return min(1.0, max(0.0, v))


def _to_byte(v):
    return int(math.floor(_clamp01(v) * 255.0 + 0.5))


def _alpha_css(a: float) -> str:
    text = f"{a:.6f}".rstrip("0").rstrip(".")
    return text or "0"


# ------------------------------------------------------------- 总入口


def mix_colors(req: ColorMixRequest) -> ColorMixResponse:
    # --- 权重 ---
    wa_given, wb_given = req.weight_a, req.weight_b
    trace: list[str] = []

    for name, w in (("weight_a", wa_given), ("weight_b", wb_given)):
        if w is not None:
            if not math.isfinite(w):
                raise ColorMixError(f"{name} 必须为有限数值")
            if w < 0.0:
                raise ColorMixError(f"{name} 不能为负（收到 {w}）")

    if wa_given is None and wb_given is None:
        pa, pb = 50.0, 50.0
        trace.append("两个权重均省略：各取 50%")
    elif wa_given is None:
        pa = 100.0 - wb_given
        pb = wb_given
        trace.append(f"weight_a 省略：补为 100% - {pb:g}% = {pa:g}%")
        if pa < 0.0:
            raise ColorMixError(f"weight_b={pb:g}% 超过 100%，补出的 weight_a 为负")
    elif wb_given is None:
        pa = wa_given
        pb = 100.0 - wa_given
        trace.append(f"weight_b 省略：补为 100% - {pa:g}% = {pb:g}%")
        if pb < 0.0:
            raise ColorMixError(f"weight_a={pa:g}% 超过 100%，补出的 weight_b 为负")
    else:
        pa, pb = wa_given, wb_given

    total = pa + pb
    if total <= 0.0:
        raise ColorMixError("权重和为 0，无法混合")
    wa, wb = pa / total, pb / total
    trace.append(
        f"给定权重 {pa:g}% / {pb:g}%（和 {total:g}%）按和归一化："
        f"{wa:.6f} / {wb:.6f}"
    )
    alpha_scale = min(1.0, total / 100.0)
    if alpha_scale < 1.0:
        trace.append(
            f"权重和 {total:g}% < 100%：结果 alpha 同比乘以 {alpha_scale:.6f}"
        )
    else:
        trace.append("权重和不少于 100%：alpha 不再缩放")

    # --- 解析 ---
    ca = parse_color(req.color_a)
    cb = parse_color(req.color_b)
    trace.append(f"解析颜色 A {ca.raw!r}（{ca.syntax}）：sRGB={ca.srgb}，alpha={ca.alpha:g}")
    trace.append(f"解析颜色 B {cb.raw!r}（{cb.syntax}）：sRGB={cb.srgb}，alpha={cb.alpha:g}")
    space = req.interpolation_space
    arc = req.hue_interpolation or "shorter"
    if space not in _POLAR and req.hue_interpolation is not None:
        raise ColorMixError(
            f"hue_interpolation 仅可用于极坐标空间（hsl / oklch），"
            f"当前插值空间为 {space}"
        )
    trace.append(f"插值空间：{space}；hue 弧线：{arc}")

    # --- 转换到目标空间（逐跳留痕）---
    # 转换图以 srgb 为根：srgb-linear / hsl 为子节点，oklab 挂在
    # srgb-linear 下，oklch 挂在 oklab 下。
    parent = {
        "srgb": None,
        "srgb-linear": "srgb",
        "hsl": "srgb",
        "oklab": "srgb-linear",
        "oklch": "oklab",
    }

    def hops_between(src: str, dst: str) -> list[str]:
        """src 逐跳转换到 dst 的目标节点序列（经最近公共祖先）。"""
        chain_src: list[str] = []
        node: str | None = src
        while node is not None:
            chain_src.append(node)
            node = parent[node]
        chain_dst: list[str] = []
        node = dst
        while node not in chain_src:
            chain_dst.append(node)
            node = parent[node]
        lca = node  # 最近公共祖先
        up = chain_src[1 : chain_src.index(lca) + 1]
        down = list(reversed(chain_dst))
        return up + down

    def path_to_target(parsed: ParsedColor, label: str):
        chain = []
        if parsed.syntax == "hsl" and parsed.hsl_hint is not None:
            h, s, l = parsed.hsl_hint
            coords = [None if parsed.hue_missing else h, s, l]
            missing = [parsed.hue_missing, False, False]
            chain.append(_node("hsl", coords, missing, parsed.alpha))
            cur_space = "hsl"
        else:
            coords = list(parsed.srgb)
            chain.append(_node("srgb", coords, [False] * 3, parsed.alpha))
            cur_space = "srgb"

        for nxt in hops_between(cur_space, space):
            coords, missing = convert(coords, cur_space, nxt,
                                      chain[-1]["missing"])
            chain.append(_node(nxt, coords, missing, parsed.alpha))
            cur_space = nxt
        trace.append(
            f"颜色 {label} 经 {' -> '.join(n['space'] for n in chain)} "
            f"转换到 {space}，坐标 {chain[-1]['coords']}"
        )
        return chain

    chain_a = path_to_target(ca, "A")
    chain_b = path_to_target(cb, "B")
    coords_a = chain_a[-1]["coords"]
    coords_b = chain_b[-1]["coords"]
    missing_a = chain_a[-1]["missing"]
    missing_b = chain_b[-1]["missing"]

    interp = interpolate(
        coords_a, missing_a, ca.alpha,
        coords_b, missing_b, cb.alpha,
        wa, wb, space, arc,
    )

    if interp["hue_note"]:
        trace.append("hue 处理：" + interp["hue_note"])
    trace.append(
        f"预乘 alpha 后按 {wa:.6f}/{wb:.6f} 线性插值："
        f"{[ _r6(x) for x in interp['pre_a']]} -> {[ _r6(x) for x in interp['pre_b']]}，"
        f"混合得 {[ _r6(x) for x in interp['mixed']]}"
        f"（alpha={interp['alpha']:g}）"
    )

    target_coords = interp["result"]
    target_missing = interp["missing"]
    target_alpha = interp["alpha"] * alpha_scale
    trace.append(
        f"还原预乘"
        + (" 并应用权重和 alpha 缩放" if alpha_scale < 1.0 else "")
        + f"：目标空间坐标 {[ _r6(x) for x in target_coords]}，"
        f"alpha={target_alpha:g}"
    )

    # --- 转回 sRGB ---
    back_order = hops_between(space, "srgb") if space != "srgb" else []
    back_chain = [_node(space, target_coords, target_missing, target_alpha)]
    cur_space = space
    cur_coords = [0.0 if c is None else c for c in target_coords]
    cur_missing = list(target_missing)
    for nxt in back_order:
        cur_coords, cur_missing = convert(cur_coords, cur_space, nxt, cur_missing)
        back_chain.append(_node(nxt, cur_coords, cur_missing, target_alpha))
        cur_space = nxt

    srgb_pre = back_chain[-1]["coords"]
    clipped = [not (0.0 <= c <= 1.0) for c in srgb_pre]
    srgb_final = [_clamp01(c) for c in srgb_pre]
    alpha_final = _clamp01(target_alpha)
    if any(clipped):
        trace.append(
            f"转回 sRGB 后 {[ _r6(x) for x in srgb_pre]} 超出色域，"
            f"逐通道裁剪为 {[ _r6(x) for x in srgb_final]}"
        )
    else:
        trace.append(f"转回 sRGB：{[ _r6(x) for x in srgb_final]}（无需裁剪）")

    r8, g8, b8 = (_to_byte(c) for c in srgb_final)
    css = f"rgba({r8}, {g8}, {b8}, {_alpha_css(alpha_final)})"

    # --- 组织响应 ---
    def input_out(parsed: ParsedColor, given, norm_w):
        return {
            "raw": parsed.raw,
            "syntax": parsed.syntax,
            "given_weight": None if given is None else _r6(given),
            "normalized_weight": _r6(norm_w),
            "srgb": [_r6(c) for c in parsed.srgb],
            "alpha": _r6(parsed.alpha),
        }

    start_node = _node(space, interp["pre_a"], interp["pre_missing"][0], ca.alpha,
                       premultiplied=True)
    end_node = _node(space, interp["pre_b"], interp["pre_missing"][1], cb.alpha,
                     premultiplied=True)
    mixed_node = _node(space, interp["mixed"], target_missing, interp["alpha"],
                       premultiplied=True)
    result_node = _node(space, target_coords, target_missing, target_alpha)

    conversions = chain_a + chain_b
    return ColorMixResponse(
        color_a=input_out(ca, wa_given, wa),
        color_b=input_out(cb, wb_given, wb),
        weights={
            "given": [None if wa_given is None else _r6(wa_given),
                      None if wb_given is None else _r6(wb_given)],
            "completed": [_r6(pa), _r6(pb)],
            "sum": _r6(total),
            "normalized": [_r6(wa), _r6(wb)],
            "alpha_scale": _r6(alpha_scale),
        },
        interpolation_space=space,
        hue_interpolation=arc if space in _POLAR else None,
        conversions=conversions,
        interpolation={
            "space": space,
            "hue_interpolation": arc if space in _POLAR else None,
            "start": start_node,
            "end": end_node,
            "hue_adjustment": (
                {"method": arc, "detail": interp["hue_note"]}
                if interp["hue_note"] else None
            ),
            "mixed": mixed_node,
            "result": result_node,
        },
        back_conversion=back_chain,
        clipped=clipped,
        rgba={
            "r": r8, "g": g8, "b": b8,
            "alpha": _r6(alpha_final),
            "css": css,
            "srgb_unclamped": [_r6(c) for c in srgb_pre],
            "srgb": [_r6(c) for c in srgb_final],
        },
        trace=trace,
    )
