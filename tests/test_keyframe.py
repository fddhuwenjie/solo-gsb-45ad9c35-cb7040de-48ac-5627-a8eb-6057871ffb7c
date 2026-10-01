"""端到端测试：POST /keyframe-sample。"""
import pytest
from fastapi.testclient import TestClient

from app.keyframe import KeyframeValidationError, sample_keyframes
from app.main import app
from app.models import KeyframeIn, KeyframeSampleRequest

client = TestClient(app)


def make_req(**over):
    base = dict(
        keyframes=[
            KeyframeIn(offset=0, value="0px"),
            KeyframeIn(offset=0.5, value="10px"),
            KeyframeIn(offset=1, value="20px"),
        ],
        duration=1000,
        delay=0,
        iteration_count=1,
        direction="normal",
        fill_mode="none",
        easing="linear",
        time=0,
    )
    base.update(over)
    return KeyframeSampleRequest(**base)


def sample(**over):
    return sample_keyframes(make_req(**over))


def post(body):
    return client.post("/keyframe-sample", json=body)


def base_body(**over):
    body = {
        "keyframes": [
            {"offset": 0, "value": "0px"},
            {"offset": 0.5, "value": "10px"},
            {"offset": 1, "value": "20px"},
        ],
        "duration": 1000,
        "delay": 0,
        "iteration_count": 1,
        "direction": "normal",
        "fill_mode": "none",
        "easing": "linear",
        "time": 0,
    }
    body.update(over)
    return body


# ---------- active：定位、轮次、插值 ----------


def test_active_start():
    data = sample(time=0)
    assert (data.phase, data.iteration, data.segment) == ("active", 0, 0)
    assert data.local_progress == 0
    assert data.eased_progress == 0
    assert data.value == "0px"


def test_active_mid_segment():
    data = sample(time=250)
    assert data.phase == "active"
    assert data.iteration == 0
    assert data.segment == 0
    assert data.local_progress == pytest.approx(0.5)
    assert data.eased_progress == pytest.approx(0.5)
    assert data.value == "5px"


def test_second_segment():
    data = sample(time=750)
    assert (data.iteration, data.segment) == (0, 1)
    assert data.local_progress == pytest.approx(0.5)
    assert data.value == "15px"


def test_internal_boundary_goes_to_next_segment():
    # t=500 是帧边界：归下一段（段 1）的起点
    data = sample(time=500)
    assert data.segment == 1
    assert data.local_progress == 0
    assert data.value == "10px"


def test_end_boundary_uses_last_segment_end():
    # active 末边界：取末段终点（段 1），而非下一轮
    data = sample(time=1000)
    assert data.phase == "active"
    assert data.iteration == 0
    assert data.segment == 1
    assert data.local_progress == 1
    assert data.eased_progress == 1
    assert data.value == "20px"


def test_multiple_iterations_internal_boundary_goes_to_next_iteration():
    data = sample(iteration_count=3, time=1000)
    assert (data.phase, data.iteration, data.local_progress) == ("active", 1, 0)
    assert data.value == "0px"


def test_multiple_iterations_middle():
    data = sample(iteration_count=3, time=2250)
    assert (data.iteration, data.segment, data.local_progress) == (2, 0, 0.5)
    assert data.value == "5px"


def test_final_end_boundary_uses_final_iteration_end():
    data = sample(iteration_count=3, time=3000)
    assert (data.phase, data.iteration, data.segment) == ("active", 2, 1)
    assert data.value == "20px"


def test_delay_shifts_active_window():
    data = sample(delay=200, time=450)
    assert data.phase == "active"
    assert data.iteration == 0
    assert data.segment == 0
    assert data.local_progress == pytest.approx(0.5)
    assert data.value == "5px"  # 段 0 中点：0 与 10 之间


def test_negative_delay():
    # active_start = -500，t=0 时进度 0.5
    data = sample(delay=-500, time=0)
    assert data.phase == "active"
    assert data.local_progress == 0
    assert data.segment == 1
    assert data.value == "10px"


# ---------- 方向 ----------


def test_reverse_direction():
    # reverse：首轮起点定向到关键帧 1（末帧），为末段终点
    data = sample(direction="reverse", time=0)
    assert data.iteration == 0
    assert data.segment == 1
    assert data.local_progress == 1
    assert data.value == "20px"


