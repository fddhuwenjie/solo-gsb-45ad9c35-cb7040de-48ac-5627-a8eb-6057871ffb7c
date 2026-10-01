"""端到端测试：POST /counter-render。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def render(payload):
    resp = client.post("/counter-render", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def expect422(payload):
    resp = client.post("/counter-render", json=payload)
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def node(root_id="r", content=None, **over):
    n = {"id": root_id}
    if content is not None:
        n["content"] = content
    n.update(over)
    return n


def tree(**over):
    return {"root": node(**over)}


# ---------- 基本渲染 ----------


def test_text_and_default_decimal_counter():
    body = tree(
        id="r",
        counter_reset=[{"op": "reset", "name": "n", "value": 3}],
        content=["第 ", {"kind": "counter", "name": "n"}, " 章"],
    )
    data = render(body)
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["id"] == "r"
    assert data["nodes"][0]["text"] == "第 3 章"


def test_increment_default_delta_is_one():
    body = tree(
        id="r",
        counter_reset=[{"op": "reset", "name": "n"}],  # 默认 0
        children=[
            node("a", counter_increment=[{"op": "increment", "name": "n"}],
                 content=[{"kind": "counter", "name": "n"}]),
            node("b", counter_increment=[{"op": "increment", "name": "n", "value": 4}],
                 content=[{"kind": "counter", "name": "n"}]),
            node("c", content=[{"kind": "counter", "name": "n"}]),
        ],
    )
    data = render(body)
    assert [n["text"] for n in data["nodes"]] == ["", "1", "5", "5"]


def test_preorder_output_order():
    body = tree(
        id="r",
        children=[
            node("a", children=[node("a1"), node("a2")]),
            node("b"),
        ],
    )
    data = render(body)
    assert [n["id"] for n in data["nodes"]] == ["r", "a", "a1", "a2", "b"]


# ---------- reset -> set -> increment 顺序 ----------


def test_reset_set_increment_order_on_one_node():
    body = tree(
        id="r",
        counter_reset=[{"op": "reset", "name": "n", "value": 10}],
        counter_set=[{"op": "set", "name": "n", "value": 3}],
        counter_increment=[{"op": "increment", "name": "n", "value": 2}],
        content=[{"kind": "counter", "name": "n"}],
    )
    data = render(body)
    assert data["nodes"][0]["text"] == "5"
    types = [e["type"] for e in data["nodes"][0]["enter"]]
    assert types == ["reset", "set", "increment"]


def test_set_default_value_is_zero():
    body = tree(
        id="r",
        counter_set=[{"op": "set", "name": "n"}],
        content=[{"kind": "counter", "name": "n"}],
    )
    data = render(body)
    assert data["nodes"][0]["text"] == "0"
    assert [e["type"] for e in data["nodes"][0]["enter"]] == ["create", "set"]


# ---------- 作用域：遮蔽与恢复 ----------


def test_reset_shadows_outer_and_restores_on_exit():
    body = tree(
        id="r",
        counter_reset=[{"op": "reset", "name": "n", "value": 7}],
        content=[{"kind": "counter", "name": "n"}],
        children=[
            node(
                "inner",
                counter_reset=[{"op": "reset", "name": "n", "value": 2}],
                content=[{"kind": "counter", "name": "n"}],
                children=[
                    node("kid",
                         counter_increment=[{"op": "increment", "name": "n"}],
                         content=[{"kind": "counter", "name": "n"}]),
                ],
            ),
            node("after", content=[{"kind": "counter", "name": "n"}]),
        ],
    )
    data = render(body)
    texts = {n["id"]: n["text"] for n in data["nodes"]}
    # kid 中的 increment 作用于 inner 建立的内层实例；inner 退出后恢复外层 7
    assert texts == {"r": "7", "inner": "2", "kid": "3", "after": "7"}

    rows = {n["id"]: n for n in data["nodes"]}
    reset_event = rows["inner"]["enter"][0]
    assert reset_event["type"] == "reset"
    assert reset_event["value"] == 2
    assert reset_event["shadow"] == 7
    exit_event = rows["inner"]["exit"][0]
    assert exit_event["type"] == "restore"
    assert exit_event["value"] == 7


def test_double_reset_on_same_node_is_lifo():
    body = tree(
        id="r",
        counter_reset=[
            {"op": "reset", "name": "n", "value": 1},
            {"op": "reset", "name": "n", "value": 2},
        ],
        content=[{"kind": "counters", "name": "n", "separator": "-"}],
    )
    data = render(body)
    assert data["nodes"][0]["text"] == "1-2"
    exits = [(e["type"], e.get("value")) for e in data["nodes"][0]["exit"]]
    assert exits == [("restore", 1), ("destroy", None)]


def test_lazy_create_is_destroyed_on_exit():
    body = tree(
        id="r",
        counter_increment=[{"op": "increment", "name": "n"}],
        children=[
            node("kid", content=[{"kind": "counter", "name": "n"}]),
        ],
    )
    data = render(body)
    rows = {n["id"]: n for n in data["nodes"]}
    assert [e["type"] for e in rows["r"]["enter"]] == ["create", "increment"]
    assert rows["r"]["exit"] == [{"phase": "exit", "type": "destroy",
                                  "name": "n", "value": None, "shadow": None}]
    assert rows["kid"]["text"] == "1"


def test_set_within_subtree_modifies_existing_outer_instance():
    # counter-set 作用于已存在的外层实例时直接改写（CSS 语义：只有
    # counter-reset 才建立嵌套作用域）；惰性建立仅在计数器缺失时发生
    body = tree(
        id="r",
        counter_reset=[{"op": "reset", "name": "n", "value": 9}],
        children=[
            node(
                "inner",
                counter_set=[{"op": "set", "name": "n", "value": 0}],
                content=[{"kind": "counter", "name": "n"}],
            ),
            node("after", content=[{"kind": "counter", "name": "n"}]),
        ],
    )
    data = render(body)
    texts = {n["id"]: n["text"] for n in data["nodes"]}
    assert texts == {"r": "", "inner": "0", "after": "0"}
    rows = {n["id"]: n for n in data["nodes"]}
    assert rows["inner"]["enter"][0]["type"] == "set"
    assert rows["inner"]["exit"] == []


def test_set_missing_counter_creates_scoped_instance():
    # 无外层实例时 set 先以 0 建立实例，该实例只在当前子树生效，退出销毁
    body = tree(
        id="r",
        children=[
            node(
                "inner",
                counter_set=[{"op": "set", "name": "n", "value": 4}],
                children=[node("kid", content=[{"kind": "counter", "name": "n"}])],
            ),
        ],
    )
    data = render(body)
    rows = {n["id"]: n for n in data["nodes"]}
    assert rows["kid"]["text"] == "4"
    assert [e["type"] for e in rows["inner"]["enter"]] == ["create", "set"]
    assert rows["inner"]["exit"][0]["type"] == "destroy"


# ---------- counters() 从外到内 ----------


def test_counters_concatenates_outer_to_inner():
    body = tree(
        id="book",
        counter_reset=[{"op": "reset", "name": "chap"}],
        children=[
            node(
                "ch",
                counter_increment=[{"op": "increment", "name": "chap"}],
                children=[
                    node(
                        "sec-wrap",
                        counter_reset=[{"op": "reset", "name": "sec"}],
                        children=[
                            node(
                                "sec",
                                counter_increment=[
                                    {"op": "increment", "name": "sec"}
                                ],
                                content=[
                                    {"kind": "counters", "name": "chap",
                                     "separator": "."},
                                    ".",
                                    {"kind": "counters", "name": "sec",
                                     "separator": ".",
                                     "style": "decimal-leading-zero"},
                                ],
                            ),
                        ],
                    )
                ],
            )
        ],
    )
    data = render(body)
    sec = next(n for n in data["nodes"] if n["id"] == "sec")
    assert sec["text"] == "1.01"


# ---------- 计数格式 ----------


def test_alpha_styles():
    def styled(style, value):
        body = tree(id="r",
                    counter_reset=[{"op": "reset", "name": "n", "value": value}],
                    content=[{"kind": "counter", "name": "n", "style": style}])
        return render(body)["nodes"][0]["text"]

    assert styled("lower-alpha", 1) == "a"
    assert styled("upper-alpha", 26) == "Z"
    assert styled("lower-latin", 27) == "aa"
    assert styled("upper-latin", 27) == "AA"
    assert styled("lower-alpha", 53) == "ba"


def test_alpha_out_of_range_falls_back_to_decimal():
    for style, value in [("lower-alpha", 0), ("upper-alpha", -3)]:
        body = tree(id="r",
                    counter_reset=[{"op": "reset", "name": "n", "value": value}],
                    content=[{"kind": "counter", "name": "n", "style": style}])
        assert render(body)["nodes"][0]["text"] == str(value)


def test_decimal_leading_zero():
    for value, expected in [(1, "01"), (9, "09"), (10, "10"), (123, "123"), (0, "00")]:
        body = tree(id="r",
                    counter_reset=[{"op": "reset", "name": "n", "value": value}],
                    content=[{"kind": "counter", "name": "n",
                              "style": "decimal-leading-zero"}])
        assert render(body)["nodes"][0]["text"] == expected


def test_negative_increment_and_counters():
    body = tree(
        id="r",
        counter_reset=[{"op": "reset", "name": "n", "value": 5}],
        counter_increment=[{"op": "increment", "name": "n", "value": -10}],
        content=[{"kind": "counters", "name": "n", "separator": "->"}],
    )
    assert render(body)["nodes"][0]["text"] == "-5"


# ---------- 裸字符串片段 ----------


def test_bare_string_fragments_are_text():
    body = tree(id="r", content=["hello", " ", "world"])
    assert render(body)["nodes"][0]["text"] == "hello world"


# ---------- 422 校验 ----------


def test_422_duplicate_ids():
    detail = expect422(tree(id="a", children=[node("a")]))
    assert "重复" in detail


def test_422_duplicate_integer_ids():
    detail = expect422(tree(id=3, children=[node(3)]))
    assert "重复" in detail


def test_422_illegal_name_in_operation():
    for bad in ["1x", "-x", "a b", ""]:
        detail = expect422(
            tree(id="r", counter_reset=[{"op": "reset", "name": bad}])
        )
        assert "非法计数器名称" in detail


def test_422_illegal_name_in_fragment():
    detail = expect422(
        tree(id="r", content=[{"kind": "counter", "name": "a$"}])
    )
    assert "非法计数器名称" in detail


def test_422_illegal_style():
    detail = expect422(
        tree(id="r",
             counter_reset=[{"op": "reset", "name": "x"}],
             content=[{"kind": "counter", "name": "x", "style": "lower-roman"}])
    )
    assert "计数格式" in detail


def test_422_missing_counter_reference():
    detail = expect422(tree(id="r", content=[{"kind": "counter", "name": "x"}]))
    assert "不存在" in detail


def test_422_missing_counters_reference():
    detail = expect422(
        tree(id="r", content=[{"kind": "counters", "name": "y", "separator": "."}])
    )
    assert "不存在" in detail


def test_422_reference_escapes_destroyed_scope():
    detail = expect422(
        tree(
            id="r",
            children=[
                node("a", counter_reset=[{"op": "reset", "name": "x"}]),
                node("b", content=[{"kind": "counter", "name": "x"}]),
            ],
        )
    )
    assert "不存在" in detail


def test_422_missing_separator():
    detail = expect422(
        tree(id="r", content=[{"kind": "counters", "name": "x"}])
    )
    assert "separator" in detail


def test_422_non_integer_value():
    detail1 = str(expect422(
        tree(id="r", counter_set=[{"op": "set", "name": "x", "value": 1.5}])
    ))
    detail2 = str(expect422(
        tree(id="r",
             counter_increment=[{"op": "increment", "name": "x", "value": True}])
    ))
    assert "整数" in detail1
    assert "整数" in detail2


def test_integer_and_string_ids_coexist():
    # 1 与 "1" 是不同的 id，不应判重
    data = render(tree(id=1, children=[node("1")]))
    assert [n["id"] for n in data["nodes"]] == [1, "1"]
