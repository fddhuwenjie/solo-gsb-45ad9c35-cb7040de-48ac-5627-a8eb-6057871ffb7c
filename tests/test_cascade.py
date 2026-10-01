"""端到端测试：通过 FastAPI TestClient 打 POST /compute。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

P = {"tag": "p"}


def compute(payload):
    resp = client.post("/compute", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def base_payload(**overrides):
    payload = {"element": P, "ancestors": [], "layers": [], "rules": []}
    payload.update(overrides)
    return payload


def rule(selector, *decls, origin="author", layer=None, order=None):
    r = {
        "selector": selector,
        "origin": origin,
        "layer": layer,
        "declarations": [
            {"property": p, "value": v, "important": imp} for p, v, imp in decls
        ],
    }
    if order is not None:
        r["order"] = order
    return r


def decl(prop, value, important=False):
    return (prop, value, important)


# ---------- 来源（origin） ----------


def test_origin_order_author_beats_user_beats_ua():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("color", "black"), origin="ua"),
                rule("p", decl("color", "gray"), origin="user"),
                rule("p", decl("color", "red"), origin="author"),
            ]
        )
    )
    assert data["computed"]["color"] == "red"
    stages = {e["stage"] for e in data["evidence"][0]["eliminated"]}
    assert stages == {"origin"}


def test_ua_beats_author_when_no_author_declaration():
    data = compute(
        base_payload(rules=[rule("p", decl("color", "black"), origin="ua")])
    )
    assert data["computed"]["color"] == "black"


# ---------- important 反转 ----------


def test_important_reverses_origin_order():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("color", "red", True), origin="author"),
                rule("p", decl("color", "black", True), origin="ua"),
            ]
        )
    )
    assert data["computed"]["color"] == "black"
    elim = data["evidence"][0]["eliminated"][0]
    assert elim["stage"] == "origin"
    assert "反转" in elim["detail"]


def test_important_beats_normal_regardless_of_specificity():
    el = {"tag": "p", "id": "x", "classes": ["y"]}
    data = compute(
        base_payload(
            element=el,
            rules=[
                rule("p#x.y", decl("color", "red")),
                rule("p", decl("color", "blue", True), origin="ua"),
            ],
        )
    )
    assert data["computed"]["color"] == "blue"
    assert data["evidence"][0]["eliminated"][0]["stage"] == "importance"


# ---------- 层（layer） ----------


def test_unlayered_beats_layered_for_normal():
    data = compute(
        base_payload(
            layers=["base"],
            rules=[
                rule("p", decl("color", "blue"), layer="base"),
                rule("p", decl("color", "red")),
            ],
        )
    )
    assert data["computed"]["color"] == "red"
    elim = data["evidence"][0]["eliminated"][0]
    assert elim["stage"] == "layer"
    assert "未分层" in elim["detail"]


def test_later_layer_beats_earlier_layer():
    data = compute(
        base_payload(
            layers=["reset", "base", "components"],
            rules=[
                rule("p", decl("color", "green"), layer="components"),
                rule("p", decl("color", "blue"), layer="base"),
                rule("p", decl("color", "gray"), layer="reset"),
            ],
        )
    )
    assert data["computed"]["color"] == "green"


def test_important_reverses_layer_order():
    # important：分层高于未分层，早层高于晚层
    data = compute(
        base_payload(
            layers=["early", "late"],
            rules=[
                rule("p", decl("color", "unlayered", True)),
                rule("p", decl("color", "late", True), layer="late"),
                rule("p", decl("color", "early", True), layer="early"),
            ],
        )
    )
    assert data["computed"]["color"] == "early"
    details = [e["detail"] for e in data["evidence"][0]["eliminated"]]
    assert any("早层高于晚层" in d for d in details)
    assert any("分层高于未分层" in d for d in details)


def test_undeclared_layer_appended_after_declared():
    data = compute(
        base_payload(
            layers=["base"],
            rules=[
                rule("p", decl("color", "blue"), layer="base"),
                rule("p", decl("color", "red"), layer="extra"),  # 未声明，视为更晚
            ],
        )
    )
    assert data["computed"]["color"] == "red"


# ---------- 特异性与源码次序 ----------


def test_specificity_id_beats_class_beats_tag():
    el = {"tag": "p", "id": "x", "classes": ["y"]}
    data = compute(
        base_payload(
            element=el,
            rules=[
                rule("p", decl("color", "tag")),
                rule(".y", decl("color", "class")),
                rule("#x", decl("color", "id")),
            ],
        )
    )
    assert data["computed"]["color"] == "id"
    stages = [e["stage"] for e in data["evidence"][0]["eliminated"]]
    assert stages == ["specificity", "specificity"]


def test_attr_selector_counts_as_class_level():
    el = {"tag": "p", "classes": ["lead"], "attributes": {"data-role": "lead"}}
    data = compute(
        base_payload(
            element=el,
            rules=[
                rule("p.lead", decl("color", "class"), ),
                rule("p[data-role=lead]", decl("color", "attr")),
            ],
        )
    )
    # 特异性相同 (0,1,1)，源码次序靠后者胜
    assert data["computed"]["color"] == "attr"
    assert data["evidence"][0]["eliminated"][0]["stage"] == "source_order"


def test_source_order_later_wins_and_explicit_order():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("color", "first")),
                rule("p", decl("color", "second")),
            ]
        )
    )
    assert data["computed"]["color"] == "second"
    # 显式 order 覆盖数组顺序
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("color", "first"), order=10),
                rule("p", decl("color", "second"), order=5),
            ]
        )
    )
    assert data["computed"]["color"] == "first"


def test_later_declaration_in_same_rule_wins():
    data = compute(
        base_payload(rules=[rule("p", decl("color", "red"), decl("color", "blue"))])
    )
    assert data["computed"]["color"] == "blue"


# ---------- 选择器匹配 ----------


def test_descendant_and_child_combinators():
    ancestors = [{"tag": "html"}, {"tag": "body"}, {"tag": "div", "id": "app"}]
    data = compute(
        base_payload(
            ancestors=ancestors,
            rules=[
                rule("body p", decl("margin", "desc")),
                rule("div > p", decl("padding", "child")),
                rule("html > p", decl("border", "no-match")),
            ],
        )
    )
    assert data["computed"]["margin"] == "desc"
    assert data["computed"]["padding"] == "child"
    assert "border" not in data["computed"]


def test_child_combinator_rejects_grandchild():
    ancestors = [{"tag": "div"}, {"tag": "section"}]
    data = compute(
        base_payload(
            ancestors=ancestors,
            rules=[rule("div > p", decl("color", "red"))],
        )
    )
    assert "color" not in data["computed"]


def test_attr_presence_and_quoted_value():
    el = {"tag": "input", "attributes": {"type": "text", "disabled": ""}}
    data = compute(
        base_payload(
            element=el,
            rules=[
                rule('[type="text"]', decl("color", "quoted")),
                rule("[disabled]", decl("cursor", "present")),
                rule("[type=password]", decl("background", "no")),
            ],
        )
    )
    assert data["computed"]["color"] == "quoted"
    assert data["computed"]["cursor"] == "present"
    assert "background" not in data["computed"]


def test_invalid_selector_returns_422():
    resp = client.post(
        "/compute",
        json=base_payload(rules=[rule("div >> p", decl("color", "red"))]),
    )
    assert resp.status_code == 422


# ---------- 自定义属性与 var() ----------


def test_custom_property_inherits_then_overridden_by_cascade():
    ancestors = [{"tag": "body"}]
    data = compute(
        base_payload(
            ancestors=ancestors,
            rules=[
                rule("body", decl("--gap", "10px"), decl("--tone", "blue")),
                rule("p", decl("--gap", "20px")),
                rule("p", decl("margin", "var(--gap)")),
                rule("p", decl("color", "var(--tone)")),
            ],
        )
    )
    assert data["computed"]["margin"] == "20px"  # 本元素覆盖继承值
    assert data["computed"]["color"] == "blue"  # 未覆盖则继承
    assert data["custom_properties"] == {"--gap": "20px", "--tone": "blue"}


def test_custom_property_cascade_respects_layers():
    data = compute(
        base_payload(
            layers=["base"],
            rules=[
                rule("p", decl("--x", "layered"), layer="base"),
                rule("p", decl("--x", "unlayered")),
                rule("p", decl("width", "var(--x)")),
            ],
        )
    )
    assert data["computed"]["width"] == "unlayered"


def test_var_fallback_used_when_missing():
    data = compute(
        base_payload(rules=[rule("p", decl("margin", "var(--missing, 5px)"))])
    )
    assert data["computed"]["margin"] == "5px"
    assert data["invalid"] == []
    assert any("fallback" in s for s in data["evidence"][0]["var_steps"])


def test_var_missing_without_fallback_is_invalid():
    data = compute(
        base_payload(rules=[rule("p", decl("margin", "var(--missing)"))])
    )
    assert data["computed"]["margin"] is None
    assert data["invalid"][0]["property"] == "margin"
    assert "未定义" in data["invalid"][0]["reason"]


def test_var_nested_fallback_and_chain():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("--b", "var(--c, 3px)")),
                rule("p", decl("margin", "var(--a, var(--b, 1px))")),
            ],
        )
    )
    assert data["computed"]["margin"] == "3px"


def test_cyclic_custom_properties_become_invalid():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("--a", "var(--b)")),
                rule("p", decl("--b", "var(--a)")),
                rule("p", decl("color", "var(--a)")),
                rule("p", decl("margin", "var(--a, 1px)")),
            ],
        )
    )
    assert data["computed"]["color"] is None
    assert data["computed"]["margin"] == "1px"  # fallback 兜底
    assert "循环依赖" in data["invalid"][0]["reason"] or "失效" in data["invalid"][0]["reason"]
    trace = {t["property"]: t for t in data["custom_property_trace"]}
    assert trace["--a"]["status"] == "invalid"
    assert "循环依赖" in trace["--a"]["note"]


def test_self_reference_is_a_cycle():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("--a", "var(--a)")),
                rule("p", decl("color", "var(--a)")),
            ]
        )
    )
    assert data["computed"]["color"] is None
    assert data["invalid"][0]["property"] == "color"


def test_var_in_custom_property_value_resolved_at_computed_time():
    ancestors = [{"tag": "body"}]
    data = compute(
        base_payload(
            ancestors=ancestors,
            rules=[
                rule("body", decl("--base", "4px")),
                rule("p", decl("--double", "calc(var(--base) * 2)")),
                rule("p", decl("margin", "var(--double)")),
            ],
        )
    )
    assert data["computed"]["margin"] == "calc(4px * 2)"


def test_custom_property_names_are_case_sensitive():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("--Gap", "10px")),
                rule("p", decl("margin", "var(--gap, 1px)")),
            ],
        )
    )
    assert data["computed"]["margin"] == "1px"


# ---------- 证据结构 ----------


def test_evidence_lists_all_losers_with_stages():
    el = {"tag": "p", "id": "x"}
    data = compute(
        base_payload(
            element=el,
            layers=["base"],
            rules=[
                rule("p", decl("color", "ua"), origin="ua"),
                rule("p", decl("color", "layered"), layer="base"),
                rule("p", decl("color", "unlayered")),
                rule("#x", decl("color", "id")),
            ],
        )
    )
    ev = data["evidence"][0]
    assert data["computed"]["color"] == "id"
    assert ev["winner"]["selector"] == "#x"
    # 落选者按层叠排序从高到低：未分层 p（特异性负）、分层 p（层负）、ua p（来源负）
    stages = [e["stage"] for e in ev["eliminated"]]
    assert stages == ["specificity", "layer", "origin"]
    assert len(ev["eliminated"]) == 3


def test_full_example_from_readme():
    payload = {
        "element": {"tag": "p", "id": "intro", "classes": ["text"], "attributes": {"data-role": "lead"}},
        "ancestors": [
            {"tag": "html"},
            {"tag": "body", "classes": ["dark"]},
            {"tag": "div", "id": "app"},
        ],
        "layers": ["reset", "base", "components"],
        "rules": [
            {"origin": "ua", "selector": "p", "declarations": [
                {"property": "color", "value": "black"},
                {"property": "margin", "value": "1em 0"},
            ]},
            {"origin": "author", "layer": "reset", "selector": "p", "declarations": [
                {"property": "margin", "value": "0"},
            ]},
            {"origin": "author", "layer": "base", "selector": "div p", "declarations": [
                {"property": "color", "value": "blue"},
                {"property": "--gap", "value": "8px"},
            ]},
            {"origin": "author", "selector": "#app > p.text", "declarations": [
                {"property": "color", "value": "red"},
                {"property": "padding", "value": "var(--gap, 4px)"},
            ]},
            {"origin": "author", "layer": "components", "selector": "p[data-role=lead]", "declarations": [
                {"property": "color", "value": "green", "important": True},
            ]},
        ],
    }
    data = compute(payload)
    assert data["computed"]["color"] == "green"  # important 压过未分层高特异性
    assert data["computed"]["margin"] == "0"  # author 分层 > ua
    assert data["computed"]["padding"] == "8px"  # var() 命中继承链上的自定义属性
    assert data["custom_properties"]["--gap"] == "8px"
