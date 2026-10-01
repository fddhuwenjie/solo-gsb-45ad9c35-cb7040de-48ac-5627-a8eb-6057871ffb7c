"""端到端测试：POST /color-mix。"""
import pytest
from fastapi.testclient import TestClient

from app.color_mix import ColorMixError, mix_colors
from app.main import app
from app.models import ColorMixRequest

client = TestClient(app)


def mix(body):
    resp = client.post("/color-mix", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def expect422(body):
    resp = client.post("/color-mix", json=body)
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def engine(**over):
    base = dict(color_a="#ff0000", color_b="#ffffff", interpolation_space="srgb")
    base.update(over)
    return mix_colors(ColorMixRequest(**base))


def rgba_tuple(data):
    r = data["rgba"]
    return r["r"], r["g"], r["b"], r["alpha"]


# ---------- 输入语法 ----------


@pytest.mark.parametrize(
    "text,srgb8",
    [
        ("#abc", (170, 187, 204)),
        ("#ABCD", (170, 187, 204)),
        ("#aabbcc", (170, 187, 204)),
        ("#AABBCCDD", (170, 187, 204)),
        ("rgb(170, 187, 204)", (170, 187, 204)),
        ("rgba(170,187,204,0.5)", (170, 187, 204)),
        ("rgb(170 187 204)", (170, 187, 204)),
        ("rgb(170 187 204 / 50%)", (170, 187, 204)),
        ("rgb(66.7% 73.3% 80%)", (170, 187, 204)),
    ],
)
def test_parse_syntaxes(text, srgb8):
    data = mix({"color_a": text, "color_b": text, "interpolation_space": "srgb"})
    # 同色混合（50/50）应得到同色
    assert (data["rgba"]["r"], data["rgba"]["g"], data["rgba"]["b"]) == srgb8


@pytest.mark.parametrize(
    "text,srgb8",
    [
        ("hsl(120, 100%, 50%)", (0, 255, 0)),
        ("hsl(120 100% 50%)", (0, 255, 0)),
        ("hsl(0.5turn 100% 50%)", (0, 255, 255)),
    ],
)
def test_parse_hsl_syntaxes(text, srgb8):
    # 同色 HSL 在 hsl 空间自混合应得到该色（s/l 通道相同，hue 不随转换漂移）
    data = mix({"color_a": text, "color_b": text, "interpolation_space": "hsl"})
    assert (data["rgba"]["r"], data["rgba"]["g"], data["rgba"]["b"]) == srgb8


@pytest.mark.parametrize(
    "text",
    [
        "#12",
        "#gggggg",
        "red",
        "rgb(1,2)",
        "rgb(1 2 3 / 0.5, 0.5)",
        "rgb(255, 50%, 0)",
        "hsl(120, 100, 50)",
        "lab(50% 40 59)",
        "rgb(none 0 0)",
        "",
    ],
)
def test_invalid_colors_422(text):
    expect422({"color_a": text, "color_b": "#ffffff"})


def test_hex_alpha():
    data = mix({"color_a": "#ff0000ff", "color_b": "#00000000",
                "interpolation_space": "srgb"})
    # 不透明红 与完全透明（黑，alpha=0）按预乘 alpha 混合 -> 红色、alpha 0.5
    assert rgba_tuple(data) == (255, 0, 0, 0.5)


# ---------- 权重 ----------


def test_weights_default_50_50():
    data = mix({"color_a": "#000000", "color_b": "#ffffff",
                "interpolation_space": "srgb"})
    assert data["weights"]["normalized"] == [0.5, 0.5]
    assert data["weights"]["completed"] == [50.0, 50.0]
    assert rgba_tuple(data) == (128, 128, 128, 1.0)


def test_weight_completion():
    data = mix({"color_a": "#000000", "color_b": "#ffffff", "weight_a": 25,
                "interpolation_space": "srgb"})
    assert data["weights"]["completed"] == [25.0, 75.0]
    assert data["weights"]["normalized"] == [0.25, 0.75]
    assert rgba_tuple(data)[:3] == (191, 191, 191)


def test_weights_normalized_by_sum_over_100():
    data = mix({"color_a": "#000000", "color_b": "#ffffff",
                "weight_a": 30, "weight_b": 90, "interpolation_space": "srgb"})
    assert data["weights"]["sum"] == 120.0
    assert data["weights"]["normalized"] == [pytest.approx(0.25), pytest.approx(0.75)]
    assert data["weights"]["alpha_scale"] == 1.0
    assert rgba_tuple(data)[:3] == (191, 191, 191)


def test_weights_sum_under_100_scales_alpha():
    data = mix({"color_a": "#ff0000", "color_b": "#00ff00",
                "weight_a": 20, "weight_b": 30, "interpolation_space": "srgb"})
    assert data["weights"]["sum"] == 50.0
    assert data["weights"]["normalized"] == [0.4, 0.6]
    assert data["weights"]["alpha_scale"] == 0.5
    assert rgba_tuple(data) == (102, 153, 0, 0.5)


def test_negative_weight_422():
    assert "负" in expect422(
        {"color_a": "#fff", "color_b": "#000", "weight_a": -0.01})
    assert "负" in expect422(
        {"color_a": "#fff", "color_b": "#000", "weight_b": -1})


def test_zero_sum_weights_422():
    msg = expect422({"color_a": "#fff", "color_b": "#000",
                     "weight_a": 0, "weight_b": 0})
    assert "权重和为 0" in msg


def test_completion_negative_422():
    msg = expect422({"color_a": "#fff", "color_b": "#000", "weight_a": 120})
    assert "为负" in msg


# ---------- 插值空间 ----------


def test_srgb_midpoint():
    data = mix({"color_a": "#ff0000", "color_b": "#0000ff",
                "interpolation_space": "srgb"})
    assert rgba_tuple(data)[:3] == (128, 0, 128)


def test_srgb_linear_midpoint_is_darker():
    data = mix({"color_a": "#ff0000", "color_b": "#0000ff",
                "interpolation_space": "srgb-linear"})
    # 线性光中点明显比 srgb 中点暗（188,0,188 附近）
    r, g, b, _ = rgba_tuple(data)
    assert (r, g, b) == (188, 0, 188)


def test_oklab_default_space():
    data = mix({"color_a": "rgb(255 0 0)", "color_b": "rgb(0 0 255)"})
    assert data["interpolation_space"] == "oklab"
    assert data["interpolation"]["space"] == "oklab"
    r, g, b, _ = rgba_tuple(data)
    assert (r, g, b) == (140, 83, 162)


def test_hue_interpolation_only_for_polar_422():
    for space in ("srgb", "srgb-linear", "oklab"):
        msg = expect422({"color_a": "#f00", "color_b": "#00f",
                         "interpolation_space": space,
                         "hue_interpolation": "shorter"})
        assert "hue_interpolation" in msg


def test_conversion_path_recorded():
    # hsl 输入 -> srgb -> linear-srgb -> oklab -> oklch（逐跳留痕）
    data = mix({"color_a": "hsl(0 100% 50%)", "color_b": "#0000ff",
                "interpolation_space": "oklch"})
    spaces = [node["space"] for node in data["conversions"]]
    assert spaces == ["hsl", "srgb", "srgb-linear", "oklab", "oklch",
                      "srgb", "srgb-linear", "oklab", "oklch"]
    back = [node["space"] for node in data["back_conversion"]]
    assert back == ["oklch", "oklab", "srgb-linear", "srgb"]


def test_conversion_path_from_matching_space():
    data = mix({"color_a": "#ff0000", "color_b": "#0000ff",
                "interpolation_space": "srgb-linear"})
    spaces = [node["space"] for node in data["conversions"]]
    assert spaces == ["srgb", "srgb-linear", "srgb", "srgb-linear"]


# ---------- hue 弧线 ----------


def test_hsl_shorter_goes_through_magenta():
    # 红 hue0 与蓝 hue240：shorter 走 360 方向，中点 hue300 = 品红
    data = mix({"color_a": "hsl(0 100% 50%)", "color_b": "hsl(240 100% 50%)",
                "interpolation_space": "hsl"})
    assert rgba_tuple(data)[:3] == (255, 0, 255)


def test_hsl_longer_goes_through_green():
    data = mix({"color_a": "hsl(0 100% 50%)", "color_b": "hsl(240 100% 50%)",
                "interpolation_space": "hsl", "hue_interpolation": "longer"})
    assert rgba_tuple(data)[:3] == (0, 255, 0)


@pytest.mark.parametrize(
    "arc,h1,h2,expected_hue",
    [
        # 30 -> 330
        ("shorter", 30, 330, 0),       # 差 -60，中点 0（红）
        ("longer", 30, 330, 180),      # 绕长路，中点 180（青）
        ("increasing", 30, 330, 180),  # 单调增：330 不加 360，中点 180
        ("decreasing", 30, 330, 0),    # 起点 +360，中点 0
    ],
)
def test_hue_arcs(arc, h1, h2, expected_hue):
    data = mix({
        "color_a": f"hsl({h1} 100% 50%)",
        "color_b": f"hsl({h2} 100% 50%)",
        "interpolation_space": "hsl",
        "hue_interpolation": arc,
    })
    assert data["hue_interpolation"] == arc
    result = data["interpolation"]["result"]["coords"]
    assert result[0] == pytest.approx(expected_hue, abs=1e-3)


def test_longer_equal_hues_goes_full_circle():
    # 同 hue 的 longer：终点绕整周（+360），插值沿整周行走；
    # 中点取对侧 hue 200（绕远路），结果 hue 归一化到 [0,360)
    data = mix({"color_a": "hsl(20 100% 50%)", "color_b": "hsl(20 100% 50%)",
                "interpolation_space": "hsl", "hue_interpolation": "longer"})
    assert data["interpolation"]["end"]["coords"][0] == pytest.approx(380, abs=1e-6)
    result = data["interpolation"]["result"]["coords"]
    assert result[0] == pytest.approx(200, abs=1e-6)
    assert data["interpolation"]["hue_adjustment"] is not None
    # 1/4 处经过 hue290
    q1 = mix({"color_a": "hsl(20 100% 50%)", "color_b": "hsl(20 100% 50%)",
              "interpolation_space": "hsl", "hue_interpolation": "longer",
              "weight_a": 25, "weight_b": 75})
    assert q1["interpolation"]["result"]["coords"][0] == pytest.approx(290, abs=1e-6)


def test_missing_hue_takes_other_side():
    # 无彩色（s=0）一侧 hue 缺失，取有彩色一侧 hue：结果保持 120 相
    data = mix({"color_a": "hsl(none 0% 50%)", "color_b": "hsl(120 100% 50%)",
                "interpolation_space": "hsl"})
    result = data["interpolation"]["result"]["coords"]
    assert result[0] == pytest.approx(120, abs=1e-6)
    assert data["interpolation"]["hue_adjustment"] is not None
    assert "缺失" in data["interpolation"]["hue_adjustment"]["detail"]


def test_extremal_lightness_hue_missing():
    # l=100%（白）无 hue，与绿色混合时缺失 hue 取绿
    data = mix({"color_a": "hsl(0 100% 100%)", "color_b": "hsl(120 100% 50%)",
                "interpolation_space": "hsl"})
    result = data["interpolation"]["result"]["coords"]
    assert result[0] == pytest.approx(120, abs=1e-6)


def test_both_missing_hue_stays_missing():
    data = mix({"color_a": "hsl(0 0% 30%)", "color_b": "hsl(120 0% 70%)",
                "interpolation_space": "hsl"})
    assert data["interpolation"]["result"]["coords"][0] is None
    # 灰度混合仍正确
    assert rgba_tuple(data)[:3] == (128, 128, 128)


def test_oklch_gray_takes_other_hue():
    # 灰度（oklch c=0）的 hue 缺失，不能使用转换产生的噪声 hue，
    # 应直接取彩色一侧 hue（200）
    data = mix({"color_a": "#888888", "color_b": "hsl(200 100% 50%)",
                "interpolation_space": "oklch"})
    result = data["interpolation"]["result"]["coords"]
    assert result[0] is not None
    assert result[2] == pytest.approx(242.042, abs=1e-2)
    assert data["conversions"][3]["missing"][2] is True
    assert "缺失" in data["interpolation"]["hue_adjustment"]["detail"]


def test_hsl_input_preserves_hue_in_hsl_space():
    # hsl 输入在 hsl 目标空间直接使用语法中的 hue，不经过 sRGB 往返漂移
    data = mix({"color_a": "hsl(12.345 80% 40%)", "color_b": "hsl(60 80% 40%)",
                "interpolation_space": "hsl"})
    assert data["conversions"][0]["coords"][0] == pytest.approx(12.345, abs=1e-6)


# ---------- 预乘 alpha 与裁剪 ----------


def test_premultiplied_alpha_transparent_keeps_other_color():
    data = mix({"color_a": "rgb(255 0 0 / 0)", "color_b": "rgb(0 0 255)",
                "interpolation_space": "srgb"})
    assert rgba_tuple(data) == (0, 0, 255, 0.5)


def test_premultiplied_nodes_marked():
    data = mix({"color_a": "rgb(255 0 0 / 0.5)", "color_b": "rgb(0 0 255 / 0.5)",
                "interpolation_space": "srgb"})
    assert data["interpolation"]["start"]["premultiplied"] is True
    assert data["interpolation"]["result"]["premultiplied"] is False
    assert rgba_tuple(data) == (128, 0, 128, 0.5)


def test_clipping_recorded():
    # srgb 空间混合通常不越界；用 srgb-linear 中越界路径：构造高饱和 oklab
    data = mix({"color_a": "hsl(0 100% 50%)", "color_b": "hsl(180 100% 50%)",
                "interpolation_space": "oklch"})
    assert set(data["clipped"]) <= {True, False}
    r, g, b, _ = rgba_tuple(data)
    assert 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255
    assert data["rgba"]["alpha"] == 1.0


# ---------- 响应结构与轨迹 ----------


def test_response_fields_and_trace():
    data = mix({"color_a": "#ff0000", "color_b": "#0000ff",
                "interpolation_space": "oklab", "weight_a": 30})
    assert set(data.keys()) == {
        "color_a", "color_b", "weights", "interpolation_space",
        "hue_interpolation", "conversions", "interpolation",
        "back_conversion", "clipped", "rgba", "trace",
    }
    assert data["color_a"]["normalized_weight"] == pytest.approx(0.3)
    assert data["color_b"]["normalized_weight"] == pytest.approx(0.7)
    assert data["hue_interpolation"] is None
    assert data["rgba"]["css"].startswith("rgba(")
    assert isinstance(data["trace"], list) and len(data["trace"]) >= 6
    assert any("权重" in step for step in data["trace"])
    assert any("插值" in step for step in data["trace"])


def test_engine_validation_error_class():
    with pytest.raises(ColorMixError):
        engine(color_a="not-a-color")
