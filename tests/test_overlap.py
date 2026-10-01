"""端到端测试：POST /selector-overlap。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def overlap(a, b):
    resp = client.post("/selector-overlap", json={"selector_a": a, "selector_b": b})
    assert resp.status_code == 200, resp.text
    return resp.json()


def chain(data):
    """根 -> 目标 的见证节点标签序列。"""
    return [el.get("tag", "") for el in data["ancestors"]] + [
        data["element"].get("tag", "")
    ]


# ---------- 可满足：基本对齐 ----------


def test_single_compound_identical_selectors():
    data = overlap("p.text", "p.text")
    assert data["satisfiable"] is True
    assert data["conflict"] is None
    assert data["ancestors"] == []
    assert data["element"] == {"tag": "p", "classes": ["text"]}
    rows = data["alignment"]
    assert len(rows) == 1
    assert rows[0]["depth"] == 0
    assert (rows[0]["segment_a"], rows[0]["segment_b"]) == (0, 0)


def test_compound_constraints_merge_union():
    data = overlap("p.x", "p.y#intro[data-role=lead]")
    assert data["satisfiable"] is True
    el = data["element"]
    assert el == {
        "tag": "p",
        "id": "intro",
        "classes": ["x", "y"],
        "attributes": {"data-role": "lead"},
    }


def test_universal_selectors_merge_without_constraints():
    data = overlap("*", "div")
    assert data["satisfiable"] is True
    assert data["element"] == {"tag": "div"}


def test_descendant_conflict_is_satisfiable_with_extra_depth():
    # 同为后代组合符时，不同标签的祖先段可以错开深度
    data = overlap("div p", "section p")
    assert data["satisfiable"] is True
    assert chain(data) == ["div", "section", "p"]
    assert len(data["ancestors"]) == 2
    rows = {r["depth"]: r for r in data["alignment"]}
    assert rows[2]["segment_a"] == 0 and rows[2]["segment_b"] is None
    assert rows[1]["segment_a"] is None and rows[1]["segment_b"] == 0
    assert rows[0]["segment_a"] == 1 and rows[0]["segment_b"] == 1


def test_descendant_lexicographic_choice_is_direction_sensitive():
    # 反向请求时字典序唯一解也随之翻转
    data = overlap("section p", "div p")
    assert chain(data) == ["div", "section", "p"]


def test_child_against_descendant_is_satisfiable():
    # div > p 要求 div 恰为父；section p 的 section 可再向上放
    data = overlap("div > p", "section p")
    assert data["satisfiable"] is True
    assert chain(data) == ["section", "div", "p"]
    rows = {r["depth"]: r for r in data["alignment"]}
    assert rows[1]["segment_a"] == 0 and rows[1]["combinator_a"] == ">"
    assert rows[2]["segment_b"] == 0 and rows[2]["combinator_b"] == " "


def test_child_chains_merge_on_compatible_segments():
    data = overlap("div.foo > section > p", "div.bar > section > p")
    assert data["satisfiable"] is True
    assert chain(data) == ["div", "section", "p"]
    root = data["ancestors"][0]
    assert root["classes"] == ["bar", "foo"]
    rows = {r["depth"]: r for r in data["alignment"]}
    assert (rows[2]["segment_a"], rows[2]["segment_b"]) == (0, 0)
    assert rows[2]["combinator_a"] == ">" and rows[2]["combinator_b"] == ">"
    assert (rows[1]["segment_a"], rows[1]["segment_b"]) == (1, 1)


def test_one_side_single_compound_merges_at_target():
    data = overlap("p", "div.foo > section > p")
    assert data["satisfiable"] is True
    assert chain(data) == ["div", "section", "p"]
    assert data["ancestors"][0]["classes"] == ["foo"]


def test_universal_descendant_absorbs_other_side_chain():
    # * p 的 * 可命中任意祖先，最小解与 section p 共用一个 section 节点
    data = overlap("* p", "section p")
    assert data["satisfiable"] is True
    assert chain(data) == ["section", "p"]
    rows = {r["depth"]: r for r in data["alignment"]}
    assert (rows[1]["segment_a"], rows[1]["segment_b"]) == (0, 0)


def test_attr_presence_merges_with_concrete_value():
    data = overlap("[data-x]", "p[data-x=1]")
    assert data["satisfiable"] is True
    assert data["element"] == {"tag": "p", "attributes": {"data-x": "1"}}


def test_minimal_node_count_avoids_unnecessary_levels():
    # 两侧祖先段完全相容时应合并，节点数最少
    data = overlap("body div p", "body div p")
    assert chain(data) == ["body", "div", "p"]
    assert all(r["segment_a"] is not None and r["segment_b"] is not None
               for r in data["alignment"])


# ---------- 不可满足：复合约束冲突 ----------


def test_target_tag_conflict_is_compound():
    data = overlap("div", "p")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["kind"] == "compound"
    assert (c["segment_a"], c["segment_b"]) == (0, 0)
    assert c["field"] == "tag"
    assert c["value_a"] == "div" and c["value_b"] == "p"


def test_target_id_conflict_is_compound():
    data = overlap("p#x", "p#y")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["kind"] == "compound"
    assert c["field"] == "id"
    assert (c["value_a"], c["value_b"]) == ("x", "y")


def test_target_attr_value_conflict_is_compound():
    data = overlap("[data-x=1]", "[data-x=2]")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["kind"] == "compound"
    assert c["field"] == "attr:data-x"
    assert (c["value_a"], c["value_b"]) == ("1", "2")


def test_same_id_with_disjoint_classes_merges_on_one_node():
    # 同一个 #a 可同时带 x 与 y：后代段直接合并，节点数最少
    data = overlap("#a.x p", "#a.y p")
    assert data["satisfiable"] is True
    assert len(data["ancestors"]) == 1
    assert data["ancestors"][0]["id"] == "a"
    assert data["ancestors"][0]["classes"] == ["x", "y"]
    assert data["element"] == {"tag": "p"}


def test_self_conflicting_compound_attr_values():
    data = overlap("[data-x=1][data-x=2] p", "p")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["kind"] == "compound"
    assert c["segment_a"] == 0 and c["segment_b"] is None
    assert c["field"] == "attr:data-x"
    assert c["value_a"] == "1" and c["value_b"] == "2"


def test_self_conflict_on_side_b():
    data = overlap("p", "p[data-x=a][data-x=b]")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["segment_a"] is None and c["segment_b"] == 0


# ---------- 不可满足：子关系强制对齐冲突 ----------


def test_conflicting_parents_under_child_combinator():
    data = overlap("div > p", "section > p")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["kind"] == "child"
    assert (c["segment_a"], c["segment_b"]) == (0, 0)
    assert c["field"] == "tag"
    assert (c["value_a"], c["value_b"]) == ("div", "section")
    assert "强制对齐" in c["reason"]


def test_deep_child_chain_conflict():
    # p 的父必须同为 li；祖父必须一个是 ul 一个是 ol，无后代间隙可规避
    data = overlap("ul > li > p", "ol > li > p")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["kind"] == "child"
    assert (c["segment_a"], c["segment_b"]) == (0, 0)
    assert c["field"] == "tag"
    assert (c["value_a"], c["value_b"]) == ("ul", "ol")


def test_child_conflict_reports_innermost_forced_pair():
    # ul>li>a 与 ol>li>a：t=1 的 li/li 相容，t=2 的 ul/ol 冲突
    data = overlap("ul > li > a", "ol > li > a")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["kind"] == "child"
    assert (c["segment_a"], c["segment_b"]) == (0, 0)


def test_conflicting_child_against_compatible_descendant_is_sat():
    # div > p 对 span div > p：父同为 div，span 只是更高祖先，可满足
    data = overlap("div > p", "span div > p")
    assert data["satisfiable"] is True
    assert chain(data) == ["span", "div", "p"]


def test_attr_conflict_under_child_combinator_is_child_kind():
    data = overlap("div[data-x=1] > p", "div[data-x=2] > p")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["kind"] == "child"
    assert c["field"] == "attr:data-x"
    assert (c["segment_a"], c["segment_b"]) == (0, 0)


def test_absorption_propagates_child_rigidity_to_root_segments():
    # selector_a 的弹性后代段 p 被最近祖先吸附到 selector_b 的 p#a.y 所在层
    # 占位，于是两侧的 ">" 把根段 div 与 span 推到同一层：传播型强制对齐冲突
    data = overlap("div.x[data-k=2] > p p", "span > p#a.y > p")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["kind"] == "child"
    assert (c["segment_a"], c["segment_b"]) == (0, 0)
    assert c["field"] == "tag"
    assert (c["value_a"], c["value_b"]) == ("div", "span")
    assert "吸附" in c["reason"]
    assert "强制对齐" in c["reason"]


def test_absorption_propagation_reports_each_elastic_step():
    # 两段弹性后代段依次被吸附占位后，">" 刚性继续上传到根段
    data = overlap("div > p em i", "span > p > em > i")
    assert data["satisfiable"] is False
    c = data["conflict"]
    assert c["kind"] == "child"
    assert (c["segment_a"], c["segment_b"]) == (0, 0)
    assert c["field"] == "tag"
    assert (c["value_a"], c["value_b"]) == ("div", "span")


# ---------- 见证节点字段最小化 ----------


def test_witness_nodes_keep_only_required_fields():
    # div.foo > p.x 与 div > p：父同为 div（合并 .foo），目标同为 p（带 x）
    data = overlap("div.foo > p.x", "div > p")
    assert data["satisfiable"] is True
    assert chain(data) == ["div", "p"]
    for el in data["ancestors"] + [data["element"]]:
        assert "id" not in el
        assert "attributes" not in el
    assert data["ancestors"][0] == {"tag": "div", "classes": ["foo"]}
    assert data["element"] == {"tag": "p", "classes": ["x"]}


def test_different_child_parents_are_unsatisfiable():
    # 两侧都用 ">" 强制父标签且标签不同：子关系强制对齐冲突
    data = overlap("div > p.x", "section.foo > p")
    assert data["satisfiable"] is False
    assert data["conflict"]["kind"] == "child"


def test_presence_attr_witness_uses_empty_string():
    data = overlap("[disabled]", "input")
    assert data["satisfiable"] is True
    assert data["element"]["attributes"] == {"disabled": ""}


# ---------- 语法错误沿用既有响应 ----------


def test_syntax_error_returns_422_like_compute():
    resp = client.post(
        "/selector-overlap", json={"selector_a": "div >> p", "selector_b": "p"}
    )
    assert resp.status_code == 422
    assert "选择器语法错误" in resp.json()["detail"]


def test_empty_selector_returns_422():
    resp = client.post(
        "/selector-overlap", json={"selector_a": "p", "selector_b": ""}
    )
    assert resp.status_code == 422


def test_request_requires_both_selectors():
    resp = client.post("/selector-overlap", json={"selector_a": "p"})
    assert resp.status_code == 422
