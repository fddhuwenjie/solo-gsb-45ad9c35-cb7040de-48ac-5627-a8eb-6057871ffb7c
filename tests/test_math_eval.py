"""端到端测试：POST /math-evaluate。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.math_eval import MathEvaluationError, evaluate_math
from app.models import MathContext, MathEvaluateRequest

client = TestClient(app)


def evaluate(expression, **context):
    return evaluate_math(
        MathEvaluateRequest(expression=expression, context=MathContext(**context))
    )


def post(body):
    return client.post("/math-evaluate", json=body)


def collect_ids(node):
    """按先序收集 AST 节点 id。"""
    ids = [node.id]
    children = []
    if node.operand is not None:
        children.append(node.operand)
    if node.left is not None:
        children.extend([node.left, node.right])
    children.extend(node.args)
    for child in children:
        ids.extend(collect_ids(child))
    return ids


# ---------- 基本四则与优先级 ----------


def test_simple_addition():
    data = evaluate("calc(1px + 2px)")
    assert (data.value, data.unit) == (3.0, "px")


def test_precedence():
    data = evaluate("calc(1 + 2 * 3)")
    assert (data.value, data.unit) == (7.0, "")


def test_parentheses():
    assert evaluate("calc((1 + 2) * 3)").value == 9.0


def test_bare_expression_without_calc():
    assert evaluate("1px + 2px").value == 3.0


def test_unary_minus():
    assert evaluate("calc(-5px + 2px)").value == -3.0
    assert evaluate("calc(1px - -2px)").value == 3.0
    assert evaluate("calc(-(1px + 2px))").value == -3.0
    assert evaluate("calc(--5px)").value == 5.0


def test_unary_plus_is_identity():
    assert evaluate("calc(+5)").value == 5.0


def test_division_unitless():
    data = evaluate("calc(10 / 4)")
    assert (data.value, data.unit) == (2.5, "")


def test_length_divided_by_number():
    data = evaluate("calc(10px / 4)")
    assert (data.value, data.unit) == (2.5, "px")


def test_multiply_number_and_length():
    assert evaluate("calc(3 * 2px)").value == 6.0
    assert evaluate("calc(2px * 3)").value == 6.0
    assert evaluate("calc(3 * 2)").unit == ""


# ---------- 单位换算 ----------


def test_em_conversion():
    data = evaluate("calc(2em)", font_size=16)
    assert (data.value, data.unit) == (32.0, "px")


def test_rem_conversion():
    assert evaluate("calc(2rem)", root_font_size=8).value == 16.0


def test_vw_vh_conversion():
    assert evaluate("calc(50vw)", viewport_width=1000).value == 500.0
    assert evaluate("calc(50vh)", viewport_height=500).value == 250.0


def test_percentage_conversion():
    data = evaluate("calc(50%)", percentage_basis=200)
    assert (data.value, data.unit) == (100.0, "px")


def test_percentage_plus_length():
    assert evaluate("calc(50% + 10px)", percentage_basis=200).value == 110.0


def test_mixed_length_units_share_dimension():
    data = evaluate("calc(1em + 1rem + 1px)", font_size=16, root_font_size=8)
    assert data.value == 25.0


def test_unit_case_insensitive():
    assert evaluate("CALC(1PX + Min(2Px, 3pX))").value == 3.0


def test_scientific_notation():
    assert evaluate("calc(1e2px)").value == 100.0
    assert evaluate("calc(1.5e-1px)").value == 0.15


def test_float_noise_is_rounded():
    assert evaluate("calc(0.1 + 0.2)").value == 0.3


def test_negative_zero_is_normalized():
    data = evaluate("calc(-0px)")
    assert data.value == 0.0
    assert data.trace[-1].value == 0.0


# ---------- min / max / clamp ----------


def test_min_max():
    assert evaluate("min(3, 1, 2)").value == 1.0
    assert evaluate("max(3px, 1px, 2px)").value == 3.0


def test_min_max_single_arg():
    assert evaluate("min(5px)").value == 5.0
    assert evaluate("max(5)").value == 5.0


def test_clamp():
    assert evaluate("clamp(1px, 5px, 10px)").value == 5.0
    assert evaluate("clamp(1px, -5px, 10px)").value == 1.0
    assert evaluate("clamp(1px, 50px, 10px)").value == 10.0


def test_clamp_min_wins_over_max():
    # CSS 语义：clamp = max(下限, min(首选值, 上限))，下限大于上限时下限胜出
    assert evaluate("clamp(10px, 5px, 1px)").value == 10.0


def test_nested_functions():
    data = evaluate(
        "calc(min(100%, 480px) / 2 + 1em)", font_size=16, percentage_basis=800
    )
    assert (data.value, data.unit) == (256.0, "px")


def test_nested_calc_is_transparent():
    assert evaluate("calc(calc(1px) + calc(2px))").value == 3.0


# ---------- AST 与轨迹 ----------


def test_ast_structure():
    data = evaluate("calc(1px + 2px)")
    ast = data.ast
    assert ast.type == "function" and ast.name == "calc"
    add = ast.args[0]
    assert add.type == "binary" and add.op == "+"
    assert add.left.type == "number" and add.left.value == 1.0
    assert add.left.unit == "px"
    assert add.right.value == 2.0


def test_ast_ids_preorder_from_one():
    data = evaluate("calc(1px + 2px * 3)")
    assert collect_ids(data.ast) == [1, 2, 3, 4, 5, 6]


def test_ast_unary_node():
    data = evaluate("calc(-5px)")
    unary = data.ast.args[0]
    assert unary.type == "unary" and unary.op == "-"
    assert unary.operand.type == "number" and unary.operand.value == 5.0


def test_trace_postorder_last_step_is_result():
    data = evaluate("calc(1px + 2px * 3)")
    assert data.trace[-1].node_id == data.ast.id
    assert data.trace[-1].kind == "calc"
    assert data.trace[-1].value == data.value == 7.0
    assert data.trace[-1].unit == "px"
    # 每个 AST 节点恰好对应一条轨迹
    assert sorted(step.node_id for step in data.trace) == [1, 2, 3, 4, 5, 6]


def test_trace_conversion_detail():
    data = evaluate("calc(2em)", font_size=16)
    step = data.trace[0]
    assert step.kind == "number"
    assert "font_size=16" in step.detail
    assert (step.value, step.unit) == (32.0, "px")


def test_trace_multiply_and_divide_details():
    data = evaluate("calc(6px * 3 / 2)")
    kinds = [step.kind for step in data.trace]
    assert kinds == ["number", "number", "multiply", "number", "divide", "calc"]
    assert data.trace[2].detail == "6px * 3 = 18px"
    assert data.trace[4].detail == "18px / 2 = 9px"


# ---------- 422：维度与运算规则 ----------


def test_422_dimension_conflict_add_subtract():
    with pytest.raises(MathEvaluationError, match="维度冲突"):
        evaluate("calc(1px + 1)")
    with pytest.raises(MathEvaluationError, match="维度冲突"):
        evaluate("calc(1 - 1px)")


def test_422_dimension_conflict_function_args():
    with pytest.raises(MathEvaluationError, match="维度冲突"):
        evaluate("min(1px, 2)")
    with pytest.raises(MathEvaluationError, match="维度冲突"):
        evaluate("clamp(1px, 2px, 3)")


def test_422_multiply_two_lengths():
    with pytest.raises(MathEvaluationError, match="乘法最多一侧带维度"):
        evaluate("calc(2px * 3px)")


def test_422_division_by_length():
    with pytest.raises(MathEvaluationError, match="除数"):
        evaluate("calc(6 / 2px)")


def test_422_division_by_zero():
    with pytest.raises(MathEvaluationError, match="除数为 0"):
        evaluate("calc(1 / 0)")
    with pytest.raises(MathEvaluationError, match="除数为 0"):
        evaluate("calc(1px / (2 - 2))")


# ---------- 422：换算上下文 ----------


def test_422_missing_context():
    with pytest.raises(MathEvaluationError, match="缺少换算上下文"):
        evaluate("calc(2em)")
    with pytest.raises(MathEvaluationError, match="缺少换算上下文"):
        evaluate("calc(2rem)")
    with pytest.raises(MathEvaluationError, match="缺少换算上下文"):
        evaluate("calc(1vw)")
    with pytest.raises(MathEvaluationError, match="缺少换算上下文"):
        evaluate("calc(1vh)")
    with pytest.raises(MathEvaluationError, match="缺少换算上下文"):
        evaluate("calc(50%)")


def test_422_invalid_context_values():
    with pytest.raises(MathEvaluationError, match="非负"):
        evaluate("calc(1em)", font_size=-1)
    with pytest.raises(MathEvaluationError, match="有限"):
        evaluate("calc(1em)", font_size=float("nan"))
    with pytest.raises(MathEvaluationError, match="有限"):
        evaluate("calc(50%)", percentage_basis=float("inf"))


# ---------- 422：非有限结果 ----------


def test_422_non_finite_results():
    with pytest.raises(MathEvaluationError, match="非有限"):
        evaluate("calc(1e308 * 10)")
    with pytest.raises(MathEvaluationError, match="非有限"):
        evaluate("calc(1e999)")
    with pytest.raises(MathEvaluationError, match="非有限"):
        evaluate("calc(10em)", font_size=1e308)


# ---------- 422：语法错误 ----------


def test_422_syntax_errors():
    for expr in [
        "",
        "   ",
        "calc(1px +)",
        "calc(*1px)",
        "1px 2px",
        "calc(1px",
        "(1px",
        "1px)",
        "calc()",
        "foo(1)",
        "calc(1px @ 2px)",
        "calc(1deg)",
    ]:
        with pytest.raises(MathEvaluationError, match="语法错误"):
            evaluate(expr)


def test_422_function_arity():
    with pytest.raises(MathEvaluationError, match=r"calc\(\) 只接受 1 个"):
        evaluate("calc(1px, 2px)")
    with pytest.raises(MathEvaluationError, match=r"clamp\(\) 需要 3 个"):
        evaluate("clamp(1px, 2px)")
    with pytest.raises(MathEvaluationError, match=r"clamp\(\) 需要 3 个"):
        evaluate("clamp(1px, 2px, 3px, 4px)")


def test_422_too_deep_nesting():
    expr = "calc(" + "(" * 150 + "1px" + ")" * 150 + ")"
    with pytest.raises(MathEvaluationError, match="嵌套"):
        evaluate(expr)


# ---------- HTTP ----------


def test_http_success():
    resp = post(
        {
            "expression": "calc(min(100%, 480px) / 2 + 1em)",
            "context": {"font_size": 16, "percentage_basis": 800},
        }
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert list(data.keys()) == ["expression", "value", "unit", "ast", "trace"]
    assert data["value"] == 256.0
    assert data["unit"] == "px"
    assert data["ast"]["name"] == "calc"
    assert data["trace"][-1]["node_id"] == data["ast"]["id"]


def test_http_without_context():
    resp = post({"expression": "calc(1px + 2px)"})
    assert resp.status_code == 200
    assert resp.json()["value"] == 3.0


def test_http_422_dimension_conflict():
    resp = post({"expression": "calc(1px + 1)"})
    assert resp.status_code == 422
    assert "维度冲突" in resp.json()["detail"]


def test_http_422_missing_context():
    resp = post({"expression": "calc(2em + 1px)"})
    assert resp.status_code == 422
    assert "缺少换算上下文" in resp.json()["detail"]


def test_http_422_division_by_zero():
    resp = post({"expression": "calc(1px / 0)"})
    assert resp.status_code == 422
    assert "除数为 0" in resp.json()["detail"]


def test_http_422_non_finite():
    resp = post({"expression": "calc(1e308 * 10)"})
    assert resp.status_code == 422
    assert "非有限" in resp.json()["detail"]


def test_http_422_syntax_error():
    resp = post({"expression": "calc(1px +)"})
    assert resp.status_code == 422
    assert "语法错误" in resp.json()["detail"]


def test_health_still_ok():
    assert client.get("/health").json() == {"status": "ok"}
