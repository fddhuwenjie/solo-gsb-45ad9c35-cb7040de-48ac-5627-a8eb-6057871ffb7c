"""端到端测试：POST /grid-tracks。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def solve(body):
    resp = client.post("/grid-tracks", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def expect422(body):
    resp = client.post("/grid-tracks", json=body)
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def final_sizes(body):
    return [t["final_size"] for t in solve(body)["tracks"]]


def approx_list(values):
    return pytest.approx(values, abs=1e-6)


# ---------- 固定轨道与百分比 ----------


def test_fixed_px_and_percent_tracks():
    data = solve(
        {
            "container_width": 1000,
            "gap": 0,
            "columns": ["100px", "25%", "50.5px"],
            "items": [],
        }
    )
    assert [t["base"] for t in data["tracks"]] == [100.0, 250.0, 50.5]
    assert [t["cap"] for t in data["tracks"]] == [100.0, 250.0, 50.5]
    assert final_sizes(
        {"container_width": 1000, "columns": ["100px", "25%", "50.5px"]}
    ) == [100.0, 250.0, 50.5]


def test_total_gap_accounted_before_fr():
    data = solve(
        {
            "container_width": 1000,
            "gap": 10,
            "columns": ["100px", "1fr", "2fr", "25%"],
            "items": [],
        }
    )
    assert data["total_gap"] == 30.0
    # 100 + 250 + 30 间距 = 380，剩余 620 按 1:2 分
    assert final_sizes(
        {"container_width": 1000, "gap": 10, "columns": ["100px", "1fr", "2fr", "25%"]}
    ) == approx_list([100.0, 620.0 / 3, 1240.0 / 3, 250.0])
    assert data["used_width"] == 1000.0
    assert data["container_overflow"] == 0.0


def test_minmax_fixed_interval_and_inverted_minmax():
    data = solve(
        {"container_width": 1000, "columns": ["minmax(100px, 200px)"]}
    )
    t = data["tracks"][0]
    assert (t["base"], t["cap"], t["fr"], t["final_size"]) == (
        100.0,
        200.0,
        0.0,
        100.0,
    )
    # 下限大于上限：CSS 语义下下限收缩为上限，退化为固定轨道
    data = solve({"container_width": 1000, "columns": ["minmax(10px, 5px)"]})
    assert data["tracks"][0]["base"] == 5.0
    assert data["tracks"][0]["cap"] == 5.0


def test_percent_minmax_bounds_resolve_against_container():
    data = solve(
        {
            "container_width": 1000,
            "columns": ["minmax(10%, 30%)", "minmax(0px, 1fr)"],
            "gap": 0,
        }
    )
    assert data["tracks"][0]["base"] == 100.0
    assert data["tracks"][0]["cap"] == 300.0


# ---------- 项目 min_content ----------


def test_min_content_equal_split_within_caps():
    body = {
        "container_width": 1000,
        "gap": 0,
        "columns": ["minmax(0px,1fr)", "minmax(0px,1fr)", "1fr"],
        "items": [{"start": 1, "span": 2, "min_content": 300}],
    }
    data = solve(body)
    # 阶段一前两列各撑到 150；阶段二全部为弹性轨道，剩余 1000 按 fr 均分
    # （333.3 均不低于撑大后的基础尺寸 150），三列最终 333.3
    assert final_sizes(body) == approx_list(
        [1000.0 / 3, 1000.0 / 3, 1000.0 / 3]
    )
    step = data["trace"][0]
    assert step["phase"] == "min_content"
    assert step["rounds"][0]["shortfall"] == 300.0
    assert step["rounds"][0]["share"] == 150.0
    assert step["rounds"][0]["capped"] == []
    assert step["unmet"] == 0.0
    assert data["overflow"] == []


def test_min_content_capped_share_redistributes_to_others():
    body = {
        "container_width": 1000,
        "gap": 0,
        "columns": ["minmax(0px,100px)", "minmax(0px,1fr)", "1fr"],
        "items": [{"start": 1, "span": 2, "min_content": 300}],
    }
    data = solve(body)
    # 第一轮各分 150：第 0 列触顶 100，50 回流给第 1 列 -> 阶段一结束时
    # 第 1 列基础尺寸为 200；阶段二剩余 900 在两个 1fr 轨道间均分（各 450，
    # 均不低于其基础尺寸），最终 [100, 450, 450]
    assert final_sizes(body) == approx_list([100.0, 450.0, 450.0])
    rounds = data["trace"][0]["rounds"]
    assert rounds[0]["capped"] == [0]
    assert rounds[0]["redistributed"] == [1]
    assert rounds[1]["shortfall"] == 200.0
    assert rounds[1]["share"] == 200.0


def test_items_processed_short_span_first_then_input_order():
    body = {
        "container_width": 1000,
        "gap": 0,
        "columns": ["minmax(0px,1fr)", "minmax(0px,1fr)"],
        "items": [
            {"start": 1, "span": 2, "min_content": 400},  # 先给的长跨度
            {"start": 1, "span": 1, "min_content": 300},  # 短跨度先处理
        ],
    }
    data = solve(body)
    assert data["processing_order"] == [1, 0]
    steps = [s["item_index"] for s in data["trace"] if s["phase"] == "min_content"]
    assert steps == [1, 0]
    # 短跨度先把第 0 列撑到 300；长跨度还需 100，两列等额各 +50
    # （阶段一末 [350, 50]）；阶段二 1000 由两列均分，份额 500 均不低于基础
    assert final_sizes(body) == approx_list([500.0, 500.0])


def test_min_content_already_satisfied_records_zero_round():
    body = {
        "container_width": 1000,
        "gap": 0,
        "columns": ["300px", "300px"],
        "items": [{"start": 1, "span": 2, "min_content": 100}],
    }
    data = solve(body)
    step = data["trace"][0]
    assert step["rounds"][0]["share"] == 0.0
    assert step["unmet"] == 0.0


def test_unmeetable_item_reports_overflow_gap():
    body = {
        "container_width": 1000,
        "gap": 0,
        "columns": ["100px", "100px"],
        "items": [
            {"name": "wide", "start": 1, "span": 2, "min_content": 500}
        ],
    }
    data = solve(body)
    assert data["overflow"] == [
        {
            "item_index": 0,
            "name": "wide",
            "start": 1,
            "span": 2,
            "min_content": 500.0,
            "available": 200.0,
            "gap": 300.0,
        }
    ]
    step = data["trace"][0]
    assert step["rounds"] == []
    assert step["unmet"] == 300.0


# ---------- fr 迭代分配 ----------


def test_fr_freeze_when_share_below_base():
    body = {
        "container_width": 1000,
        "gap": 0,
        "columns": ["minmax(600px,1fr)", "1fr", "1fr"],
        "items": [],
    }
    data = solve(body)
    # 首轮每 fr 份额 1000/3≈333 < 第 0 列 base 600：固定第 0 列；
    # 剩余 400 在另两列均分
    assert final_sizes(body) == approx_list([600.0, 200.0, 200.0])
    fr_step = data["trace"][-1]
    assert fr_step["phase"] == "fr"
    assert fr_step["initial_free_space"] == 1000.0
    assert fr_step["rounds"][0]["frozen"] == [0]
    assert fr_step["rounds"][0]["fr_size"] == pytest.approx(1000.0 / 3, abs=1e-6)
    assert fr_step["rounds"][1]["active"] == [1, 2]
    assert fr_step["rounds"][1]["free_space"] == 400.0
    assert fr_step["rounds"][1]["fr_size"] == 200.0
    assert fr_step["rounds"][1]["frozen"] == []


def test_fr_freeze_iterates_until_stable():
    # 第 0 轮固定第 0 列后，第 1 列 base 仍高于新份额，再固定一轮
    body = {
        "container_width": 1000,
        "gap": 0,
        "columns": [
            "minmax(550px,1fr)",
            "minmax(300px,1fr)",
            "1fr",
        ],
        "items": [],
    }
    data = solve(body)
    fr_rounds = data["trace"][-1]["rounds"]
    assert fr_rounds[0]["frozen"] == [0]
    # 剩 450 分给 base=300 的第 1 列与第 2 列：fr_size=225 < 300
    assert fr_rounds[1]["frozen"] == [1]
    assert fr_rounds[2]["active"] == [2]
    assert final_sizes(body) == approx_list([550.0, 300.0, 150.0])


def test_no_fr_tracks_skips_distribution():
    body = {"container_width": 1000, "columns": ["300px", "300px"], "gap": 0}
    data = solve(body)
    assert final_sizes(body) == [300.0, 300.0]
    assert data["trace"][-1]["rounds"] == []


def test_overflow_container_when_fixed_tracks_exceed_width():
    body = {"container_width": 1000, "columns": ["600px", "600px"], "gap": 0}
    data = solve(body)
    assert data["used_width"] == 1200.0
    assert data["container_overflow"] == 200.0


def test_single_column_has_no_gap():
    body = {"container_width": 500, "gap": 99, "columns": ["1fr"]}
    data = solve(body)
    assert data["total_gap"] == 0.0
    assert final_sizes(body) == [500.0]


# ---------- 校验（422） ----------


def test_negative_and_bad_track_syntax_return_422():
    bad_tracks = [
        "-5px",
        "auto",
        "1em",
        "100",  # 无单位数不是合法轨道（px 必须显式书写）
        "minmax(1fr, 1fr)",
        "minmax(10px, auto)",
        "minmax(1px, 2px, 3px)",
        "minmax(10px, )",
        "calc(1px + 2px)",
        "100 px",  # 数字与单位间不允许空白
        "",
    ]
    for track in bad_tracks:
        detail = expect422(
            {"container_width": 1000, "columns": [track], "items": []}
        )
        assert track == "" or "轨道" in detail


def test_negative_container_and_gap_return_422():
    assert "container_width" in expect422(
        {"container_width": -1, "columns": ["1fr"]}
    )
    assert "gap" in expect422(
        {"container_width": 100, "gap": -1, "columns": ["1fr"]}
    )
    assert "gap 合计" in expect422(
        {"container_width": 100, "gap": 200, "columns": ["1fr", "1fr"]}
    )


def test_empty_columns_return_422():
    assert "轨道" in expect422({"container_width": 100, "columns": []})


def test_bad_items_return_422():
    base = {"container_width": 100, "columns": ["1fr", "1fr"]}
    assert "start" in expect422({**base, "items": [{"start": 0, "span": 1}]})
    assert "span" in expect422({**base, "items": [{"start": 1, "span": 0}]})
    assert "超出" in expect422({**base, "items": [{"start": 2, "span": 2}]})
    assert "min_content" in expect422(
        {**base, "items": [{"start": 1, "span": 1, "min_content": -5}]}
    )
    # 布尔 / 浮点跨度由 Pydantic 与引擎双重拒绝
    resp = client.post(
        "/grid-tracks",
        json={**base, "items": [{"start": 1, "span": True}]},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/grid-tracks",
        json={**base, "items": [{"start": 1.5, "span": 1}]},
    )
    assert resp.status_code == 422


def test_fr_unit_case_insensitive():
    body = {"container_width": 100, "columns": ["1FR", "MinMax(0PX, 1FR)"]}
    data = solve(body)
    assert final_sizes(body) == approx_list([50.0, 50.0])
    assert data["tracks"][0]["kind"] == "fr"
    assert data["tracks"][1]["kind"] == "minmax"
