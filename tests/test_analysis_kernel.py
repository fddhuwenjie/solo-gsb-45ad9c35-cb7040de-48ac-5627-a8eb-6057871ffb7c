"""整树分析内核一致性的端到端测试。

/compute（含 patches 预演）、/stylesheet-audit、/dom-impact 共享同一个
不可变整树分析内核（app/analysis.py）与同一节点状态签名（胜出声明定位 +
状态 + 取值 + 失效原因）。本文件覆盖：嵌套继承、fallback 改道、循环形成 /
解除、同值换胜出者在三个入口的一致判定、未改变节点不列入结果、内核的
一次解析与不可变性，以及非法输入。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def decl(prop, value, important=False):
    d = {"property": prop, "value": value}
    if important:
        d["important"] = True
    return d


def rule(selector, *decls, layer=None, order=None, origin="author"):
    r = {
        "selector": selector,
        "origin": origin,
        "layer": layer,
        "declarations": list(decls),
    }
    if order is not None:
        r["order"] = order
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


def audit(payload, expected=200):
    resp = client.post("/stylesheet-audit", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def impact(payload, expected=200):
    resp = client.post("/dom-impact", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def compute(payload, expected=200):
    resp = client.post("/compute", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def changes_by_id(data):
    return {n["node_id"]: {c["property"]: c for c in n["changes"]} for n in data["affected_nodes"]}


def retained_map(data):
    return {(d["rule_index"], d["declaration_index"]): d for d in data["retained"]}


def deleted_locs(data):
    return {(d["rule_index"], d["declaration_index"]) for d in data["deleted"]}


# ---------- 同值换胜出者：三个入口共享同一签名 ----------


def test_same_value_winner_change_consistent_across_endpoints():
    # 两条规则取值相同、特异性不同：p.x 胜出。“胜出者改变但值相同”在
    # 审计 / DOM 变更 / 补丁预演中都必须计为变化（共享签名含胜出定位）
    rules = [
        rule("p", decl("color", "red")),
        rule("p.x", decl("color", "red")),
    ]

    # 1) 审计：落选者可删；删除胜出者即使继任者同值也被阻断
    audit_data = audit({"root": node("r", "p", classes=["x"]), "rules": rules})
    assert deleted_locs(audit_data) == {(0, 0)}
    keeps = retained_map(audit_data)
    assert keeps[(1, 0)]["reason"] == "winner_changed"
    assert keeps[(1, 0)]["conflict_node"] == "r"

    # 2) DOM 变更：添加 class x 使胜出者换人（值不变），节点必须列出
    impact_data = impact({
        "root": node("r", "p"),
        "rules": rules,
        "mutation": {"type": "add_class", "node_id": "r", "class_name": "x"},
    })
    assert [n["node_id"] for n in impact_data["affected_nodes"]] == ["r"]
    ch = impact_data["affected_nodes"][0]["changes"][0]
    assert ch["property"] == "color"
    assert ch["reasons"] == ["selector_match", "cascade_winner"]
    assert ch["before"]["value"] == ch["after"]["value"] == "red"
    assert ch["before"]["winner"]["rule_index"] == 0
    assert ch["after"]["winner"]["rule_index"] == 1

    # 3) 补丁预演：important 补丁使胜出者换人（值不变），必须报告
    #    winner_changed 且归属该补丁，不得误判为未生效（shadowed）
    comp = compute({
        "element": {"tag": "p", "classes": ["x"]},
        "rules": rules,
        "patches": [{"rule_index": 0, "declaration_index": 0, "important": True}],
    })
    pv = comp["preview"]
    assert pv["baseline"]["computed"]["color"] == "red"
    assert pv["patched"]["computed"]["color"] == "red"
    assert len(pv["changes"]) == 1
    ch = pv["changes"][0]
    assert ch["property"] == "color" and ch["kind"] == "winner_changed"
    assert ch["before_value"] == ch["after_value"] == "red"
    assert ch["before_winner"]["rule_index"] == 1
    assert ch["after_winner"]["rule_index"] == 0
    assert [p["index"] for p in ch["responsible_patches"]] == [0]
    assert pv["shadowed_patches"] == []


# ---------- 嵌套继承 ----------


def test_compute_nested_inheritance_chain():
    # 自定义属性沿三层祖先链继承并逐级经 var() 串联
    data = compute({
        "element": {"tag": "span"},
        "ancestors": [{"tag": "div"}, {"tag": "section"}, {"tag": "p"}],
        "rules": [
            rule("div", decl("--a", "3px")),
            rule("section", decl("--b", "var(--a)")),
            rule("p", decl("--c", "var(--b)")),
            rule("span", decl("margin", "var(--c)")),
        ],
    })
    assert data["computed"]["margin"] == "3px"
    assert data["custom_properties"] == {"--a": "3px", "--b": "3px", "--c": "3px"}


def test_nested_inheritance_propagates_through_tree_and_skips_unaffected():
    # 顶层变更沿「继承 -> var() 链 -> 再继承」传导三层；自身未变的节点
    # （top、other）不列入结果
    root = node(
        "top", "div",
        children=[
            node(
                "gp", "div", classes=["wrap"],
                children=[
                    node(
                        "mid", "section",
                        children=[
                            node("leaf", "p", children=[node("txt", "span")]),
                        ],
                    ),
                ],
            ),
            node("other", "aside"),
        ],
    )
    rules = [
        rule("div", decl("--a", "2px")),
        rule("div.hot", decl("--a", "9px")),
        rule("section", decl("--b", "var(--a)")),
        rule("p", decl("margin", "var(--b)"), decl("--c", "var(--b)")),
        rule("span", decl("color", "var(--c, blue)")),
    ]
    data = impact({
        "root": root,
        "rules": rules,
        "mutation": {"type": "add_class", "node_id": "gp", "class_name": "hot"},
    })
    assert data["nodes_checked"] == 6
    by_id = changes_by_id(data)
    # top 自身 --a 仍由 div 规则给出（2px 未变），other 无任何相关声明：均不列出
    assert set(by_id) == {"gp", "mid", "leaf", "txt"}

    gp = by_id["gp"]
    assert gp["--a"]["reasons"] == ["selector_match", "cascade_winner"]
    assert (gp["--a"]["before"]["value"], gp["--a"]["after"]["value"]) == ("2px", "9px")

    mid = by_id["mid"]
    assert mid["--a"]["reasons"] == ["inheritance"]
    assert mid["--b"]["reasons"] == ["var_chain"]  # 本元素有胜出声明，不标 inheritance
    assert mid["--b"]["after"]["value"] == "9px"

    leaf = by_id["leaf"]
    assert leaf["--a"]["reasons"] == ["inheritance"]
    assert leaf["--b"]["reasons"] == ["inheritance"]
    assert leaf["--c"]["reasons"] == ["var_chain"]
    assert leaf["margin"]["reasons"] == ["var_chain"]
    assert leaf["margin"]["after"]["value"] == "9px"

    txt = by_id["txt"]
    assert txt["--c"]["reasons"] == ["inheritance"]
    assert txt["color"]["reasons"] == ["var_chain"]
    assert (txt["color"]["before"]["value"], txt["color"]["after"]["value"]) == ("2px", "9px")


# ---------- fallback 改道 ----------


def test_fallback_reroute_and_rejoin_via_mutation():
    rules = [
        rule("div.on", decl("--x", "5px")),
        rule("p", decl("margin", "var(--x, 1px)")),
    ]
    # 移除 on：--x 从链上消失，margin 由主分支改道 fallback
    data = impact({
        "root": node("r", "div", classes=["on"], children=[node("k", "p")]),
        "rules": rules,
        "mutation": {"type": "remove_class", "node_id": "r", "class_name": "on"},
    })
    by_id = changes_by_id(data)
    assert set(by_id) == {"r", "k"}
    assert by_id["r"]["--x"]["reasons"] == ["selector_match", "cascade_winner"]
    assert (by_id["r"]["--x"]["before"]["status"], by_id["r"]["--x"]["after"]["status"]) == (
        "ok",
        "missing",
    )
    assert by_id["k"]["--x"]["reasons"] == ["inheritance"]
    margin = by_id["k"]["margin"]
    assert margin["reasons"] == ["var_chain"]
    assert (margin["before"]["value"], margin["after"]["value"]) == ("5px", "1px")
    assert "fallback" not in margin["before"]["reason"] if margin["before"]["reason"] else True

    # 反向：加回 on，fallback 重新并回主分支
    data2 = impact({
        "root": node("r", "div", children=[node("k", "p")]),
        "rules": rules,
        "mutation": {"type": "add_class", "node_id": "r", "class_name": "on"},
    })
    margin2 = changes_by_id(data2)["k"]["margin"]
    assert margin2["reasons"] == ["var_chain"]
    assert (margin2["before"]["value"], margin2["after"]["value"]) == ("1px", "5px")


def test_audit_blocks_fallback_reroute():
    # 落选的 --x 声明可删（主分支不受影响）；删除胜出声明会让 --x 消失、
    # color 改道 fallback -> 阻断
    root = node("r", "div", children=[node("k", "p")])
    rules = [
        rule("div", decl("--x", "3px"), decl("--x", "4px")),  # 同规则后者胜出
        rule("p", decl("color", "var(--x, red)")),
    ]
    data = audit({"root": root, "rules": rules})
    assert deleted_locs(data) == {(0, 0)}
    keeps = retained_map(data)
    # 删除胜出者后 --x 在根节点消失（先序首个冲突），color 随之改道 fallback
    assert keeps[(0, 1)]["conflict_node"] == "r"
    assert keeps[(0, 1)]["conflict_property"] == "--x"
    assert keeps[(0, 1)]["reason"] == "winner_changed"
    assert keeps[(1, 0)]["conflict_property"] == "color"


# ---------- 循环形成与解除 ----------


def test_cycle_resolution_and_formation_via_mutation():
    rules = [
        rule("p", decl("--a", "1px")),
        rule("p.c", decl("--a", "var(--b)"), decl("--b", "var(--a)")),
        rule("p", decl("color", "var(--a, 7px)")),
    ]
    # 基线带 class c：--a/--b 成环失效，color 走 fallback 7px；
    # 移除 c 后环解除：--a 恢复 1px，--b 消失，color 回到主分支
    data = impact({
        "root": node("r", "p", classes=["c"]),
        "rules": rules,
        "mutation": {"type": "remove_class", "node_id": "r", "class_name": "c"},
    })
    r = changes_by_id(data)["r"]
    assert (r["--a"]["before"]["status"], r["--a"]["after"]["status"]) == ("invalid", "ok")
    assert r["--a"]["after"]["value"] == "1px"
    assert r["--a"]["reasons"] == ["selector_match", "cascade_winner"]
    assert (r["--b"]["before"]["status"], r["--b"]["after"]["status"]) == ("invalid", "missing")
    color = r["color"]
    assert color["reasons"] == ["var_chain"]
    assert (color["before"]["value"], color["after"]["value"]) == ("7px", "1px")

    # 反向：添加 c 形成循环，--a 失效，color 改道 fallback
    data2 = impact({
        "root": node("r", "p"),
        "rules": rules,
        "mutation": {"type": "add_class", "node_id": "r", "class_name": "c"},
    })
    r2 = changes_by_id(data2)["r"]
    assert (r2["--a"]["before"]["status"], r2["--a"]["after"]["status"]) == ("ok", "invalid")
    assert (r2["color"]["before"]["value"], r2["color"]["after"]["value"]) == ("1px", "7px")


def test_audit_blocks_declaration_whose_removal_resolves_cycle():
    # 循环中的声明：删除任一都会解除循环或改变失效原因 -> 全部保留
    root = node("r", "div")
    rules = [
        rule("div", decl("--a", "var(--b)")),
        rule("div", decl("--b", "var(--a)")),
        rule("div", decl("color", "var(--a)")),
    ]
    data = audit({"root": root, "rules": rules})
    assert data["deleted"] == []
    keeps = retained_map(data)
    # 删 (0,0)：--a 从链上消失（missing）
    assert keeps[(0, 0)]["conflict_property"] == "--a"
    assert keeps[(0, 0)]["reason"] == "winner_changed"
    # 删 (1,0)：--b 消失后 --a 仍为 invalid，但失效原因由循环依赖变为
    # var() 解析失败——签名含失效原因，同样阻断
    assert keeps[(1, 0)]["conflict_property"] == "--a"
    assert keeps[(1, 0)]["reason"] == "reason_changed"
    assert "循环依赖" in keeps[(1, 0)]["detail"]
    # 删 (2,0)：color 由 invalid 变为无胜出声明
    assert keeps[(2, 0)]["reason"] == "winner_changed"


def test_preview_cycle_break_restores_main_branch():
    # 补丁把 --a 改为字面值：基线成环失效，补丁后环解除并回主分支
    data = compute({
        "element": {"tag": "p"},
        "rules": [
            rule(
                "p",
                decl("--a", "var(--b)"),
                decl("--b", "var(--a)"),
                decl("color", "var(--a, 7px)"),
            )
        ],
        "patches": [{"rule_index": 0, "declaration_index": 0, "value": "2px"}],
    })
    pv = data["preview"]
    assert pv["baseline"]["computed"]["color"] == "7px"
    assert pv["patched"]["computed"]["color"] == "2px"
    cm = {c["property"]: c for c in pv["changes"]}
    assert cm["--a"]["before_status"] == "invalid"
    assert cm["--a"]["after_status"] == "ok"
    assert cm["--b"]["after_status"] == "ok"
    assert cm["--b"]["after_value"] == "2px"
    assert cm["color"]["kind"] == "var_changed"
    assert (cm["color"]["before_value"], cm["color"]["after_value"]) == ("7px", "2px")
    assert "循环依赖" in "\n".join(cm["color"]["trail"])


# ---------- 内核：一次解析、不可变、共享签名 ----------


def test_audit_parses_each_selector_once(monkeypatch):
    import app.analysis as analysis

    parsed = []
    real = analysis.parse_selector

    def spy(selector):
        parsed.append(selector)
        return real(selector)

    monkeypatch.setattr(analysis, "parse_selector", spy)
    root = node("r", "div", children=[node("a", "p")])
    rules = [
        rule("div", decl("color", "red")),
        rule("p", decl("color", "blue"), decl("margin", "0")),
    ]
    resp = client.post("/stylesheet-audit", json={"root": root, "rules": rules})
    assert resp.status_code == 200, resp.text
    # 每条选择器只解析一次：逐条试删的整树复算共享同一 RuleSet
    assert sorted(parsed) == ["div", "p"]


def test_kernel_immutable_and_shared_signature():
    from app.analysis import RuleSet, SolveOptions, solve_forest
    from app.models import Declaration, ElementInfo, Rule

    ruleset = RuleSet.build(
        [
            Rule(selector="div", declarations=[Declaration(property="--g", value="4px")]),
            Rule(selector="p", declarations=[Declaration(property="color", value="var(--g)")]),
        ],
        [],
    )
    chains = [
        (ElementInfo(tag="div"), []),
        (ElementInfo(tag="p"), [ElementInfo(tag="div")]),
    ]
    base = solve_forest(ruleset, chains)
    trial = solve_forest(ruleset, chains, SolveOptions(disabled=frozenset({(0, 0)})))

    # 一次解析：复算变体共享同一规则集
    assert base.ruleset is trial.ruleset
    assert base.signature() != trial.signature()

    leaf = base.nodes[1]
    assert leaf.computed() == {"color": "4px"}
    assert leaf.custom_properties() == {"--g": "4px"}

    # 共享签名：胜出定位 + 状态 + 取值 + 失效原因
    sig = leaf.signature()
    normal = dict(sig.normal)
    custom = dict(sig.custom)
    assert normal["color"].winner == (1, 0)
    assert normal["color"].status == "computed"
    assert custom["--g"].winner is None  # 纯继承，本元素无胜出声明
    assert custom["--g"].value == "4px"

    # 不可变：冻结 dataclass 与只读映射都不允许写入
    with pytest.raises(Exception):
        leaf.normals["color"] = None
    with pytest.raises(Exception):
        leaf.depths = ()
    with pytest.raises(Exception):
        base.nodes = ()


# ---------- 非法输入（三个入口同一校验口径） ----------


def test_422_bad_selector_shared_by_all_entries():
    bad_rules = [rule("div >> p", decl("color", "red"))]
    assert client.post(
        "/compute", json={"element": {"tag": "p"}, "rules": bad_rules}
    ).status_code == 422
    assert client.post(
        "/stylesheet-audit", json={"root": node("a", "p"), "rules": bad_rules}
    ).status_code == 422
    assert client.post(
        "/dom-impact",
        json={
            "root": node("a", "p"),
            "rules": bad_rules,
            "mutation": {"type": "set_tag", "node_id": "a", "tag": "div"},
        },
    ).status_code == 422


def test_422_audit_children_null_and_bool_id():
    assert client.post(
        "/stylesheet-audit",
        json={"root": {"id": "a", "tag": "div", "children": None}, "rules": []},
    ).status_code == 422
    assert client.post(
        "/stylesheet-audit", json={"root": {"id": True, "tag": "div"}, "rules": []}
    ).status_code == 422


def test_422_audit_blank_property():
    resp = client.post(
        "/stylesheet-audit",
        json={"root": node("a", "p"), "rules": [rule("p", decl("  ", "red"))]},
    )
    assert resp.status_code == 422
    assert "property" in resp.text


def test_422_dom_impact_null_mutation_value():
    resp = client.post(
        "/dom-impact",
        json={
            "root": node("a", "p"),
            "rules": [],
            "mutation": {
                "type": "set_attribute",
                "node_id": "a",
                "name": "data-x",
                "value": None,
            },
        },
    )
    assert resp.status_code == 422


def test_422_preview_negative_declaration_index():
    resp = client.post(
        "/compute",
        json={
            "element": {"tag": "p"},
            "rules": [rule("p", decl("color", "red"))],
            "patches": [{"rule_index": 0, "declaration_index": -1, "value": "blue"}],
        },
    )
    assert resp.status_code == 422
    assert "declaration_index" in resp.text


def test_422_preview_patch_on_empty_rules():
    resp = client.post(
        "/compute",
        json={
            "element": {"tag": "p"},
            "rules": [],
            "patches": [{"rule_index": 0, "declaration_index": 0, "value": "x"}],
        },
    )
    assert resp.status_code == 422
    assert "越界" in resp.text