def test_reverse_direction_halfway():
    data = sample(direction="reverse", time=250)
    assert data.segment == 1
    assert data.local_progress == pytest.approx(0.5)
    assert data.value == "15px"


def test_reverse_direction_end():
    # 末边界取末轮终点；reverse 下末轮终点定向到首帧：段 0 起点
    data = sample(direction="reverse", time=1000)
    assert data.segment == 0
    assert data.local_progress == 0
    assert data.value == "0px"


def test_alternate_iterations_even_odd():
    # 轮次 0 正向，轮次 1 反向
    even = sample(direction="alternate", iteration_count=2, time=0)
    odd = sample(direction="alternate", iteration_count=2, time=1000)
    assert even.value == "0px"
    assert odd.value == "20px"  # 第二轮起点即首轮终点值
    final = sample(direction="alternate", iteration_count=2, time=2000)
    assert final.iteration == 1
    assert final.value == "0px"  # 反向轮终点


def test_alternate_reverse_starts_reversed():
    first = sample(direction="alternate-reverse", iteration_count=2, time=0)
    second = sample(direction="alternate-reverse", iteration_count=2, time=1000)
    assert first.value == "20px"  # 首轮反向
    assert second.value == "0px"  # 次轮正向起点
    end = sample(direction="alternate-reverse", iteration_count=2, time=2000)
    assert end.value == "20px"


# ---------- 填充 ----------


def test_before_no_fill_returns_null():
    data = sample(delay=100, time=0)
    assert data.phase == "before"
    assert data.iteration is None
    assert data.segment is None
    assert data.local_progress is None
    assert data.eased_progress is None
    assert data.value is None


def test_after_no_fill_returns_null():
    data = sample(time=1500)
    assert data.phase == "after"
    assert data.value is None


def test_backwards_fill_uses_first_iteration_start():
    data = sample(delay=100, fill_mode="backwards", time=0)
    assert data.phase == "before"
    assert data.iteration == 0
    assert data.segment == 0
    assert data.local_progress == 0
    assert data.value == "0px"


def test_forwards_fill_uses_last_iteration_end():
    data = sample(fill_mode="forwards", time=1500)
    assert data.phase == "after"
    assert data.iteration == 0
    assert data.segment == 1
    assert data.local_progress == 1
    assert data.value == "20px"


def test_both_fill_covers_both_sides():
    before = sample(delay=100, fill_mode="both", time=0)
    after = sample(fill_mode="both", time=1500)
    assert before.phase == "before" and before.value == "0px"
    assert after.phase == "after" and after.value == "20px"


def test_forwards_does_not_cover_before():
    data = sample(delay=100, fill_mode="forwards", time=0)
    assert data.phase == "before" and data.value is None


def test_backwards_does_not_cover_after():
    data = sample(fill_mode="backwards", time=1500)
    assert data.phase == "after" and data.value is None


def test_fill_endpoint_follows_direction():
    # reverse：首轮起点（backwards）取反向值 20；末轮终点（forwards）取 0
    before = sample(delay=100, direction="reverse", fill_mode="both", time=0)
    assert before.iteration == 0 and before.value == "20px"
    after = sample(direction="reverse", fill_mode="both", time=1500)
    assert after.iteration == 0 and after.value == "0px"


def test_fill_endpoint_alternate_final_iteration():
    # 2 轮 alternate：末轮（轮次 1，反向）终点值为 0
    after = sample(iteration_count=2, direction="alternate",
                   fill_mode="forwards", time=2500)
    assert (after.phase, after.iteration) == ("after", 1)
    assert after.value == "0px"
    # backwards 永远取首轮（轮次 0）起点：正向 0
    before = sample(delay=100, iteration_count=2, direction="alternate",
                    fill_mode="backwards", time=0)
    assert before.iteration == 0 and before.value == "0px"


# ---------- 缓动 ----------


def test_linear_easing_is_identity():
    data = sample(time=250)
    assert data.eased_progress == data.local_progress


def test_cubic_bezier_identity():
    data = sample(easing="cubic-bezier(0, 0, 1, 1)", time=250)
    assert data.eased_progress == pytest.approx(0.5)
    assert data.value == "5px"


