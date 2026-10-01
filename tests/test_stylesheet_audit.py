"""POST /stylesheet-audit 样式表清理审计端到端测试。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def audit(payload, expected=200):
    resp = client.post("/stylesheet-audit", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def decl(prop, value, important=False):
    d = {"property": prop, "value": value}
    if important:
        d["important"] = True
    return d


def rule(selector, *decls, origin="author", layer=None, order=None, important=False, **extra):
    if important:
        decls = tuple({**d, "important": True} for d in decls)
    r = {
        "selector": selector,
        "origin": origin,
        "layer": layer,
        "declarations": list(decls),
    }
    if order is not None:
        r["order"] = order
    r.update(extra)
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


def tree_payload(root, rules, layers=None):
    p = {"root": root, "rules": rules}
    if layers is not None:
        p["layers"] = layers
    return p


def deleted_locs(data):
    return {(d["rule_index"], d["declaration_index"]) for d in data["deleted"]}


def retained_map(data):
    return {(d["rule_index"], d["declaration_index"]): d for d in data["retained"]}


# ---------- 基础：胜出者保留、落选者与未命中声明删除 ----------


def test_winners_retained_losers_deleted():
    root = node("r", "div", children=[node("a", "p", classes=["x"])])
    payload = tree_payload(
        root,
        [
            rule("p.x", decl("color", "red"), decl("color", "blue")),
            rule("i", decl("color", "pink")),
        ],
    )
    data = audit(payload)
    assert data["nodes_checked"] == 2
    # 规则0第0条 color:red 在同规则同属性竞争中落选 -> 可删；
    # 规则1选择器不命中任何节点 -> 可删
    assert deleted_locs(data) == {(0, 0), (1, 0)}
    # 真正的胜出者必须保留
    keeps = retained_map(data)
    assert set(keeps) == {(0, 1)}
    assert keeps[(0, 1)]["conflict_node"] == "a"
    assert keeps[(0, 1)]["conflict_property"] == "color"
    assert keeps[(0, 1)]["reason"] == "winner_changed"


def test_same_value_runner_up_still_blocks_winner_deletion():
    # 即使继任者取值相同，胜出声明换人也不得纳入批次
    root = node("r", "p")
    payload = tree_payload(
        root,
        [rule("p", decl("color", "red")), rule("p", decl("color", "red"))],
    )
    data = audit(payload)
    assert deleted_locs(data) == {(0, 0)}  # 落选者可删
    keeps = retained_map(data)
    assert (1, 0) in keeps and keeps[(1, 0)]["reason"] == "winner_changed"
    # 单独删除胜出者后节点 a 的 color 仍由规则0给出，但胜出者变了 -> 阻断


def test_deleted_batch_actually_safe_via_compute():
    # 用 /compute 在每个节点上交叉验证：删除批次后结果与基线一致
    root = node(
        "r",
        "div",
        children=[
            node("a", "p", children=[node("b", "span")]),
        ],
    )
    rules = [
        rule("div", decl("color", "black"), decl("--g", "4px")),
        rule("p", decl("color", "red"), decl("color", "blue")),
        rule("span", decl("margin", "0")),
        rule("em", decl("color", "pink")),
    ]
    data = audit(tree_payload(root, rules))
    batch = deleted_locs(data)

    def compute_at(target, ancestors):
        resp = client.post(
            "/compute",
            json={"element": target, "ancestors": ancestors, "rules": rules},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return body["computed"], body["custom_properties"], sorted(
            (i["property"], i["reason"]) for i in body["invalid"]
        )

    chain_specs = [
        (node("r", "div"), []),
        (node("a", "p"), [node("r", "div")]),
        (node("b", "span"), [node("r", "div"), node("a", "p")]),
    ]
    pruned = [
        {
            **r,
            "declarations": [
                d for di, d in enumerate(r["declarations"]) if (ri, di) not in batch
            ],
        }
        for ri, r in enumerate(rules)
    ]
    for target, ancestors in chain_specs:
        before = compute_at(target, ancestors)
        resp = client.post(
            "/compute",
            json={"element": target, "ancestors": ancestors, "rules": pruned},
        )
        after = (
            resp.json()["computed"],
            resp.json()["custom_properties"],
            sorted((i["property"], i["reason"]) for i in resp.json()["invalid"]),
        )
        assert before == after


def test_deleted_and_retained_together_cover_all_declarations_in_order():
    root = node("r", "p")
    payload = tree_payload(
        root,
        [rule("p", decl("color", "red"), decl("margin", "0"))],
    )
    data = audit(payload)
    all_out = [(d["rule_index"], d["declaration_index"]) for d in data["deleted"]]
    all_out += [(d["rule_index"], d["declaration_index"]) for d in data["retained"]]
    assert all_out == sorted(all_out)  # 按 rule_index、declaration_index 顺序
    assert set(all_out) == {(0, 0), (0, 1)}
    assert all(d["selector"] == "p" for d in data["deleted"] + data["retained"])
    assert {d["property"] for d in data["deleted"] + data["retained"]} == {
        "color",
        "margin",
    }


# ---------- 自定义属性：继承、var()、fallback、循环 ----------


def test_custom_property_inheritance_blocks_at_winner_node():
    root = node(
        "r", "div", children=[node("a", "p", children=[node("b", "span")])]
    )
    payload = tree_payload(
        root,
        [
            rule("div", decl("--g", "4px")),
            rule("p", decl("padding", "var(--g)")),
            rule("span", decl("color", "var(--g)")),
        ],
    )
    data = audit(payload)
    keeps = retained_map(data)
    # --g 在根胜出，删除后沿继承链影响后代；首个冲突节点是根（先序最早）
    g = keeps[(0, 0)]
    assert g["conflict_node"] == "r"
    assert g["conflict_property"] == "--g"
    assert g["reason"] == "winner_changed"
    assert keeps[(1, 0)]["conflict_node"] == "a"
    assert keeps[(2, 0)]["conflict_node"] == "b"
    assert deleted_locs(data) == set()


def test_losing_custom_declaration_deletable_even_with_var_deps():
    # 落选的自定义属性声明不参与 own 集合（循环检测 / 解析都不含它），可删
    root = node("r", "div")
    payload = tree_payload(
        root,
        [
            rule("div", decl("--a", "1px"), decl("--a", "2px")),
            rule("div", decl("color", "var(--a)")),
        ],
    )
    data = audit(payload)
    assert deleted_locs(data) == {(0, 0)}  # 1px 落选，删掉不影响 color: var(--a)=2px
    keeps = retained_map(data)
    assert (0, 1) in keeps and (1, 0) in keeps


def test_fallback_dependency_considered():
    # --m 缺失，padding 靠 fallback 取 1px；--m 的声明一旦在某节点胜出即决定状态。
    # 这里：声明 --m: 9px 落选（同节点另一条更高优先级），删除安全；
    # 使用 fallback 的胜出声明本身必须保留。
    root = node("r", "div", children=[node("a", "p")])
    payload = tree_payload(
        root,
        [
            rule("div", decl("--m", "9px")),  # div 上唯一 --m 声明 -> 胜出 -> 保留
            rule("p", decl("padding", "var(--missing, 1px)")),  # fallback -> 胜出
        ],
    )
    data = audit(payload)
    keeps = retained_map(data)
    assert (0, 0) in keeps and keeps[(0, 0)]["conflict_node"] == "r"
    assert (1, 0) in keeps
    # 一个完全不命中、且其变量从未被引用的声明可删
    payload2 = tree_payload(
        root,
        [
            rule("div", decl("--m", "9px")),
            rule("p", decl("padding", "var(--missing, 1px)")),
            rule("i", decl("--missing", "0px")),  # 选择器不命中 -> fallback 仍生效
        ],
    )
    data2 = audit(payload2)
    assert (2, 0) in deleted_locs(data2)


def test_cyclic_custom_properties_retained():
    # 循环中的两个自定义属性都决定 invalid 状态，必须保留；
    # 靠 fallback 绕开循环的 color 胜出声明同样保留。
    root = node("r", "div")
    payload = tree_payload(
        root,
        [
            rule(
                "div",
                decl("--x", "var(--y)"),
                decl("--y", "var(--x)"),
                decl("color", "var(--x, red)"),
            )
        ],
    )
    data = audit(payload)
    assert deleted_locs(data) == set()
    keeps = retained_map(data)
    for loc in ((0, 0), (0, 1), (0, 2)):
        assert loc in keeps
    # 循环声明的值为 invalid，删除会使 invalid 状态消失（missing），阻断信息体现消失
    assert "missing" in keeps[(0, 0)]["detail"] or "胜出" in keeps[(0, 0)]["detail"]


def test_shadowed_inheritance_descendant_own_winner():
    # 后代用本元素胜出声明遮蔽继承值时，祖先声明的首个冲突仍在祖先自身
    root = node(
        "r", "div", children=[node("a", "p"), node("b", "span")]
    )
    payload = tree_payload(
        root,
        [
            rule("div", decl("--g", "4px")),
            rule("p", decl("--g", "8px")),  # p 自己胜出，遮蔽继承
            rule("span", decl("color", "var(--g)")),  # span 继承 div 的 4px
        ],
    )
    data = audit(payload)
    keeps = retained_map(data)
    assert keeps[(0, 0)]["conflict_node"] == "r"  # 不是 b
    assert keeps[(1, 0)]["conflict_node"] == "a"
    assert keeps[(2, 0)]["conflict_node"] == "b"


def test_invalid_custom_property_winner_retained():
    # --bad 引用不存在的变量且无 fallback -> 本元素 invalid；删除会改变 invalid 状态
    root = node("r", "div")
    payload = tree_payload(
        root,
        [
            rule("div", decl("--bad", "var(--nope)")),
            rule("div", decl("color", "var(--bad, red)")),  # fallback 改道
        ],
    )
    data = audit(payload)
    assert deleted_locs(data) == set()
    keeps = retained_map(data)
    bad = keeps[(0, 0)]
    assert bad["conflict_property"] == "--bad"
    assert bad["conflict_node"] == "r"


# ---------- 层 / 来源 / important ----------


def test_layer_order_respected_in_audit():
    root = node("r", "p")
    payload = tree_payload(
        root,
        [
            rule("p", decl("color", "red"), layer="base"),
            rule("p", decl("color", "blue")),  # 未分层高于分层 -> 胜出
        ],
        layers=["base"],
    )
    data = audit(payload)
    assert deleted_locs(data) == {(0, 0)}
    assert retained_map(data)[(1, 0)]["reason"] == "winner_changed"


def test_important_runner_up_deletable():
    root = node("r", "p")
    payload = tree_payload(
        root,
        [
            # 同为 author 普通声明，未分层 > 分层：base 层的落选可删
            rule("p", decl("color", "black"), layer="base"),
            # 同来源同层序，important 优先；普通声明落选
            rule("p", decl("color", "gray"), layer="base"),
            rule("p", decl("color", "red"), important=True),
        ],
        layers=["base"],
    )
    data = audit(payload)
    assert deleted_locs(data) == {(0, 0), (1, 0)}
    assert (2, 0) in retained_map(data)


# ---------- 冲突节点取先序首个 ----------


def test_conflict_node_is_first_in_preorder():
    # 同一声明在多个节点胜出，报告先序最早的节点
    root = node(
        "r",
        "div",
        children=[
            node("a", "div", children=[node("b", "div")]),
        ],
    )
    payload = tree_payload(root, [rule("div", decl("margin", "0"))])
    data = audit(payload)
    keep = retained_map(data)[(0, 0)]
    assert keep["conflict_node"] == "r"


def test_integer_ids_supported():
    root = node(1, "div", children=[node(2, "p")])
    data = audit(tree_payload(root, [rule("p", decl("color", "red"))]))
    assert data["nodes_checked"] == 2
    keeps = retained_map(data)
    assert keeps[(0, 0)]["conflict_node"] == 2
    assert deleted_locs(data) == set()


# ---------- 校验：422 ----------


def test_duplicate_id_returns_422():
    root = node("a", "div", children=[node("a", "p")])
    audit(tree_payload(root, []), expected=422)


def test_empty_tag_returns_422():
    audit(tree_payload(node("a", "   "), []), expected=422)


def test_missing_id_returns_422():
    audit({"root": {"tag": "div"}, "rules": []}, expected=422)


def test_null_children_returns_422():
    audit({"root": {"id": "a", "tag": "div", "children": None}, "rules": []}, expected=422)


def test_empty_id_string_returns_422():
    audit(tree_payload(node(" ", "div"), []), expected=422)


def test_boolean_id_rejected():
    audit(tree_payload(node(True, "div"), []), expected=422)


def test_invalid_selector_returns_422():
    payload = tree_payload(
        node("a", "div"), [rule(">>", decl("color", "red"))]
    )
    audit(payload, expected=422)


def test_empty_property_name_returns_422():
    payload = tree_payload(
        node("a", "div"), [rule("div", decl(" ", "red"))]
    )
    audit(payload, expected=422)


# ---------- 兼容性 ----------


def test_compute_without_audit_field_unchanged():
    # /compute 旧请求体（无 root）行为不变
    resp = client.post(
        "/compute",
        json={
            "element": node("a", "p"),
            "ancestors": [],
            "rules": [rule("p", decl("color", "red"))],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["computed"] == {"color": "red"}


def test_health_and_openapi_include_audit():
    assert client.get("/health").json() == {"status": "ok"}
    paths = client.get("/openapi.json").json()["paths"]
    assert "/stylesheet-audit" in paths
