"""端到端测试：POST /compute 携带 patches 的样式变更预演。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

P = {"tag": "p"}


def compute(payload, status=200):
    resp = client.post("/compute", json=payload)
    assert resp.status_code == status, resp.text
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


def changes_map(data):
    pv = data["preview"]
    return {c["property"]: c for c in pv["changes"]}


# ---------- 兼容旧请求 ----------


def test_no_patches_response_has_no_preview_field():
    data = compute(base_payload(rules=[rule("p", decl("color", "red"))]))
    assert "preview" not in data
    assert data["computed"] == {"color": "red"}


def test_empty_patches_list_is_legacy_response():
    data = compute(base_payload(rules=[rule("p", decl("color", "red"))], patches=[]))
    assert "preview" not in data


def test_candidate_out_carries_rule_and_declaration_indices():
    data = compute(base_payload(rules=[rule("p", decl("color", "red"))]))
    winner = data["evidence"][0]["winner"]
    assert winner["rule_index"] == 0
    assert winner["declaration_index"] == 0


# ---------- 胜出者变化（important / layer / value） ----------


def test_important_patch_flips_winner():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("color", "red")),
                rule("p", decl("color", "blue")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "important": True}],
        )
    )
    pv = data["preview"]
    assert pv["baseline"]["computed"]["color"] == "blue"
    assert pv["patched"]["computed"]["color"] == "red"
    ch = changes_map(data)["color"]
    assert ch["kind"] == "winner_changed"
    assert ch["status_changed"] is False
    assert ch["before_winner"]["rule_index"] == 1
    assert ch["after_winner"]["rule_index"] == 0
    assert [p["index"] for p in ch["responsible_patches"]] == [0]
    text = "\n".join(ch["trail"])
    assert "important False → True" in text
    assert "层叠胜出者" in text


def test_layer_patch_to_unlayered_flips_winner():
    data = compute(
        base_payload(
            layers=["base", "components"],
            rules=[
                rule("p", decl("color", "red"), layer="base"),
                rule("p", decl("color", "green"), layer="components"),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "layer": None}],
        )
    )
    pv = data["preview"]
    assert pv["baseline"]["computed"]["color"] == "green"
    assert pv["patched"]["computed"]["color"] == "red"
    ch = changes_map(data)["color"]
    assert ch["kind"] == "winner_changed"
    assert ch["before_winner"]["layer"] == "components"
    assert ch["after_winner"]["layer"] is None
    assert "layer 'base' → None" in "\n".join(ch["trail"])


def test_layer_patch_to_declared_layer():
    data = compute(
        base_payload(
            layers=["base", "components"],
            rules=[
                rule("p", decl("color", "red"), layer="base"),
                rule("p", decl("color", "green"), layer="components"),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "layer": "components"}],
        )
    )
    pv = data["preview"]
    # 同层后源码次序靠后者仍胜（规则 1），结果不变 -> shadowed
    assert pv["patched"]["computed"]["color"] == "green"
    assert pv["changes"] == []
    assert pv["shadowed_patches"][0]["patch"]["index"] == 0


def test_undeclared_layer_in_patch_is_appended():
    data = compute(
        base_payload(
            layers=["base"],
            rules=[
                rule("p", decl("color", "red"), layer="base"),
                rule("p", decl("color", "green"), layer="base"),
            ],
            patches=[{"rule_index": 1, "declaration_index": 0, "layer": "extra"}],
        )
    )
    pv = data["preview"]
    # extra 作为更晚声明的层追加，规则 1 的 green 反超
    assert pv["patched"]["computed"]["color"] == "green"
    assert pv["baseline"]["computed"]["color"] == "green"  # 基线：同层源码次序 green 已胜
    # 两个世界都是 green，无差异，补丁 shadowed
    assert pv["changes"] == []


# ---------- 同胜出声明：edited ----------


def test_edit_winning_literal_declaration():
    data = compute(
        base_payload(
            rules=[rule("p", decl("margin", "1px"))],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "2px"}],
        )
    )
    ch = changes_map(data)["margin"]
    assert ch["kind"] == "edited"
    assert ch["before_value"] == "1px"
    assert ch["after_value"] == "2px"
    assert ch["before_winner"]["rule_index"] == ch["after_winner"]["rule_index"]


def test_noop_value_patch_is_shadowed():
    data = compute(
        base_payload(
            rules=[rule("p", decl("margin", "1px"))],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "1px"}],
        )
    )
    pv = data["preview"]
    assert pv["changes"] == []
    assert [s["patch"]["index"] for s in pv["shadowed_patches"]] == [0]


# ---------- 同胜出声明：var() 依赖变化 ----------


def test_var_dependency_change_with_same_winner():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("--x", "1px"), decl("margin", "var(--x)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "3px"}],
        )
    )
    cm = changes_map(data)
    assert cm["--x"]["kind"] == "edited"
    ch = cm["margin"]
    assert ch["kind"] == "var_changed"
    assert ch["before_value"] == "1px"
    assert ch["after_value"] == "3px"
    trail = "\n".join(ch["trail"])
    assert "规则 0 第 0 条声明" in trail
    assert "var(--x)" in trail


def test_fallback_reroute_when_variable_breaks():
    data = compute(
        base_payload(
            rules=[
                rule(
                    "p",
                    decl("--x", "1px"),
                    decl("margin", "var(--x, 9px)"),
                )
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "var(--missing)"}],
        )
    )
    ch = changes_map(data)["margin"]
    assert ch["kind"] == "var_changed"
    assert ch["before_value"] == "1px"
    assert ch["after_value"] == "9px"
    trail = "\n".join(ch["trail"])
    assert "fallback 改道" in trail
    assert "9px" in trail


def test_fallback_rejoin_when_variable_fixed():
    # 基线缺失走 fallback；补丁新增有效值，重新走主分支
    data = compute(
        base_payload(
            rules=[
                rule(
                    "p",
                    decl("--x", "var(--never, 5px)"),
                    decl("margin", "var(--x, 9px)"),
                )
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "7px"}],
        )
    )
    pv = data["preview"]
    assert pv["baseline"]["computed"]["margin"] == "5px"
    assert pv["patched"]["computed"]["margin"] == "7px"
    trail = "\n".join(changes_map(data)["margin"]["trail"])
    assert "【基线】" in trail and "【补丁后】" in trail


# ---------- computed / invalid 状态切换 ----------


def test_computed_to_invalid_status_switch():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("--x", "1px"), decl("color", "var(--x)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "var(--nope)"}],
        )
    )
    ch = changes_map(data)["color"]
    assert ch["status_changed"] is True
    assert ch["before_status"] == "computed"
    assert ch["after_status"] == "invalid"
    assert ch["after_value"] is None
    # --x 自身因引用未定义变量而失效，color 看到的是“自定义属性已失效”
    assert ch["after_reason"] and "已失效" in ch["after_reason"]
    assert ch["before_reason"] is None
    trail = "\n".join(ch["trail"])
    assert "失效" in trail


def test_invalid_to_computed_status_switch():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("width", "var(--x)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "10px"}],
        )
    )
    pv = data["preview"]
    assert pv["baseline"]["invalid"][0]["property"] == "width"
    assert pv["patched"]["invalid"] == []
    ch = changes_map(data)["width"]
    assert ch["status_changed"] is True
    assert ch["before_status"] == "invalid"
    assert ch["after_status"] == "computed"
    assert ch["after_value"] == "10px"


def test_status_switch_can_coincide_with_winner_change():
    # 规则 1 的声明引用缺失变量（胜出但 invalid）；把规则 0 改为 important
    # 后由其字面值胜出，invalid -> computed 且胜出者换人
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("color", "red")),
                rule("p", decl("color", "var(--missing)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "important": True}],
        )
    )
    ch = changes_map(data)["color"]
    assert ch["kind"] == "winner_changed"
    assert ch["status_changed"] is True
    assert ch["before_status"] == "invalid"
    assert ch["after_status"] == "computed"
    assert ch["after_value"] == "red"
    assert ch["before_value"] is None


# ---------- 自定义属性：循环 ----------


def test_cycle_formation_joint_responsibility():
    data = compute(
        base_payload(
            rules=[
                rule(
                    "p",
                    decl("--a", "1px"),
                    decl("--b", "2px"),
                    decl("color", "var(--a)"),
                    decl("margin", "var(--a, 9px)"),
                )
            ],
            patches=[
                {"rule_index": 0, "declaration_index": 0, "value": "var(--b)"},
                {"rule_index": 0, "declaration_index": 1, "value": "var(--a)"},
            ],
        )
    )
    cm = changes_map(data)
    for name in ("--a", "--b"):
        ch = cm[name]
        assert ch["is_custom"] is True
        assert ch["before_status"] == "ok"
        assert ch["after_status"] == "invalid"
        # 两条补丁联合致因：任一条单独都不成环
        assert [p["index"] for p in ch["responsible_patches"]] == [0, 1]
    assert cm["color"]["after_status"] == "invalid"
    assert cm["margin"]["after_value"] == "9px"
    assert "循环依赖" in "\n".join(cm["--b"]["trail"])
    assert "fallback 改道" in "\n".join(cm["margin"]["trail"])
    assert data["preview"]["shadowed_patches"] == []


def test_cycle_break_restores_values():
    data = compute(
        base_payload(
            ancestors=[{"tag": "body"}],
            rules=[
                rule("body", decl("--t", "10px")),
                rule(
                    "p",
                    decl("--a", "var(--b)"),
                    decl("--b", "var(--a)"),
                    decl("width", "var(--a)"),
                    decl("height", "var(--a, 5px)"),
                ),
            ],
            patches=[{"rule_index": 1, "declaration_index": 0, "value": "var(--t)"}],
        )
    )
    cm = changes_map(data)
    assert cm["--a"]["before_status"] == "invalid"
    assert cm["--a"]["after_status"] == "ok"
    assert cm["--a"]["after_value"] == "10px"
    assert cm["width"]["kind"] == "var_changed"
    assert cm["width"]["before_status"] == "invalid"
    assert cm["width"]["after_value"] == "10px"
    assert cm["height"]["before_value"] == "5px"
    assert cm["height"]["after_value"] == "10px"
    trail = "\n".join(cm["width"]["trail"])
    assert "循环依赖" in trail  # 基线成环、补丁后解除


def test_only_one_patch_breaking_other_pair_cycle():
    # --a/--b 已在基线上成环；补丁改 --a 引用 --c，--a 恢复但 --b 仍引用 --a……
    data = compute(
        base_payload(
            rules=[
                rule(
                    "p",
                    decl("--a", "var(--b)"),
                    decl("--b", "var(--a)"),
                    decl("color", "var(--a)"),
                )
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "4px"}],
        )
    )
    cm = changes_map(data)
    assert cm["--a"]["after_status"] == "ok"
    assert cm["--a"]["after_value"] == "4px"
    assert cm["--b"]["after_status"] == "ok"
    assert cm["--b"]["after_value"] == "4px"
    assert cm["color"]["after_value"] == "4px"
    trail = "\n".join(cm["--b"]["trail"])
    assert "循环依赖" in trail and "直接替换" in trail


# ---------- 祖先链继承 ----------


def test_ancestor_patch_inherits_through_chain():
    data = compute(
        base_payload(
            element=P,
            ancestors=[{"tag": "body"}, {"tag": "div"}],
            rules=[
                rule("body", decl("--g", "4px")),
                rule("div", decl("--x", "var(--g)")),
                rule("p", decl("margin", "var(--x)"), decl("color", "var(--g)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "6px"}],
        )
    )
    cm = changes_map(data)
    assert cm["--g"]["after_value"] == "6px"
    assert cm["--x"]["after_value"] == "6px"
    assert cm["margin"]["after_value"] == "6px"
    assert cm["color"]["after_value"] == "6px"
    trail = "\n".join(cm["margin"]["trail"])
    assert "body" in trail and "div" in trail
    # 自定义属性 --g 在目标元素未声明，其轨迹含沿链继承边
    assert "继承" in "\n".join(cm["--g"]["trail"])


def test_intermediate_override_shadows_ancestor_patch():
    # div 本元素覆盖 --g，body 上的补丁不影响目标结果 -> shadowed
    data = compute(
        base_payload(
            ancestors=[{"tag": "body"}, {"tag": "div"}],
            rules=[
                rule("body", decl("--g", "4px")),
                rule("div", decl("--g", "8px")),
                rule("p", decl("color", "var(--g)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "6px"}],
        )
    )
    pv = data["preview"]
    assert pv["patched"]["computed"]["color"] == "8px"
    assert pv["changes"] == []
    assert pv["shadowed_patches"][0]["patch"]["index"] == 0


def test_custom_property_winner_change_on_target():
    data = compute(
        base_payload(
            layers=["base"],
            rules=[
                rule("p", decl("--x", "layered"), layer="base"),
                rule("p", decl("--x", "unlayered")),
                rule("p", decl("margin", "var(--x)")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "layer": None}],
        )
    )
    # 两条都未分层后同特异性，规则 0 源码次序更早 -> 规则 1 仍胜，无变化
    pv = data["preview"]
    assert pv["patched"]["custom_properties"] == {"--x": "unlayered"}
    assert pv["changes"] == []


# ---------- shadowed 分类 ----------


def test_shadowed_non_matching_rule():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("color", "red")),
                rule("div", decl("color", "black")),
            ],
            patches=[{"rule_index": 1, "declaration_index": 0, "value": "white"}],
        )
    )
    s = data["preview"]["shadowed_patches"]
    assert len(s) == 1
    assert "未命中目标元素" in s[0]["reason"]


def test_shadowed_losing_declaration():
    data = compute(
        base_payload(
            element={"tag": "p", "id": "x"},
            rules=[
                rule("p", decl("color", "tag")),
                rule("#x", decl("color", "id")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "important": True}],
        )
    )
    # tag 规则改 important 后会胜出 -> 实际有变化；改为只改 value 才 shadowed
    assert changes_map(data)["color"]["after_value"] == "tag"


def test_shadowed_losing_declaration_value_only():
    data = compute(
        base_payload(
            element={"tag": "p", "id": "x"},
            rules=[
                rule("p", decl("color", "tag")),
                rule("#x", decl("color", "id")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "gray"}],
        )
    )
    s = data["preview"]["shadowed_patches"]
    assert len(s) == 1
    assert "落选" in s[0]["reason"]


def test_shadowed_winner_already_wins_by_other_dimension():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("color", "red")),
                rule("p", decl("color", "blue")),
            ],
            patches=[{"rule_index": 1, "declaration_index": 0, "important": True}],
        )
    )
    s = data["preview"]["shadowed_patches"]
    assert len(s) == 1
    assert "仍是胜出者" in s[0]["reason"]


# ---------- 原子性：非法补丁整次 422 ----------


def _expect_422(payload, fragment=""):
    resp = client.post("/compute", json=payload)
    assert resp.status_code == 422, resp.text
    assert fragment in resp.text


def test_rule_index_out_of_range_422():
    _expect_422(
        base_payload(
            rules=[rule("p", decl("color", "red"))],
            patches=[{"rule_index": 5, "declaration_index": 0, "value": "x"}],
        ),
        "越界",
    )


def test_declaration_index_out_of_range_422():
    _expect_422(
        base_payload(
            rules=[rule("p", decl("color", "red"))],
            patches=[{"rule_index": 0, "declaration_index": 3, "value": "x"}],
        ),
        "只有 1 条声明",
    )


def test_negative_index_422():
    _expect_422(
        base_payload(
            rules=[rule("p", decl("color", "red"))],
            patches=[{"rule_index": -1, "declaration_index": 0, "value": "x"}],
        ),
        "越界",
    )


def test_duplicate_location_422():
    _expect_422(
        base_payload(
            rules=[rule("p", decl("color", "red"))],
            patches=[
                {"rule_index": 0, "declaration_index": 0, "value": "x"},
                {"rule_index": 0, "declaration_index": 0, "important": True},
            ],
        ),
        "定位重复",
    )


def test_patch_without_any_change_422():
    _expect_422(
        base_payload(
            rules=[rule("p", decl("color", "red"))],
            patches=[{"rule_index": 0, "declaration_index": 0}],
        ),
        "至少给一个",
    )


def test_float_and_bool_indices_rejected_422():
    payload = base_payload(
        rules=[rule("p", decl("color", "red"))],
        patches=[{"rule_index": 0.5, "declaration_index": 0, "value": "x"}],
    )
    assert client.post("/compute", json=payload).status_code == 422
    payload = base_payload(
        rules=[rule("p", decl("color", "red"))],
        patches=[{"rule_index": True, "declaration_index": 0, "value": "x"}],
    )
    assert client.post("/compute", json=payload).status_code == 422


def test_null_value_and_important_rejected_422():
    rules = [rule("p", decl("color", "red"))]
    for patch in (
        {"rule_index": 0, "declaration_index": 0, "value": None},
        {"rule_index": 0, "declaration_index": 0, "important": None},
        {"rule_index": 0, "declaration_index": 0, "layer": 5},
        {"rule_index": 0, "declaration_index": 0, "value": 123},
    ):
        resp = client.post(
            "/compute", json=base_payload(rules=rules, patches=[patch])
        )
        assert resp.status_code == 422, patch


def test_layer_null_is_explicit_unlayer_not_rejected():
    data = compute(
        base_payload(
            layers=["base"],
            rules=[rule("p", decl("color", "red"), layer="base")],
            patches=[{"rule_index": 0, "declaration_index": 0, "layer": None}],
        )
    )
    # 改为未分层后仍正常计算，不报 422
    assert data["preview"]["patched"]["computed"]["color"] == "red"


def test_atomicity_one_bad_patch_rejects_all():
    # 即便另一条补丁本可生效，任一定位非法则整次无结果
    resp = client.post(
        "/compute",
        json=base_payload(
            rules=[rule("p", decl("color", "red"))],
            patches=[
                {"rule_index": 0, "declaration_index": 0, "value": "blue"},
                {"rule_index": 9, "declaration_index": 0, "value": "x"},
            ],
        ),
    )
    assert resp.status_code == 422


# ---------- 响应结构 ----------


def test_preview_response_shape():
    data = compute(
        base_payload(
            rules=[rule("p", decl("margin", "1px"))],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "2px"}],
        )
    )
    # 顶层仍为基线结果
    assert data["computed"]["margin"] == "1px"
    pv = data["preview"]
    assert set(pv.keys()) == {
        "baseline",
        "patched",
        "changes",
        "shadowed_patches",
        "applied_patches",
    }
    assert pv["baseline"]["computed"]["margin"] == "1px"
    assert pv["patched"]["computed"]["margin"] == "2px"
    assert pv["applied_patches"] == [
        {"index": 0, "rule_index": 0, "declaration_index": 0}
    ]
    # 快照只含 computed / custom_properties / invalid
    assert set(pv["baseline"].keys()) == {"computed", "custom_properties", "invalid"}


def test_invalid_list_mirrored_in_both_snapshots():
    data = compute(
        base_payload(
            rules=[rule("p", decl("width", "var(--x)"))],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "10px"}],
        )
    )
    pv = data["preview"]
    assert [i["property"] for i in pv["baseline"]["invalid"]] == ["width"]
    assert pv["patched"]["invalid"] == []


def test_changes_only_list_changed_properties():
    data = compute(
        base_payload(
            rules=[
                rule("p", decl("color", "red"), decl("margin", "1px")),
            ],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "blue"}],
        )
    )
    assert list(changes_map(data).keys()) == ["color"]


def test_custom_property_change_is_flagged_custom():
    data = compute(
        base_payload(
            rules=[rule("p", decl("--x", "1px"), decl("--y", "2px"))],
            patches=[{"rule_index": 0, "declaration_index": 0, "value": "9px"}],
        )
    )
    ch = changes_map(data)["--x"]
    assert ch["is_custom"] is True
    assert "--y" not in changes_map(data)
