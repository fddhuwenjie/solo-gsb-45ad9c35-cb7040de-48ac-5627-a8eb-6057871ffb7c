"""整树分析内核（kernel.py）的端到端测试。

四个入口（/compute、/compute 补丁预演、/stylesheet-audit、/dom-impact）
共享同一份规则解析与同一套节点签名，这里通过 HTTP 端到端覆盖：

- 多层嵌套继承（根声明、中间层遮蔽、深层纯继承 / var() 取值）；
- fallback 改道与重新走主分支（含嵌套 fallback 在两侧都不改值时不列出）；
- 循环依赖的形成与解除（失效原因变化也算差异，fallback 两侧同值不算）；
- 非法输入在各入口的 422；
- 三个比对入口共享同一签名：胜出者换人但值相同必列出，未改变节点不列出；
- 内核对象不可变、不修改入参。
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.kernel import RuleSet, analyze_chain
from app.main import app
from app.models import Declaration, ElementInfo, Rule

client = TestClient(app)


def post(path, payload, expected=200):
    resp = client.post(path, json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


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


# ---------- 多层嵌套继承 ----------


NESTED_TREE = node(
    "r", "div",
    children=[
        node("s", "section", children=[
            node("p", "p", children=[node("k", "span")])
        ])
    ],
)

NESTED_RULES = [
    rule("div", decl("--g", "10px")),       # 0
    rule("section", decl("--g", "20px")),   # 1：中间层遮蔽祖先
    rule("p", decl("margin", "var(--g)")),  # 2
    rule("span", decl("color", "var(--g)")),  # 3
]


def test_compute_nested_inheritance_uses_nearest_ancestor_override():
    data = post("/compute", {
        "element": node(None, "span") if False else {"tag": "span"},
        "ancestors": [{"tag": "div"}, {"tag": "section"}, {"tag": "p"}],
        "rules": NESTED_RULES,
    })
    # section 的 20px 遮蔽 div 的 10px，深层 span 取最近祖先值
    assert data["computed"] == {"color": "20px"}
    assert data["custom_properties"] == {"--g": "20px"}
    trace = {(t["element"], t["property"]): t for t in data["custom_property_trace"]}
    assert trace[("div", "--g")]["status"] == "ok"
    assert trace[("section", "--g")]["status"] == "ok"
    # p / span 两层均为纯继承
    assert trace[("p", "--g")]["status"] == "inherited"
    assert trace[("span", "--g")]["status"] == "inherited"
    assert trace[("span", "--g")]["resolved"] == "20px"


def test_audit_nested_inheritance_blocks_at_each_declaring_node():
    data = post("/stylesheet-audit", {"root": NESTED_TREE, "rules": NESTED_RULES})
    assert data["deleted"] == []
    retained = {(d["rule_index"], d["declaration_index"]): d for d in data["retained"]}
    # 祖先声明删除后首先在其自身节点冲突（先序最早），即使深层已被遮蔽
    assert retained[(0, 0)]["conflict_node"] == "r"
    assert retained[(0, 0)]["conflict_property"] == "--g"
    assert retained[(1, 0)]["conflict_node"] == "s"
    assert retained[(2, 0)]["conflict_node"] == "p"
    assert retained[(2, 0)]["conflict_property"] == "margin"
    assert retained[(3, 0)]["conflict_node"] == "k"
    assert retained[(3, 0)]["conflict_property"] == "color"


def test_dom_impact_nested_inheritance_propagates_through_all_levels():
    # 中间层 section 改标签后不再命中 --g:20px，全链退回 div 的 10px
    data = post("/dom-impact", {
        "root": NESTED_TREE,
        "rules": NESTED_RULES,
        "mutation": {"type": "set_tag", "node_id": "s", "tag": "footer"},
    })
    affected = {n["node_id"]: {c["property"]: c for c in n["changes"]}
                for n in data["affected_nodes"]}
    # 根 div 完全不受影响：不得列出
    assert "r" not in affected

    s_gap = affected["s"]["--g"]
    assert s_gap["reasons"] == ["selector_match", "cascade_winner", "inheritance"]
    assert (s_gap["before"]["value"], s_gap["after"]["value"]) == ("20px", "10px")
    assert s_gap["before"]["winner"] is not None and s_gap["after"]["winner"] is None

    assert affected["p"]["--g"]["reasons"] == ["inheritance"]
    assert affected["p"]["margin"]["reasons"] == ["var_chain"]
    assert (affected["p"]["margin"]["before"]["value"],
            affected["p"]["margin"]["after"]["value"]) == ("20px", "10px")

    assert affected["k"]["--g"]["reasons"] == ["inheritance"]
    assert affected["k"]["color"]["reasons"] == ["var_chain"]
    assert (affected["k"]["color"]["before"]["value"],
            affected["k"]["color"]["after"]["value"]) == ("20px", "10px")


# ---------- fallback 改道 / 重归主分支 ----------


def test_preview_fallback_reroute_and_unchanged_nested_fallback_not_listed():
    payload = {
        "element": {"tag": "p"},
        "rules": [
            rule("p", decl("--x", "4px")),
            rule("p",
                 decl("margin", "var(--x, 9px)"),
                 # 嵌套 fallback：两侧都走 fallback 且值恒为 3px -> 不构成差异
                 decl("padding", "var(--nope, var(--deep, 3px))")),
        ],
        "patches": [{"rule_index": 0, "declaration_index": 0,
                     "value": "var(--nope)"}],
    }
    data = post("/compute", payload)
    pv = data["preview"]
    assert (pv["baseline"]["computed"]["margin"],
            pv["patched"]["computed"]["margin"]) == ("4px", "9px")
    changes = {c["property"]: c for c in pv["changes"]}
    assert set(changes) == {"--x", "margin"}
    assert changes["margin"]["kind"] == "var_changed"
    assert "fallback 改道" in "\n".join(changes["margin"]["trail"])
    # padding 胜出声明与取值两侧一致，即使内部走过 fallback 也不列出
    assert "padding" not in changes


def test_dom_impact_fallback_rejoin_main_branch():
    # 基线变量有效走主分支；移除 class 后变量缺失改走 fallback
    tree = node("r", "div", classes=["on"], children=[node("p", "p")])
    rules = [
        rule("div.on", decl("--x", "1px")),
        rule("p", decl("color", "var(--x, red)")),
    ]
    data = post("/dom-impact", {
        "root": tree,
        "rules": rules,
        "mutation": {"type": "remove_class", "node_id": "r", "class_name": "on"},
    })
    affected = {n["node_id"]: {c["property"]: c for c in n["changes"]}
                for n in data["affected_nodes"]}
    ch = affected["p"]["color"]
    assert ch["reasons"] == ["var_chain"]
    assert (ch["before"]["value"], ch["after"]["value"]) == ("1px", "red")
    assert affected["p"]["--x"]["reasons"] == ["inheritance"]
    assert (affected["p"]["--x"]["before"]["status"],
            affected["p"]["--x"]["after"]["status"]) == ("ok", "missing")


def test_audit_fallback_provider_declaration_is_retained():
    # 提供 --x 的声明一旦删除，var(--x, 9px) 改走 fallback，取值 4px -> 9px
    tree = node("r", "div", children=[node("p", "p")])
    rules = [
        rule("div", decl("--x", "4px")),
        rule("p", decl("margin", "var(--x, 9px)")),
        rule("i", decl("--x", "0px")),  # 选择器不命中 -> 可删
    ]
    data = post("/stylesheet-audit", {"root": tree, "rules": rules})
    deleted = {(d["rule_index"], d["declaration_index"]) for d in data["deleted"]}
    retained = {(d["rule_index"], d["declaration_index"]): d for d in data["retained"]}
    assert deleted == {(2, 0)}
    assert retained[(0, 0)]["conflict_node"] == "r"
    assert retained[(0, 0)]["conflict_property"] == "--x"
    assert retained[(1, 0)]["conflict_node"] == "p"


# ---------- 循环解除 ----------


CYCLE_TREE = node(
    "r", "div", classes=["c"],
    children=[node("p", "p", children=[node("k", "span")])],
)

CYCLE_RULES = [
    rule("div.c", decl("--x", "var(--y)"), decl("--y", "var(--x)")),
    rule("p", decl("color", "var(--x)"), decl("margin", "var(--x, 7px)")),
]


def test_dom_impact_cycle_break_lists_reason_change_but_not_unchanged_fallback():
    data = post("/dom-impact", {
        "root": CYCLE_TREE,
        "rules": CYCLE_RULES,
        "mutation": {"type": "remove_class", "node_id": "r", "class_name": "c"},
    })
    affected = {n["node_id"]: {c["property"]: c for c in n["changes"]}
                for n in data["affected_nodes"]}

    # 根：循环声明整体退场
    r_changes = affected["r"]
    assert {"--x", "--y"} <= set(r_changes)
    assert (r_changes["--x"]["before"]["status"],
            r_changes["--x"]["after"]["status"]) == ("invalid", "missing")

    # p：color 两侧都 invalid、值都为 null，但失效原因（循环 -> 未定义）
    # 属于共享签名的第四分量，仍要列出
    color = affected["p"]["color"]
    assert color["reasons"] == ["var_chain"]
    assert color["before"]["status"] == color["after"]["status"] == "invalid"
    assert color["before"]["value"] is None and color["after"]["value"] is None
    assert "失效" in color["before"]["reason"]
    assert "未定义" in color["after"]["reason"]

    # margin 两侧都由 fallback 得 7px、胜出声明不变：不得列出
    assert "margin" not in affected["p"]

    # 孙代纯继承的失效变量随循环解除变为 missing
    assert (affected["k"]["--x"]["before"]["status"],
            affected["k"]["--x"]["after"]["status"]) == ("invalid", "missing")
    assert affected["k"]["--x"]["reasons"] == ["inheritance"]


def test_audit_cycle_declarations_all_retained():
    data = post("/stylesheet-audit", {"root": CYCLE_TREE, "rules": CYCLE_RULES})
    assert data["deleted"] == []
    retained = {(d["rule_index"], d["declaration_index"]) for d in data["retained"]}
    # 两条循环声明 + color / margin 的胜出声明都必须保留
    assert retained == {(0, 0), (0, 1), (1, 0), (1, 1)}
    by_loc = {(d["rule_index"], d["declaration_index"]): d for d in data["retained"]}
    assert by_loc[(0, 0)]["conflict_node"] == "r"
    assert by_loc[(1, 0)]["conflict_node"] == "p"


def test_preview_cycle_break_restores_values_through_chain():
    payload = {
        "element": {"tag": "p"},
        "ancestors": [{"tag": "div", "classes": ["c"]}],
        "rules": [
            rule("div.c", decl("--x", "var(--y)"), decl("--y", "var(--x)")),
            rule("p", decl("color", "var(--x)"), decl("margin", "var(--x, 7px)")),
        ],
        "patches": [{"rule_index": 0, "declaration_index": 0, "value": "5px"}],
    }
    data = post("/compute", payload)
    pv = data["preview"]
    assert pv["baseline"]["computed"]["color"] is None
    assert pv["patched"]["computed"]["color"] == "5px"
    assert (pv["baseline"]["computed"]["margin"],
            pv["patched"]["computed"]["margin"]) == ("7px", "5px")
    changes = {c["property"]: c for c in pv["changes"]}
    assert changes["color"]["kind"] == "var_changed"
    assert "循环依赖" in "\n".join(changes["color"]["trail"])


# ---------- 三个入口共享同一签名：同值换胜出者必列出 ----------


def test_same_value_winner_swap_listed_by_all_three_entries():
    # 审计：胜出者删除后继任者同值也阻断
    audit_data = post("/stylesheet-audit", {
        "root": node("r", "p"),
        "rules": [rule("p", decl("color", "red")), rule("p", decl("color", "red"))],
    })
    retained = {(d["rule_index"], d["declaration_index"]): d for d in audit_data["retained"]}
    deleted = {(d["rule_index"], d["declaration_index"]) for d in audit_data["deleted"]}
    assert deleted == {(0, 0)}
    assert retained[(1, 0)]["reason"] == "winner_changed"

    # DOM 变更：新增 class 使另一条同值声明胜出，也必须列出
    impact_data = post("/dom-impact", {
        "root": node("r", "p"),
        "rules": [rule("p", decl("color", "red")), rule("p.x", decl("color", "red"))],
        "mutation": {"type": "add_class", "node_id": "r", "class_name": "x"},
    })
    ch = impact_data["affected_nodes"][0]["changes"][0]
    assert ch["before"]["value"] == ch["after"]["value"] == "red"
    assert ch["reasons"] == ["selector_match", "cascade_winner"]
    assert ch["before"]["winner"]["rule_index"] != ch["after"]["winner"]["rule_index"]

    # 预演：important 补丁使同值的另一条声明夺魁，同样列出
    preview_data = post("/compute", {
        "element": {"tag": "p"},
        "rules": [rule("p", decl("color", "red")), rule("p", decl("color", "red"))],
        "patches": [{"rule_index": 0, "declaration_index": 0, "important": True}],
    })
    changes = {c["property"]: c for c in preview_data["preview"]["changes"]}
    assert set(changes) == {"color"}
    assert changes["color"]["kind"] == "winner_changed"
    assert changes["color"]["before_value"] == changes["color"]["after_value"] == "red"


def test_unchanged_nodes_never_listed_even_with_unrelated_candidates():
    # 添加一个没有任何规则使用的 class：合法但整树无变化，affected 为空
    data = post("/dom-impact", {
        "root": node("r", "div", children=[node("a", "p")]),
        "rules": [rule("p", decl("color", "red"))],
        "mutation": {"type": "add_class", "node_id": "r", "class_name": "unused"},
    })
    assert data["affected_nodes"] == []
    assert data["nodes_checked"] == 2


# ---------- 非法输入（各入口 422） ----------


def test_compute_invalid_inputs_return_422():
    post("/compute", {"element": {"tag": "p"},
                      "rules": [rule("div >>", decl("color", "red"))]}, expected=422)
    post("/compute", {
        "element": {"tag": "p"},
        "rules": [rule("p", decl("color", "red"))],
        "patches": [{"rule_index": 9, "declaration_index": 0, "value": "x"}],
    }, expected=422)


def test_audit_invalid_inputs_return_422():
    post("/stylesheet-audit", {
        "root": node("a", "div", children=[node("a", "p")]), "rules": [],
    }, expected=422)
    post("/stylesheet-audit", {
        "root": node("a", "div"), "rules": [rule("div", decl(" ", "red"))],
    }, expected=422)
    post("/stylesheet-audit", {
        "root": node("a", "div"), "rules": [rule(">>", decl("color", "red"))],
    }, expected=422)
    post("/stylesheet-audit", {
        "root": {"id": True, "tag": "div"}, "rules": [],
    }, expected=422)
    post("/stylesheet-audit", {
        "root": {"id": "a", "tag": "div", "children": None}, "rules": [],
    }, expected=422)


def test_dom_impact_invalid_inputs_return_422():
    base_tree = node("a", "p", classes=["x"], attributes={"d": "1"})
    base = {"root": base_tree, "rules": []}

    def expect_422(mutation, rules=None):
        payload = {"root": base_tree, "mutation": mutation}
        payload["rules"] = rules or []
        post("/dom-impact", payload, expected=422)

    expect_422({"type": "add_class", "node_id": "zzz", "class_name": "y"})
    expect_422({"type": "add_class", "node_id": "a", "class_name": "x"})  # 空操作
    expect_422({"type": "add_class", "node_id": "a"})  # 缺 class_name
    expect_422({"type": "set_attribute", "node_id": "a", "name": "id", "value": "b"})
    expect_422({"type": "set_tag", "node_id": "a", "tag": " "})
    expect_422({"type": "set_tag", "node_id": "a", "tag": "div"},
               rules=[rule("p >", decl("color", "red"))])
    expect_422({"type": "set_tag", "node_id": "a", "tag": "div"},
               rules=[rule("p", decl("   ", "red"))])


# ---------- 内核不可变性 ----------


def test_kernel_results_are_immutable_and_inputs_are_not_mutated():
    rules = [
        Rule(selector="div", declarations=[Declaration(property="--g", value="4px")]),
        Rule(selector="p", declarations=[Declaration(property="margin", value="var(--g)")]),
    ]
    rules_before = rules[0].model_dump()
    rs = RuleSet.parse(rules)
    div = ElementInfo(tag="div")
    node_ = analyze_chain(ElementInfo(tag="p"), [div], rs)

    # 结果对象 frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        node_.normals = {}  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        rs.disabled = frozenset({(0, 0)})  # type: ignore[misc]

    # with_changes 返回新规则表，原表不受影响
    rs2 = RuleSet.parse(rules).with_changes(disabled=frozenset({(0, 0)}))
    assert rs.disabled == frozenset()
    assert (0, 0) in rs2.disabled

    # 入参规则未被修改
    assert rules[0].model_dump() == rules_before
    assert node_.computed == {"margin": "4px"}
