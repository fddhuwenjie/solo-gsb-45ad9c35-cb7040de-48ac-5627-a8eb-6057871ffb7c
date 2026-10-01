"""端到端测试：POST /gamut-map。"""
import math

import pytest
from fastapi.testclient import TestClient

from app.gamut_map import GamutMapError, gamut_map, _in_srgb_gamut, _oklch_to_srgb
from app.main import app
from app.models import GamutMapRequest

client = TestClient(app)


def gmap(body):
    resp = client.post("/gamut-map", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def expect422(body):
    resp = client.post("/gamut-map", json=body)
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def expect422_raw(body):
    # httpx 的 json= 拒绝 NaN/Infinity，用原始 content 发送（Python json 默认放行）
    import json

    resp = client.post(
        "/gamut-map",
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def engine(**over):
    base = dict(color="oklch(0.7 0.3 30)", tolerance=1e-5)
    base.update(over)
    return gamut_map(GamutMapRequest(**base))


# ---------- 色域内：原样返回 ----------


def test_in_gamut_returned_as_is():
    data = gmap({"color": "oklch(0.5 0.05 250)"})
    assert data["was_mapped"] is False
    assert data["chroma_loss"] == 0.0
    assert data["rounds"] == []
    assert data["mapped"] == data["original"]
    assert data["original"] == {"l": 0.5, "c": 0.05, "h": 250.0, "alpha": 1.0}
    # 未裁剪与最终 sRGB 一致且都在 [0,1]
    assert data["srgb"] == data["srgb_unclamped"]
    assert all(0.0 <= c <= 1.0 for c in data["srgb"])


def test_achromatic_c_zero_hue_is_none():
    data = gmap({"color": "oklch(0.5 0 30)"})
    assert data["original"]["h"] is None  # C=0 时 hue 为 none
    assert data["mapped"]["h"] is None
    assert data["was_mapped"] is False
    r, g, b = data["srgb"]
    assert r == g == b  # 灰色


def test_black_and_white_in_gamut():
    for color in ("oklch(0 0 0)", "oklch(1 0 0)", "oklch(100% 0 none)"):
        data = gmap({"color": color})
        assert data["was_mapped"] is False


# ---------- 超色域：二分降 C ----------


def test_out_of_gamut_maps_chroma_down():
    data = gmap({"color": "oklch(70% 0.3 30 / 0.8)", "tolerance": 1e-5})
    assert data["was_mapped"] is True
    # 固定 L、h、alpha，仅 C 降低
    assert data["mapped"]["l"] == data["original"]["l"] == 0.7
    assert data["mapped"]["h"] == data["original"]["h"] == 30.0
    assert data["mapped"]["alpha"] == data["original"]["alpha"] == 0.8
    assert 0.0 < data["mapped"]["c"] < 0.3
    assert data["chroma_loss"] == pytest.approx(0.3 - data["mapped"]["c"], abs=1e-9)
    # 未裁剪 sRGB 越界，最终 sRGB 在色域内
    assert any(c < 0.0 or c > 1.0 for c in data["srgb_unclamped"])
    assert all(0.0 <= c <= 1.0 for c in data["srgb"])
    # 最终 sRGB 与逐通道硬裁剪不同（未硬裁剪：hue 保持 30°）
    assert data["srgb"][0] <= 1.0 and data["srgb"][1] > 0.0


def test_mapped_c_is_max_in_gamut_within_tolerance():
    tol = 1e-5
    data = gmap({"color": "oklch(0.7 0.3 30)", "tolerance": tol})
    c = data["mapped"]["c"]
    # 映射结果本身在色域内；再抬高不超过 tolerance 即出色域
    assert _in_srgb_gamut(_oklch_to_srgb(0.7, c, 30.0))
    assert not _in_srgb_gamut(_oklch_to_srgb(0.7, c + 2 * tol, 30.0))


def test_bisection_rounds_recorded():
    tol = 1e-4
    data = gmap({"color": "oklch(0.7 0.3 30)", "tolerance": tol})
    rounds = data["rounds"]
    assert len(rounds) >= 1
    # 首轮区间即 [0, 原 C]
    assert rounds[0]["lo"] == 0.0
    assert rounds[0]["hi"] == pytest.approx(0.3)
    # 每轮 mid 为区间中点，区间随 in_gamut 单调收窄
    lo, hi = 0.0, 0.3
    for rnd in rounds:
        assert rnd["lo"] == pytest.approx(lo, abs=1e-9)
        assert rnd["hi"] == pytest.approx(hi, abs=1e-9)
        assert rnd["mid"] == pytest.approx((lo + hi) / 2.0, abs=1e-9)
        if rnd["in_gamut"]:
            lo = rnd["mid"]
        else:
            hi = rnd["mid"]
    # 终止时区间宽度不大于 tolerance，答案为最终下界
    assert hi - lo <= tol
    assert data["mapped"]["c"] == pytest.approx(lo, abs=1e-9)


def test_tolerance_changes_round_count():
    coarse = gmap({"color": "oklch(0.7 0.3 30)", "tolerance": 1e-3})
    fine = gmap({"color": "oklch(0.7 0.3 30)", "tolerance": 1e-7})
    assert len(coarse["rounds"]) < len(fine["rounds"])
    # 更小的 tolerance 得到不低于粗结果的 C（都是最大色域内 C 的下界逼近）
    assert fine["mapped"]["c"] >= coarse["mapped"]["c"] - 1e-3


def test_alpha_preserved_and_in_rgba():
    data = gmap({"color": "oklch(0.7 0.3 30 / 0.5)"})
    assert data["original"]["alpha"] == 0.5
    assert data["mapped"]["alpha"] == 0.5
    assert data["rgba"]["alpha"] == 0.5
    assert data["rgba"]["css"].endswith(", 0.5)")


def test_extreme_lightness_maps_to_zero_chroma():
    # L=0（纯黑）带任意色度都越界：二分把 C 压到接近 0，最终为黑色
    data = gmap({"color": "oklch(0 0.3 30)", "tolerance": 1e-3})
    assert data["was_mapped"] is True
    assert data["mapped"]["c"] <= 1e-3
    assert data["srgb"] == [0.0, 0.0, 0.0]
    assert data["rgba"]["css"] == "rgba(0, 0, 0, 1)"


# ---------- 语法与通道解析 ----------


@pytest.mark.parametrize(
    "color,l,c,h,alpha",
    [
        ("oklch(0.5 0.1 30)", 0.5, 0.1, 30.0, 1.0),
        ("oklch(50% 25% 30)", 0.5, 0.1, 30.0, 1.0),  # C 百分比：100% = 0.4
        ("oklch(0.5 0.1 30 / 0.5)", 0.5, 0.1, 30.0, 0.5),
        ("oklch(0.5 0.1 30 / 50%)", 0.5, 0.1, 30.0, 0.5),
        ("oklch(0.5 0.1 0.5turn)", 0.5, 0.1, 180.0, 1.0),
        ("oklch(0.5 0.1 200grad)", 0.5, 0.1, 180.0, 1.0),
        ("oklch(0.5 0.1 3.141592653589793rad)", 0.5, 0.1, 180.0, 1.0),
        ("oklch(0.5 0.1 30deg)", 0.5, 0.1, 30.0, 1.0),
        ("OKLCH(0.5 0.1 30)", 0.5, 0.1, 30.0, 1.0),
    ],
)
def test_parse_syntaxes(color, l, c, h, alpha):
    data = gmap({"color": color})
    assert data["original"]["l"] == pytest.approx(l, abs=1e-9)
    assert data["original"]["c"] == pytest.approx(c, abs=1e-9)
    assert data["original"]["h"] == pytest.approx(h, abs=1e-6)
    assert data["original"]["alpha"] == pytest.approx(alpha, abs=1e-9)


def test_hue_normalized_to_0_360():
    assert gmap({"color": "oklch(0.5 0.1 390)"})["original"]["h"] == pytest.approx(30.0)
    assert gmap({"color": "oklch(0.5 0.1 -30)"})["original"]["h"] == pytest.approx(330.0)


def test_hue_none_accepted():
    data = gmap({"color": "oklch(0.5 0.1 none)"})
    assert data["original"]["h"] is None
    assert data["mapped"]["h"] is None


def test_input_echoed():
    data = gmap({"color": "  oklch(0.5 0.1 30)  "})
    assert data["input"] == "oklch(0.5 0.1 30)"


# ---------- 422 校验 ----------


@pytest.mark.parametrize(
    "color",
    [
        "",
        "red",
        "#ff0000",
        "rgb(255 0 0)",
        "oklch()",
        "oklch(0.5 0.1)",
        "oklch(0.5 0.1 30 40)",
        "oklch(0.5, 0.1, 30)",       # 不允许逗号
        "oklch(0.5 0.1 30 / 0.5 / 0.5)",
        "oklch(0.5 0.1 30 /)",
        "oklch(0.5 0.1 abc)",        # hue 非法
        "oklch(0.5 0.1 30px)",       # 未知角度单位
        "oklch(none 0.1 30)",        # L 不接受 none
        "oklch(0.5 none 30)",        # C 不接受 none
        "oklch(0.5 0.1 30 / none)",  # alpha 不接受 none
        "oklch 0.5 0.1 30",
    ],
)
def test_invalid_syntax_422(color):
    expect422({"color": color})


@pytest.mark.parametrize(
    "color",
    [
        "oklch(1e999 0.1 30)",        # L 非有限
        "oklch(0.5 1e999 30)",        # C 非有限
        "oklch(0.5 0.1 1e999)",       # hue 非有限
        "oklch(0.5 0.1 30 / 1e999)",  # alpha 非有限
    ],
)
def test_non_finite_422(color):
    expect422({"color": color})


@pytest.mark.parametrize(
    "color",
    [
        "oklch(1.5 0.1 30)",
        "oklch(-0.1 0.1 30)",
        "oklch(150% 0.1 30)",
        "oklch(-10% 0.1 30)",
    ],
)
def test_lightness_out_of_range_422(color):
    msg = expect422({"color": color})
    assert "L" in msg


@pytest.mark.parametrize(
    "color",
    [
        "oklch(0.5 0.1 30 / 1.5)",
        "oklch(0.5 0.1 30 / -0.1)",
        "oklch(0.5 0.1 30 / 150%)",
    ],
)
def test_alpha_out_of_range_422(color):
    msg = expect422({"color": color})
    assert "alpha" in msg


@pytest.mark.parametrize(
    "color",
    [
        "oklch(0.5 -0.1 30)",
        "oklch(0.5 -10% 30)",
    ],
)
def test_negative_chroma_422(color):
    msg = expect422({"color": color})
    assert "C" in msg


@pytest.mark.parametrize("tolerance", [0.0, -1e-5, 1e-8, 0.002, 1.0])
def test_tolerance_out_of_range_422(tolerance):
    msg = expect422({"color": "oklch(0.5 0.1 30)", "tolerance": tolerance})
    assert "tolerance" in msg


def test_tolerance_non_finite_422():
    assert "tolerance" in expect422_raw(
        {"color": "oklch(0.5 0.1 30)", "tolerance": float("nan")})
    assert "tolerance" in expect422_raw(
        {"color": "oklch(0.5 0.1 30)", "tolerance": float("inf")})


@pytest.mark.parametrize("tolerance", [1e-7, 1e-3])
def test_tolerance_boundaries_ok(tolerance):
    data = gmap({"color": "oklch(0.7 0.3 30)", "tolerance": tolerance})
    assert data["tolerance"] == tolerance
    # 最后一轮更新后区间宽度不大于 tolerance
    last = data["rounds"][-1]
    if last["in_gamut"]:
        width = last["hi"] - last["mid"]
    else:
        width = last["mid"] - last["lo"]
    assert width <= tolerance + 1e-9


def test_tolerance_default():
    data = gmap({"color": "oklch(0.5 0.1 30)"})
    assert data["tolerance"] == 1e-5


# ---------- 响应结构与引擎 ----------


def test_response_fields_and_trace():
    data = gmap({"color": "oklch(0.7 0.3 30 / 0.8)", "tolerance": 1e-5})
    assert set(data.keys()) == {
        "input", "tolerance", "original", "mapped", "was_mapped",
        "chroma_loss", "srgb_unclamped", "srgb", "rgba", "rounds", "trace",
    }
    assert set(data["rgba"].keys()) == {"r", "g", "b", "alpha", "css"}
    assert set(data["rounds"][0].keys()) == {"lo", "hi", "mid", "in_gamut"}
    assert isinstance(data["trace"], list) and len(data["trace"]) >= 3
    assert any("二分" in step for step in data["trace"])
    r8 = data["rgba"]
    assert all(isinstance(r8[k], int) for k in ("r", "g", "b"))
    assert r8["css"].startswith("rgba(")


def test_engine_validation_error_class():
    with pytest.raises(GamutMapError):
        engine(color="not-a-color")
    with pytest.raises(GamutMapError):
        engine(tolerance=0.5)


def test_huge_chroma_does_not_crash():
    # 极大 C 触发 LMS 立方溢出，按色域外处理，二分仍收敛
    data = gmap({"color": "oklch(0.5 1e200 30)", "tolerance": 1e-3})
    assert data["was_mapped"] is True
    assert 0.0 < data["mapped"]["c"] < 1.0
    assert math.isfinite(data["srgb"][0])