def test_cubic_bezier_symmetric_s_curve_midpoint():
    data = sample(easing="cubic-bezier(0.25, 0.1, 0.75, 0.9)", time=250)
    # 对称曲线：x(0.5)=0.5，且 y(0.5)=0.5
    assert data.eased_progress == pytest.approx(0.5, abs=1e-9)


def test_cubic_bezier_ease_in_slow_at_start():
    # ease-in（0.42, 0, 1, 1）：前半段缓动进度小于线性
    data = sample(easing="cubic-bezier(0.42, 0, 1, 1)", time=250)
    assert 0 < data.eased_progress < 0.5
    assert float(data.value.rstrip("px")) < 5.0


def test_cubic_bezier_overshoot_y_can_exceed_one():
    # y2 > 1：末段后段缓动进度可超过 1（值可越过末帧）
    data = sample(easing="cubic-bezier(0, 0, 0.5, 1.5)", time=900)
    assert data.eased_progress > 1.0
    assert float(data.value.rstrip("px")) > 20.0


def test_cubic_bezier_whitespace_and_uppercase_unit():
    data = sample(easing="cubic-bezier( 0.25 , 0.1 , 0.75 , 0.9 )", time=250)
    assert data.eased_progress == pytest.approx(0.5, abs=1e-9)


def test_eased_progress_differs_from_local():
    data = sample(easing="cubic-bezier(0.42, 0, 0.58, 1)", time=100)
    # ease-in-out 在 0.2 附近慢于线性
    assert data.eased_progress < data.local_progress


# ---------- 单位与取值格式 ----------


def test_percent_unit():
    data = sample(keyframes=[
        KeyframeIn(offset=0, value="0%"),
        KeyframeIn(offset=1, value="100%"),
    ], time=500)
    assert data.value == "50%"


def test_unitless_values():
    data = sample(keyframes=[
        KeyframeIn(offset=0, value="0"),
        KeyframeIn(offset=1, value="1"),
    ], time=500)
    assert data.value == "0.5"


def test_decimal_fractions():
    data = sample(keyframes=[
        KeyframeIn(offset=0, value="0px"),
        KeyframeIn(offset=1, value="3px"),
    ], time=333.333333)
    # 不产生浮点噪声
    assert "e" not in data.value
    assert float(data.value.rstrip("px")) == pytest.approx(1.0, abs=1e-8)


def test_no_negative_zero():
    # reverse 下局部 0 插值不应出现 -0px
    data = sample(keyframes=[
        KeyframeIn(offset=0, value="100px"),
        KeyframeIn(offset=1, value="0px"),
    ], direction="reverse", time=1000)
    assert data.value == "100px"


def test_unit_case_insensitive():
    data = sample(keyframes=[
        KeyframeIn(offset=0, value="0PX"),
        KeyframeIn(offset=1, value="10px"),
    ], time=500)
    assert data.value == "5px"  # 归一为小写


# ---------- 非等距偏移 ----------


def test_non_uniform_offsets():
    data = sample(keyframes=[
        KeyframeIn(offset=0, value="0px"),
        KeyframeIn(offset=0.25, value="10px"),
        KeyframeIn(offset=1, value="20px"),
    ], time=250)
    # 进度 0.25：帧边界归段 1 起点
    assert (data.segment, data.value) == (1, "10px")


def test_non_uniform_offset_mid():
    data = sample(keyframes=[
        KeyframeIn(offset=0, value="0px"),
        KeyframeIn(offset=0.25, value="10px"),
        KeyframeIn(offset=1, value="20px"),
    ], time=625)
    # 进度 0.625：位于段 [0.25, 1]，段内进度 0.5
    assert data.segment == 1
    assert data.local_progress == pytest.approx(0.5)
    assert data.value == "15px"


# ---------- 422 校验 ----------


def assert_validation_error(**over):
    with pytest.raises(KeyframeValidationError):
        sample(**over)


def test_422_non_positive_duration():
    assert_validation_error(duration=0)
    assert_validation_error(duration=-1)


