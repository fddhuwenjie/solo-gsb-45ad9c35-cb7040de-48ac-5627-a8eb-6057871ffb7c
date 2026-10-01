"""POST /dom-impact DOM 变更样式影响分析端到端测试。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def impact(payload, expected=200):
    resp = client.post("/dom-impact", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def impact_422(payload):
    resp = client.post("/dom-impact", json=payload)
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def decl(prop, value, important=False):
    d = {"property": prop, "value": value}
    if important:
        d["important"] = True
    return d


def rule(selector, *decls, origin="author", layer=None):
    r = {"selector": selector, "origin": origin, "declarations": list(decls)}
    if layer is not None:
        r["layer"] = layer
    return r


def node(id, tag, classes=None, attributes=None, children=None):
    n = {"id": id, "tag": tag}
    if classes is not None:
        n["classes"] = classes
    if attributes is not None:
        n["attributes"] = attributes
    if children is not None:
        n["children"] = children
    return n


def payload(root, rules, mutation, layers=None):
    p = {"root": root, "rules": rules, "mutation": mutation}
    if layers is not None:
        p["layers"] = layers
    return p


def changes_map(data):
    return {
        n["node_id"]: {ch["property"]: ch for ch in n["changes"]}
        for n in data["affected_nodes"]
    }


TREE = node(
    "app", "div", classes=["dark"],
    children=[
        node("title", "h1"),
        node("lead", "p", classes=["text"], attributes={"data-role": "lead"},
             children=[node("code", "code")]),
    ],
)

RULES = [
    rule("div", decl("--gap", "8px"), decl("--accent", "red")),
    rule("div.dark", decl("--gap", "12px")),
    rule("p.text", decl("color", "var(--accent)"), decl("padding", "var(--gap, 4px)")),
    rule("code", decl("color", "var(--gap)")),
    rule("h1", decl("font-size", "2em")),
]


# ---------- class 变更 ----------


def test_remove_class_on_ancestor_propagates_through_tree():
    data = impact(payload(TREE, RULES, {"type": "remove_class", "node_id": "app", "class_name": "dark"}))
    assert data["nodes_checked"] == 4
    assert data["target_id"] == "app"
    cm = changes_map(data)

    # 目标自身：--gap 胜出者由 div.dark 改回 div（选择器命中变化 + 层叠换人）
    gap = cm["app"]["--gap"]
    assert gap["is_custom"] is True
    assert gap["reasons"] == ["selector_match", "cascade_winner"]
    assert gap["before"]["value"] == "12px"
    assert gap["after"]["value"] == "8px"
    assert gap["before"]["winner"]["selector"] == "div.dark"
    assert gap["after"]["winner"]["selector"] == "div"

    # 后代 lead：纯继承的 --gap 与 var() 链上的 padding 均变化；color 不依赖 --gap，不列出
    lead_gap = cm["lead"]["--gap"]
    assert lead_gap["reasons"] == ["inheritance"]
    assert lead_gap["before"]["winner"] is None
    assert lead_gap["after"]["winner"] is None
    padding = cm["lead"]["padding"]
    assert padding["reasons"] == ["var_chain"]
    assert (padding["before"]["value"], padding["after"]["value"]) == ("12px", "8px")
    assert "color" not in cm["lead"]

    # 孙代 code：color: var(--gap) 与继承的 --gap 一并变化
    code_color = cm["code"]["color"]
    assert code_color["reasons"] == ["var_chain"]
    assert (code_color["before"]["value"], code_color["after"]["value"]) == ("12px", "8px")
    assert cm["code"]["--gap"]["reasons"] == ["inheritance"]

    # title 自身无相关规则，但自定义属性对所有后代继承：--gap 仍变化（其
    # computed 不受影响，故只列出 custom_properties 差异）
    assert list(cm["title"]) == ["--gap"]
    assert cm["title"]["--gap"]["reasons"] == ["inheritance"]


def test_add_class_on_target_changes_its_own_cascade():
    data = impact(payload(TREE, RULES, {"type": "add_class", "node_id": "code", "class_name": "hl"}))
    # 没有任何规则使用 .hl：操作合法但整树无影响
    assert data["affected_nodes"] == []


def test_add_class_with_matching_rule():
    rules = RULES + [rule("code.hl", decl("color", "yellow"))]
    data = impact(payload(TREE, rules, {"type": "add_class", "node_id": "code", "class_name": "hl"}))
    cm = changes_map(data)
    ch = cm["code"]["color"]
    assert ch["reasons"] == ["selector_match", "cascade_winner"]
    assert (ch["before"]["value"], ch["after"]["value"]) == ("12px", "yellow")


def test_same_value_runner_up_still_reports_winner_change():
    # 继任者取值恰好相同也要报告：胜出声明定位属于被比对的状态
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red")),
        rule("p.x", decl("color", "red")),
    ]
    data = impact(payload(root, rules, {"type": "add_class", "node_id": "r", "class_name": "x"}))
    ch = changes_map(data)["r"]["color"]
    assert ch["reasons"] == ["selector_match", "cascade_winner"]
    assert ch["before"]["value"] == ch["after"]["value"] == "red"
    assert ch["before"]["winner"]["rule_index"] == 0
    assert ch["after"]["winner"]["rule_index"] == 1


# ---------- attribute 变更 ----------


def test_set_attribute_matches_attribute_selector():
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "black")),
        rule("p[data-x]", decl("color", "green")),
    ]
    data = impact(
        payload(root, rules, {"type": "set_attribute", "node_id": "r", "name": "data-x", "value": "1"})
    )
    ch = changes_map(data)["r"]["color"]
    assert ch["reasons"] == ["selector_match", "cascade_winner"]
    assert (ch["before"]["value"], ch["after"]["value"]) == ("black", "green")


def test_set_attribute_value_change_only():
    # [data-x=old] 已命中；改成另一个值，同一条存在性选择器规则仍然命中 -> 无影响
    root = node("r", "p", attributes={"data-x": "old"})
    rules = [rule("p[data-x]", decl("color", "green"))]
    data = impact(
        payload(root, rules, {"type": "set_attribute", "node_id": "r", "name": "data-x", "value": "new"})
    )
    assert data["affected_nodes"] == []


def test_remove_attribute():
    root = node("r", "p", attributes={"data-x": "1"})
    rules = [
        rule("p", decl("color", "black")),
        rule("p[data-x=1]", decl("color", "green")),
    ]
    data = impact(
        payload(root, rules, {"type": "remove_attribute", "node_id": "r", "name": "data-x"})
    )
    ch = changes_map(data)["r"]["color"]
    assert ch["reasons"] == ["selector_match", "cascade_winner"]
    assert (ch["before"]["value"], ch["after"]["value"]) == ("green", "black")


# ---------- tag 变更 ----------


def test_set_tag_on_target_drops_property():
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "black")),
        rule("div p", decl("color", "red")),
    ]
    data = impact(payload(root, rules, {"type": "set_tag", "node_id": "r", "tag": "span"}))
    ch = changes_map(data)["r"]["color"]
    assert ch["reasons"] == ["selector_match", "cascade_winner"]
    assert ch["before"]["status"] == "computed"
    assert ch["after"]["status"] == "absent"
    assert ch["after"]["value"] is None
    assert ch["after"]["winner"] is None


def test_set_tag_on_ancestor_propagates_via_descendant_combinator():
    root = node("d", "div", children=[node("p", "p")])
    rules = [
        rule("p", decl("color", "black")),
        rule("div p", decl("color", "red")),
    ]
    data = impact(payload(root, rules, {"type": "set_tag", "node_id": "d", "tag": "section"}))
    cm = changes_map(data)
    # 祖先 div 自身没有普通属性变化；后代 p 的后代组合符选择器不再命中
    assert "d" not in cm
    ch = cm["p"]["color"]
    assert ch["reasons"] == ["selector_match", "cascade_winner"]
    assert (ch["before"]["value"], ch["after"]["value"]) == ("red", "black")


# ---------- invalid 与 fallback ----------


def test_computed_becomes_invalid_when_dependency_removed():
    root = node("r", "div", classes=["bad"], children=[node("k", "p")])
    rules = [
        rule("div.bad", decl("--x", "5px")),
        rule("p", decl("margin", "var(--x)")),
    ]
    data = impact(payload(root, rules, {"type": "remove_class", "node_id": "r", "class_name": "bad"}))
    cm = changes_map(data)

    margin = cm["k"]["margin"]
    assert margin["reasons"] == ["var_chain"]
    assert margin["before"]["status"] == "computed"
    assert margin["after"]["status"] == "invalid"
    assert margin["after"]["reason"]
    assert margin["after"]["value"] is None

    # 自定义属性：目标自身声明消失（cascade），后代纯继承缺失（inheritance）
    assert cm["r"]["--x"]["reasons"] == ["selector_match", "cascade_winner"]
    assert (cm["r"]["--x"]["before"]["status"], cm["r"]["--x"]["after"]["status"]) == ("ok", "missing")
    assert cm["k"]["--x"]["reasons"] == ["inheritance"]
    assert (cm["k"]["--x"]["before"]["status"], cm["k"]["--x"]["after"]["status"]) == ("ok", "missing")


def test_invalid_becomes_computed_via_fallback():
    root = node("r", "div", classes=["on"], children=[node("p", "p")])
    rules = [
        rule("div.on", decl("--x", "1px")),
        rule("p", decl("color", "var(--x, red)")),
    ]
    data = impact(payload(root, rules, {"type": "remove_class", "node_id": "r", "class_name": "on"}))
    ch = changes_map(data)["p"]["color"]
    assert ch["reasons"] == ["var_chain"]
    assert ch["before"]["value"] == "1px"
    assert ch["after"]["value"] == "red"


def test_same_own_winner_invalid_cleared_through_var_chain():
    # 本元素 --a 的胜出声明前后同为规则 0；.c 制造循环使其失效，移除后恢复
    root = node("r", "p", classes=["c"])
    rules = [
        rule("p", decl("--a", "var(--b, 9px)")),
        rule("p.c", decl("--b", "var(--a)")),
    ]
    data = impact(payload(root, rules, {"type": "remove_class", "node_id": "r", "class_name": "c"}))
    cm = changes_map(data)
    a = cm["r"]["--a"]
    assert a["reasons"] == ["var_chain"]
    assert (a["before"]["status"], a["after"]["status"]) == ("invalid", "ok")
    assert a["after"]["value"] == "9px"
    b = cm["r"]["--b"]
    assert b["reasons"] == ["selector_match", "cascade_winner"]
    assert (b["before"]["status"], b["after"]["status"]) == ("invalid", "missing")


def test_same_own_winner_value_changes_through_inherited_dependency():
    root = node("d", "div", classes=["x"], children=[node("p", "p")])
    rules = [
        rule("div", decl("--g", "1px")),
        rule("div.x", decl("--g", "2px")),
        rule("p", decl("--a", "var(--g)")),
    ]
    data = impact(payload(root, rules, {"type": "remove_class", "node_id": "d", "class_name": "x"}))
    cm = changes_map(data)
    a = cm["p"]["--a"]
    assert a["reasons"] == ["var_chain"]  # 本元素有胜出声明，不标 inheritance
    assert (a["before"]["value"], a["after"]["value"]) == ("2px", "1px")
    assert cm["p"]["--g"]["reasons"] == ["inheritance"]


# ---------- 层 ----------


def test_layer_order_recomputed_after_mutation():
    root = node("r", "p", classes=["x"])
    rules = [
        rule("p", decl("color", "red"), layer="base"),
        rule("p.x", decl("color", "blue")),
    ]
    # 未分层优先于分层：基线 blue；移除 .x 后只剩层内 red
    data = impact(payload(root, rules, {"type": "remove_class", "node_id": "r", "class_name": "x"},
                         layers=["base"]))
    ch = changes_map(data)["r"]["color"]
    assert (ch["before"]["value"], ch["after"]["value"]) == ("blue", "red")
    assert ch["before"]["winner"]["layer"] is None
    assert ch["after"]["winner"]["layer"] == "base"


# ---------- 422 校验 ----------


def test_422_target_not_found():
    detail = impact_422(
        payload(node("a", "p"), [], {"type": "add_class", "node_id": "zzz", "class_name": "y"})
    )
    assert "不存在" in detail


def test_422_duplicate_id():
    root = node("a", "p", children=[node("a", "span")])
    detail = impact_422(payload(root, [], {"type": "set_tag", "node_id": "a", "tag": "div"}))
    assert "重复 id" in detail


def test_422_noop_mutations():
    base = {"root": node("a", "p", classes=["x"], attributes={"d": "1"}), "rules": []}
    cases = [
        {"type": "add_class", "node_id": "a", "class_name": "x"},
        {"type": "remove_class", "node_id": "a", "class_name": "y"},
        {"type": "set_attribute", "node_id": "a", "name": "d", "value": "1"},
        {"type": "remove_attribute", "node_id": "a", "name": "nope"},
        {"type": "set_tag", "node_id": "a", "tag": "P"},  # 大小写不敏感的同名 tag
    ]
    for m in cases:
        detail = impact_422({**base, "mutation": m})
        assert "空操作" in detail, m


def test_422_missing_required_fields():
    p = {"root": node("a", "p"), "rules": []}
    assert "class_name" in impact_422({**p, "mutation": {"type": "add_class", "node_id": "a"}})
    assert "name" in impact_422({**p, "mutation": {"type": "set_attribute", "node_id": "a", "value": "v"}})
    assert "value" in impact_422({**p, "mutation": {"type": "set_attribute", "node_id": "a", "name": "n"}})
    assert "name" in impact_422({**p, "mutation": {"type": "remove_attribute", "node_id": "a"}})
    assert "tag" in impact_422({**p, "mutation": {"type": "set_tag", "node_id": "a"}})


def test_422_blank_values():
    p = {"root": node("a", "p"), "rules": []}
    assert "空白" in impact_422({**p, "mutation": {"type": "add_class", "node_id": "a", "class_name": " "}})
    assert "空白" in impact_422({**p, "mutation": {"type": "set_tag", "node_id": "a", "tag": "  "}})
    assert "空白" in impact_422(
        {**p, "mutation": {"type": "set_attribute", "node_id": "a", "name": " ", "value": "v"}}
    )


def test_422_id_and_class_attributes_via_attribute_ops():
    p = {"root": node("a", "p"), "rules": []}
    assert "id" in impact_422(
        {**p, "mutation": {"type": "set_attribute", "node_id": "a", "name": "id", "value": "b"}}
    )
    assert "class" in impact_422(
        {**p, "mutation": {"type": "set_attribute", "node_id": "a", "name": "class", "value": "b"}}
    )
    assert "id" in impact_422(
        {**p, "mutation": {"type": "remove_attribute", "node_id": "a", "name": "id"}}
    )


def test_422_bad_selector_and_blank_property():
    p = {"root": node("a", "p"), "mutation": {"type": "set_tag", "node_id": "a", "tag": "div"}}
    detail = impact_422({**p, "rules": [rule("p >", decl("color", "red"))]})
    assert "选择器语法错误" in detail
    detail = impact_422({**p, "rules": [rule("p", decl("  ", "red"))]})
    assert "property" in detail


def test_422_unknown_mutation_type_and_bool_node_id():
    p = {"root": node("a", "p"), "rules": []}
    assert client.post("/dom-impact", json={**p, "mutation": {"type": "explode", "node_id": "a"}}).status_code == 422
    assert client.post(
        "/dom-impact", json={**p, "mutation": {"type": "set_tag", "node_id": True, "tag": "div"}}
    ).status_code == 422


def test_integer_node_ids_supported():
    root = node(1, "p")
    data = impact(
        payload(root, [rule("div", decl("color", "red"))], {"type": "set_tag", "node_id": 1, "tag": "div"})
    )
    assert data["target_id"] == 1
    assert changes_map(data)[1]["color"]["after"]["value"] == "red"


# ---------- 兼容与不产生部分结果 ----------


def test_existing_endpoints_still_work():
    resp = client.post("/compute", json={"element": node("a", "p"), "rules": [rule("p", decl("color", "red"))]})
    assert resp.status_code == 200
    assert resp.json()["computed"] == {"color": "red"}
    assert client.get("/health").json() == {"status": "ok"}


def test_validation_failure_produces_no_partial_results():
    # 非法请求除 422 文本外不返回任何结果体（无 affected_nodes）
    resp = client.post(
        "/dom-impact",
        json={"root": node("a", "p"), "rules": [rule("p >", decl("color", "red"))],
              "mutation": {"type": "set_tag", "node_id": "a", "tag": "div"}},
    )
    assert resp.status_code == 422
    assert "affected_nodes" not in resp.json()


def test_unaffected_node_and_no_impact_mutation():
    # title 不依赖任何被改变的量，绝不列出；操作虽对样式无影响但不属于操作级空操作，返回 200 空列表
    data = impact(payload(TREE, RULES, {"type": "add_class", "node_id": "title", "class_name": "unused"}))
    assert data["affected_nodes"] == []
    assert data["nodes_checked"] == 4
