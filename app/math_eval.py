"""CSS 数学表达式求值：calc() / min() / max() / clamp() 与 + - * / 及括号。

运算数支持无单位数与 px / em / rem / vw / vh / %；所有带单位的运算数
先按 context 换算并**规范化为 px 长度**：

- em  -> context.font_size         （1em 对应的 px）
- rem -> context.root_font_size    （1rem 对应的 px）
- vw  -> context.viewport_width    （100vw 对应的 px）
- vh  -> context.viewport_height   （100vh 对应的 px）
- %   -> context.percentage_basis  （100% 对应的 px）

维度规则（与 CSS calc 一致）：

- 加减与 min / max / clamp 的各参数必须同维（长度与长度、无单位数与无单位数）；
- 乘法最多一侧带维度（长度 × 长度 为维度冲突）；
- 除法的除数必须是非零无单位数，商的维度与被除数相同。

响应包含规范化数值与单位、AST 与后序的逐节点求值轨迹；缺少换算上下文、
维度冲突、除零、非有限结果与语法错误均抛出 MathEvaluationError（HTTP 422）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .models import (
    MathAstNodeOut,
    MathContext,
    MathEvaluateRequest,
    MathEvaluateResponse,
    MathTraceStepOut,
)

# 表达式最大嵌套深度（括号 / 函数参数），防止递归过深
_MAX_DEPTH = 100

_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_IDENT_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*")
_OPERATOR_CHARS = "+-*/(),%"

_LENGTH_UNITS = ("px", "em", "rem", "vw", "vh")
_FUNCTIONS = ("calc", "min", "max", "clamp")

# 单位 -> 换算所需的 context 字段；vw / vh / % 还需除以 100
_CONTEXT_FIELD = {
    "em": "font_size",
    "rem": "root_font_size",
    "vw": "viewport_width",
    "vh": "viewport_height",
    "%": "percentage_basis",
}
_CONTEXT_FIELDS = (
    "font_size",
    "root_font_size",
    "viewport_width",
    "viewport_height",
    "percentage_basis",
)


class MathEvaluationError(ValueError):
    """表达式不满足求值前提（映射为 HTTP 422）。"""


# ----------------------------------------------------------------- 输出格式


def _round(value: float) -> float:
    """输出前去掉浮点尾巴并消掉 -0。"""
    return round(value, 10) + 0.0


def _fmt(value: float) -> str:
    """轨迹详情中的数值格式：最多 10 位有效数字并去掉浮点尾巴与 -0。"""
    if value == 0:
        value = 0.0
    text = f"{value:.10g}"
    return text if text != "-0" else "0"


def _fmt_val(value: float, dim: str) -> str:
    return _fmt(value) + ("px" if dim == "length" else "")


# ----------------------------------------------------------------- 词法分析


def _tokenize(expression: str) -> list[tuple[str, str, int]]:
    """把表达式切分为 (kind, text, position) 记号序列。

    kind 为 "num" / "ident" / "op"；数字不带前导符号（符号由一元运算处理）。
    """
    tokens: list[tuple[str, str, int]] = []
    i, n = 0, len(expression)
    while i < n:
        ch = expression[i]
        if ch.isspace():
            i += 1
            continue
        match = _NUMBER_RE.match(expression, i)
        if match is not None:
            tokens.append(("num", match.group(), i))
            i = match.end()
            continue
        match = _IDENT_RE.match(expression, i)
        if match is not None:
            tokens.append(("ident", match.group(), i))
            i = match.end()
            continue
        if ch in _OPERATOR_CHARS:
            tokens.append(("op", ch, i))
            i += 1
            continue
        raise MathEvaluationError(
            f"表达式语法错误：位置 {i} 处的字符 {ch!r} 无法识别"
        )
    return tokens


# ----------------------------------------------------------------- 语法分析


@dataclass
class _Node:
    """解析树节点。children：unary 为 [operand]，binary 为 [left, right]，
    function 为参数列表。"""

    kind: str  # number / unary / binary / function
    value: float = 0.0  # number：字面数值（未换算）
    unit: str = ""  # number：原始单位（小写），无单位为 ""
    op: str = ""  # unary / binary：运算符
    name: str = ""  # function：函数名（小写）
    children: list["_Node"] = field(default_factory=list)
    id: int = 0


class _Parser:
    """递归下降解析：sum -> product -> unary -> primary。"""

    def __init__(self, tokens: list[tuple[str, str, int]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def _error(self, message: str) -> None:
        raise MathEvaluationError(f"表达式语法错误：{message}")

    def _peek(self) -> tuple[str, str, int] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _advance(self) -> tuple[str, str, int]:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _expect(self, char: str) -> None:
        token = self._peek()
        if token is not None and token[0] == "op" and token[1] == char:
            self.pos += 1
            return
        got = "表达式结束" if token is None else f"位置 {token[2]} 处的记号 {token[1]!r}"
        self._error(f"期望 {char!r}，但遇到{got}")

    def parse(self) -> _Node:
        if not self.tokens:
            self._error("表达式为空")
        node = self._parse_sum(0)
        token = self._peek()
        if token is not None:
            self._error(f"位置 {token[2]} 处有多余的记号 {token[1]!r}")
        return node

    def _parse_sum(self, depth: int) -> _Node:
        if depth > _MAX_DEPTH:
            self._error(f"表达式嵌套过深（超过 {_MAX_DEPTH} 层）")
        node = self._parse_product(depth)
        while True:
            token = self._peek()
            if token is None or token[0] != "op" or token[1] not in "+-":
                return node
            op = self._advance()[1]
            rhs = self._parse_product(depth)
            node = _Node(kind="binary", op=op, children=[node, rhs])

    def _parse_product(self, depth: int) -> _Node:
        node = self._parse_unary(depth)
        while True:
            token = self._peek()
            if token is None or token[0] != "op" or token[1] not in "*/":
                return node
            op = self._advance()[1]
            rhs = self._parse_unary(depth)
            node = _Node(kind="binary", op=op, children=[node, rhs])

    def _parse_unary(self, depth: int) -> _Node:
        token = self._peek()
        if token is not None and token[0] == "op" and token[1] in "+-":
            op = self._advance()[1]
            operand = self._parse_unary(depth)
            return _Node(kind="unary", op=op, children=[operand])
        return self._parse_primary(depth)

    def _parse_primary(self, depth: int) -> _Node:
        token = self._peek()
        if token is None:
            self._error("表达式意外结束")
        kind, text, pos = token
        if kind == "num":
            self._advance()
            return self._parse_number(text)
        if kind == "ident":
            self._advance()
            return self._parse_function(text, pos, depth)
        if kind == "op" and text == "(":
            self._advance()
            node = self._parse_sum(depth + 1)
            self._expect(")")
            return node
        self._error(f"位置 {pos} 处意外的记号 {text!r}")

    def _parse_number(self, text: str) -> _Node:
        unit = ""
        token = self._peek()
        if token is not None and token[0] == "ident":
            self._advance()
            unit = token[1].lower()
            if unit not in _LENGTH_UNITS:
                self._error(
                    f"位置 {token[2]} 处不支持的单位 {token[1]!r}："
                    "仅支持 px / em / rem / vw / vh / % 或无单位数"
                )
        elif token is not None and token[0] == "op" and token[1] == "%":
            self._advance()
            unit = "%"
        return _Node(kind="number", value=float(text), unit=unit)

    def _parse_function(self, text: str, pos: int, depth: int) -> _Node:
        name = text.lower()
        if name not in _FUNCTIONS:
            self._error(
                f"位置 {pos} 处不支持的函数 {text!r}："
                "仅支持 calc() / min() / max() / clamp()"
            )
        self._expect("(")
        token = self._peek()
        if token is not None and token[0] == "op" and token[1] == ")":
            self._error(f"函数 {name}() 缺少参数")
        args = [self._parse_sum(depth + 1)]
        while True:
            token = self._peek()
            if token is None or token[0] != "op" or token[1] != ",":
                break
            self._advance()
            args.append(self._parse_sum(depth + 1))
        self._expect(")")
        return _Node(kind="function", name=name, children=args)


def _assign_ids(root: _Node) -> None:
    """按先序为节点编号（根为 1），供 AST 与求值轨迹交叉引用。"""
    counter = 0

    def walk(node: _Node) -> None:
        nonlocal counter
        counter += 1
        node.id = counter
        for child in node.children:
            walk(child)

    walk(root)


def _ast_out(node: _Node) -> MathAstNodeOut:
    if node.kind == "number":
        return MathAstNodeOut(
            id=node.id, type="number", value=node.value, unit=node.unit
        )
    if node.kind == "unary":
        return MathAstNodeOut(
            id=node.id,
            type="unary",
            op=node.op,
            operand=_ast_out(node.children[0]),
        )
    if node.kind == "binary":
        return MathAstNodeOut(
            id=node.id,
            type="binary",
            op=node.op,
            left=_ast_out(node.children[0]),
            right=_ast_out(node.children[1]),
        )
    return MathAstNodeOut(
        id=node.id,
        type="function",
        name=node.name,
        args=[_ast_out(child) for child in node.children],
    )


# ----------------------------------------------------------------- 求值


class _Evaluator:
    """后序求值并记录轨迹。值用 (数值, 维度) 表示，维度为
    "number"（无单位）或 "length"（已规范化为 px）。"""

    def __init__(self, context: MathContext) -> None:
        self.context = context
        self.trace: list[MathTraceStepOut] = []

    @staticmethod
    def _dim_label(dim: str) -> str:
        return "长度" if dim == "length" else "无单位数"

    def _factor(self, unit: str) -> tuple[float, str, float]:
        """单位 -> (换算系数, context 字段名, 字段值)；缺上下文时报 422。"""
        field_name = _CONTEXT_FIELD[unit]
        base = getattr(self.context, field_name)
        if base is None:
            raise MathEvaluationError(
                f"缺少换算上下文：单位 {unit!r} 需要 context.{field_name}"
            )
        factor = base / 100.0 if unit in ("vw", "vh", "%") else base
        return factor, field_name, base

    @staticmethod
    def _check_finite(value: float, what: str) -> float:
        if not math.isfinite(value):
            raise MathEvaluationError(f"非有限结果：{what} 的结果为 {value!r}")
        return value

    def _step(self, node: _Node, kind: str, detail: str, value: float, dim: str) -> None:
        self.trace.append(
            MathTraceStepOut(
                node_id=node.id,
                kind=kind,
                detail=detail,
                value=_round(value),
                unit="px" if dim == "length" else "",
            )
        )

    def run(self, node: _Node) -> tuple[float, str]:
        return self._eval(node)

    def _eval(self, node: _Node) -> tuple[float, str]:
        if node.kind == "number":
            return self._eval_number(node)
        if node.kind == "unary":
            return self._eval_unary(node)
        if node.kind == "binary":
            return self._eval_binary(node)
        return self._eval_function(node)

    def _eval_number(self, node: _Node) -> tuple[float, str]:
        value = node.value
        if not math.isfinite(value):
            raise MathEvaluationError(
                f"非有限结果：数值字面值 {value!r} 超出有限数值范围"
            )
        if node.unit == "":
            self._step(node, "number", f"无单位数 {_fmt(value)}", value, "number")
            return value, "number"
        if node.unit == "px":
            self._step(node, "number", f"长度 {_fmt(value)}px", value, "length")
            return value, "length"
        factor, field_name, base = self._factor(node.unit)
        result = self._check_finite(
            value * factor, f"{_fmt(value)}{node.unit} 按 {field_name} 换算"
        )
        self._step(
            node,
            "number",
            f"{_fmt(value)}{node.unit} 按 {field_name}={_fmt(base)} "
            f"换算为 {_fmt(result)}px",
            result,
            "length",
        )
        return result, "length"

    def _eval_unary(self, node: _Node) -> tuple[float, str]:
        value, dim = self._eval(node.children[0])
        if node.op == "+":
            self._step(
                node, "positive", f"正号保持不变：{_fmt_val(value, dim)}", value, dim
            )
            return value, dim
        result = -value
        self._step(
            node,
            "negate",
            f"取负：-({_fmt_val(value, dim)}) = {_fmt_val(result, dim)}",
            result,
            dim,
        )
        return result, dim

    def _eval_binary(self, node: _Node) -> tuple[float, str]:
        left, dim_l = self._eval(node.children[0])
        right, dim_r = self._eval(node.children[1])
        text_l = _fmt_val(left, dim_l)
        text_r = _fmt_val(right, dim_r)
        op = node.op
        if op in "+-":
            if dim_l != dim_r:
                raise MathEvaluationError(
                    f"维度冲突：{'加法' if op == '+' else '减法'}两侧必须同维，"
                    f"左侧为{self._dim_label(dim_l)}、右侧为{self._dim_label(dim_r)}"
                )
            value = left + right if op == "+" else left - right
            kind = "add" if op == "+" else "subtract"
            dim = dim_l
        elif op == "*":
            if dim_l == dim_r == "length":
                raise MathEvaluationError(
                    "维度冲突：乘法最多一侧带维度，两侧均为长度"
                )
            value = left * right
            kind = "multiply"
            dim = "length" if "length" in (dim_l, dim_r) else "number"
        else:  # "/"
            if dim_r != "number":
                raise MathEvaluationError(
                    "维度冲突：除法的除数必须为非零无单位数，实际为长度"
                )
            if right == 0:
                raise MathEvaluationError(
                    "除数为 0：除法仅允许非零无单位数作除数"
                )
            value = left / right
            kind = "divide"
            dim = dim_l
        self._check_finite(value, f"{text_l} {op} {text_r}")
        self._step(
            node, kind, f"{text_l} {op} {text_r} = {_fmt_val(value, dim)}", value, dim
        )
        return value, dim

    def _eval_function(self, node: _Node) -> tuple[float, str]:
        name = node.name
        if name == "calc":
            if len(node.children) != 1:
                raise MathEvaluationError(
                    f"calc() 只接受 1 个表达式参数，实际 {len(node.children)} 个"
                )
            value, dim = self._eval(node.children[0])
            self._step(
                node, "calc", f"calc() 透明包装：{_fmt_val(value, dim)}", value, dim
            )
            return value, dim

        if name == "clamp" and len(node.children) != 3:
            raise MathEvaluationError(
                "clamp() 需要 3 个参数（最小值, 首选值, 最大值），"
                f"实际 {len(node.children)} 个"
            )
        values: list[float] = []
        dims: list[str] = []
        for child in node.children:
            v, d = self._eval(child)
            values.append(v)
            dims.append(d)
        base_dim = dims[0]
        for i, d in enumerate(dims[1:], start=2):
            if d != base_dim:
                raise MathEvaluationError(
                    f"维度冲突：{name}() 的所有参数必须同维，"
                    f"第 1 个参数为{self._dim_label(base_dim)}、"
                    f"第 {i} 个参数为{self._dim_label(d)}"
                )
        args_text = ", ".join(_fmt_val(v, base_dim) for v in values)
        if name == "min":
            value = min(values)
            detail = f"min({args_text}) = {_fmt_val(value, base_dim)}"
        elif name == "max":
            value = max(values)
            detail = f"max({args_text}) = {_fmt_val(value, base_dim)}"
        else:  # clamp：按 CSS 语义取 max(下限, min(首选值, 上限))
            lo, preferred, hi = values
            value = max(lo, min(preferred, hi))
            detail = (
                f"clamp({args_text}) = max({_fmt_val(lo, base_dim)}, "
                f"min({_fmt_val(preferred, base_dim)}, {_fmt_val(hi, base_dim)})) "
                f"= {_fmt_val(value, base_dim)}"
            )
        self._step(node, name, detail, value, base_dim)
        return value, base_dim


# ----------------------------------------------------------------- 总入口


def _validate_context(context: MathContext) -> None:
    for field_name in _CONTEXT_FIELDS:
        value = getattr(context, field_name)
        if value is None:
            continue
        if not math.isfinite(value):
            raise MathEvaluationError(
                f"context.{field_name} 必须为有限数值，收到 {value!r}"
            )
        if value < 0:
            raise MathEvaluationError(
                f"context.{field_name} 必须为非负数值，收到 {value}"
            )


def evaluate_math(req: MathEvaluateRequest) -> MathEvaluateResponse:
    _validate_context(req.context)
    root = _Parser(_tokenize(req.expression)).parse()
    _assign_ids(root)
    evaluator = _Evaluator(req.context)
    value, dim = evaluator.run(root)
    return MathEvaluateResponse(
        expression=req.expression,
        value=_round(value),
        unit="px" if dim == "length" else "",
        ast=_ast_out(root),
        trace=evaluator.trace,
    )