def test_422_offsets_must_cover_zero_and_one():
    assert_validation_error(keyframes=[
        KeyframeIn(offset=0.1, value="0px"),
        KeyframeIn(offset=1, value="10px"),
    ])
    assert_validation_error(keyframes=[
        KeyframeIn(offset=0, value="0px"),
        KeyframeIn(offset=0.9, value="10px"),
    ])


def test_422_unsorted_offsets():
    assert_validation_error(keyframes=[
        KeyframeIn(offset=0, value="0px"),
        KeyframeIn(offset=0.8, value="10px"),
        KeyframeIn(offset=0.5, value="20px"),
        KeyframeIn(offset=1, value="30px"),
    ])


def test_422_duplicate_offsets():
    assert_validation_error(keyframes=[
        KeyframeIn(offset=0, value="0px"),
        KeyframeIn(offset=0.5, value="10px"),
        KeyframeIn(offset=0.5, value="11px"),
        KeyframeIn(offset=1, value="20px"),
    ])


def test_422_too_few_keyframes():
    assert_validation_error(keyframes=[
        KeyframeIn(offset=0, value="0px"),
    ])


def test_422_unit_conflict():
    assert_validation_error(keyframes=[
        KeyframeIn(offset=0, value="0px"),
        KeyframeIn(offset=1, value="100%"),
    ])


def test_422_non_numeric_value():
    assert_validation_error(keyframes=[
        KeyframeIn(offset=0, value="0px"),
        KeyframeIn(offset=1, value="red"),
    ])
    assert_validation_error(keyframes=[
        KeyframeIn(offset=0, value="0px"),
        KeyframeIn(offset=1, value="1px 2px"),
    ])


def test_422_non_positive_integer_iteration_count():
    assert_validation_error(iteration_count=0)
    assert_validation_error(iteration_count=-2)
    # bool 与 1.5 等非正整数值在请求模型层即被拒绝
    with pytest.raises(Exception):
        make_req(iteration_count=True)
    with pytest.raises(Exception):
        make_req(iteration_count=1.5)


def test_422_invalid_easing():
    assert_validation_error(easing="ease")
    assert_validation_error(easing="cubic-bezier(0,0,1)")
    assert_validation_error(easing="linear(1, 2)")
    assert_validation_error(easing="cubic-bezier(0.1, 0, 1.1, 1)")
    assert_validation_error(easing="cubic-bezier(-0.1, 0, 1, 1)")


def test_422_non_finite_inputs():
    assert_validation_error(duration=float("nan"))
    assert_validation_error(time=float("inf"))
    assert_validation_error(delay=float("-inf"))
    assert_validation_error(easing="cubic-bezier(0, nan, 1, 1)")
    assert_validation_error(keyframes=[
        KeyframeIn(offset=0, value="0px"),
        KeyframeIn(offset=1, value="nan"),
    ])


# ---------- HTTP ----------


def test_http_success():
    resp = post(base_body(time=250))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["phase"] == "active"
    assert data["value"] == "5px"
    # 字段依序：阶段、轮次、段、局部、缓动、取值
    assert list(data.keys()) == [
        "phase", "iteration", "segment",
        "local_progress", "eased_progress", "value",
    ]


def test_http_fill_none_null_value():
    resp = post(base_body(time=2000))
    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "phase": "after",
        "iteration": None,
        "segment": None,
        "local_progress": None,
        "eased_progress": None,
        "value": None,
    }


def test_http_422_on_non_positive_duration():
    resp = post(base_body(duration=0))
    assert resp.status_code == 422


def test_http_422_on_unit_conflict():
    body = base_body()
    body["keyframes"][-1]["value"] = "20%"
    resp = post(body)
    assert resp.status_code == 422
    assert "单位" in resp.json()["detail"]


def test_http_422_on_invalid_literal():
    resp = post(base_body(direction="sideways"))
    assert resp.status_code == 422


def test_http_cubic_bezier_request():
    resp = post(base_body(
        easing="cubic-bezier(0.42, 0, 0.58, 1)",
        fill_mode="both",
        time=250,
    ))
    assert resp.status_code == 200
    data = resp.json()
    # 对称 ease-in-out 曲线在 x=0.5 处 y=0.5
    assert data["eased_progress"] == pytest.approx(0.5, abs=1e-9)


def test_health_still_ok():
    assert client.get("/health").json() == {"status": "ok"}
