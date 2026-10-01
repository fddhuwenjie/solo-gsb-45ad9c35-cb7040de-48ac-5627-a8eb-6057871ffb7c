"""CSS 计数器（counter-reset / counter-set / counter-increment / counter() /
counters()）的元素树渲染。

每个计数器维护一个实例栈（外 -> 内）：

- counter-reset：以给定值（缺省 0）新建实例并**压栈**，只在当前节点子树内
  可见；退出节点时弹栈，恢复被遮蔽的外层实例（或销毁）。
- counter-set / counter-increment：作用于栈顶实例；计数器尚不存在时
  **先以 0 建立实例**（惰性建立的实例同样在退出节点时弹栈）。
- counter(name, style)：读取最内层（栈顶）值并按 style 格式化；
  引用不存在的计数器是错误（HTTP 422）。
- counters(name, separator, style)：把栈内值从外到内逐个格式化并以
  separator 连接。

处理顺序为每个节点先序执行 reset -> set -> increment -> 渲染 content，
随后递归 children，退出时按压栈逆序弹栈。
"""
from __future__ import annotations

from .models import (
    CounterElement,
    CounterNodeOut,
    CounterRenderRequest,
    CounterRenderResponse,
    CounterScopeEventOut,
    ContentFragment,
)

# 支持的计数样式及其规范名
_STYLES = {
    "decimal": "decimal",
    "decimal-leading-zero": "decimal-leading-zero",
    "lower-alpha": "lower-alpha",
    "upper-alpha": "upper-alpha",
    "lower-latin": "lower-alpha",
    "upper-latin": "upper-alpha",
}

_ALPHABET = 26


class CounterRenderError(ValueError):
    """计数器树不满足渲染前提（映射为 HTTP 422）。"""


# ----------------------------------------------------------------- 计数样式


def _format_alpha(value: int, upper: bool) -> str:
    """大小写字母格式：1->a..z、27->aa；非正数超出字母样式范围，按 CSS
    规范回退为十进制。"""
    if value <= 0:
        return str(value)
    chars: list[str] = []
    while value > 0:
        value -= 1
        chars.append(chr(ord("a" if not upper else "A") + value % _ALPHABET))
        value //= _ALPHABET
    return "".join(reversed(chars))


def _format_value(value: int, style: str) -> str:
    canonical = _STYLES.get(style)
    if canonical is None:
        raise CounterRenderError(
            f"不支持的计数格式 {style!r}：仅支持 decimal / decimal-leading-zero /"
            " lower-alpha（lower-latin）/ upper-alpha（upper-latin）"
        )
    if canonical == "decimal":
        return str(value)
    if canonical == "decimal-leading-zero":
        # CSS 规范：1..9 补前导零，其余按普通十进制
        return f"{value:02d}" if 0 <= value <= 9 else str(value)
    return _format_alpha(value, canonical == "upper-alpha")


# ----------------------------------------------------------------- 作用域


class _Scope:
    """所有计数器的实例栈集合。"""

    def __init__(self) -> None:
        self._stacks: dict[str, list[int]] = {}
        # 本节点压入的实例记录（计数器名），退出时逆序弹出
        self._pushes: dict[int, list[str]] = {}

    def reset(
        self, name: str, value: int, node_key: int
    ) -> CounterScopeEventOut:
        stack = self._stacks.setdefault(name, [])
        shadow = stack[-1] if stack else None
        stack.append(value)
        self._pushes.setdefault(node_key, []).append(name)
        return CounterScopeEventOut(
            phase="enter", type="reset", name=name, value=value, shadow=shadow
        )

    def _ensure(self, name: str, node_key: int) -> list[CounterScopeEventOut]:
        """set / increment 前确保实例存在；缺失时先以 0 建立。"""
        stack = self._stacks.setdefault(name, [])
        if stack:
            return []
        stack.append(0)
        self._pushes.setdefault(node_key, []).append(name)
        return [
            CounterScopeEventOut(
                phase="enter", type="create", name=name, value=0, shadow=None
            )
        ]

    def set_value(
        self, name: str, value: int, node_key: int
    ) -> list[CounterScopeEventOut]:
        events = self._ensure(name, node_key)
        self._stacks[name][-1] = value
        events.append(
            CounterScopeEventOut(
                phase="enter", type="set", name=name, value=value
            )
        )
        return events

    def increment(
        self, name: str, delta: int, node_key: int
    ) -> list[CounterScopeEventOut]:
        events = self._ensure(name, node_key)
        new_value = self._stacks[name][-1] + delta
        self._stacks[name][-1] = new_value
        events.append(
            CounterScopeEventOut(
                phase="enter", type="increment", name=name, value=new_value
            )
        )
        return events

    def counter_value(self, name: str) -> int:
        stack = self._stacks.get(name)
        if not stack:
            raise CounterRenderError(
                f"counter({name!r}) 引用了不存在的计数器：此前没有任何 "
                "counter-reset / counter-set / counter-increment 在作用域内建立该计数器"
            )
        return stack[-1]

    def counters_values(self, name: str) -> list[int]:
        stack = self._stacks.get(name)
        if not stack:
            raise CounterRenderError(
                f"counters({name!r}) 引用了不存在的计数器：此前没有任何 "
                "counter-reset / counter-set / counter-increment 在作用域内建立该计数器"
            )
        return list(stack)

    def exit_node(self, node_key: int) -> list[CounterScopeEventOut]:
        """弹出本节点压入的全部实例（逆序），生成恢复 / 销毁轨迹。"""
        events: list[CounterScopeEventOut] = []
        for name in reversed(self._pushes.pop(node_key, [])):
            stack = self._stacks[name]
            stack.pop()
            if stack:
                events.append(
                    CounterScopeEventOut(
                        phase="exit",
                        type="restore",
                        name=name,
                        value=stack[-1],
                    )
                )
            else:
                del self._stacks[name]
                events.append(
                    CounterScopeEventOut(
                        phase="exit", type="destroy", name=name
                    )
                )
        return events


# ----------------------------------------------------------------- 渲染


def _validate_fragment(frag: ContentFragment, path: str) -> None:
    if frag.kind == "text":
        return
    if not frag.name or not _valid_name(frag.name):
        raise CounterRenderError(
            f"{path}：{frag.kind} 片段使用了非法计数器名称 "
            f"{frag.name!r}：名称不能为空，且只能包含字母、数字、连字符、"
            "下划线，且不能以数字或连字符开头"
        )
    if frag.style is not None and frag.style not in _STYLES:
        raise CounterRenderError(
            f"{path}：不支持的计数格式 {frag.style!r}（计数器 {frag.name!r}），"
            "仅支持 decimal / decimal-leading-zero / lower-alpha（lower-latin）"
            "/ upper-alpha（upper-latin）"
        )
    if frag.kind == "counters" and frag.separator is None:
        raise CounterRenderError(
            f"{path}：counters({frag.name!r}) 缺少连接分隔符 separator"
        )


def _validate_tree(node: CounterElement, seen: set) -> None:
    if node.id in seen:
        raise CounterRenderError(f"节点 id 重复：{node.id!r}")
    seen.add(node.id)

    groups = (
        ("counter_reset", node.counter_reset),
        ("counter_set", node.counter_set),
        ("counter_increment", node.counter_increment),
    )
    for field, ops in groups:
        for i, op in enumerate(ops):
            if op.op != field.removeprefix("counter_"):
                # 模型层已按 op 字段解析，这里防止操作放进错误的数组
                raise CounterRenderError(
                    f"节点 {node.id!r} 的 {field}[{i}] 的 op 为 {op.op!r}，"
                    f"应放在 counter_{op.op} 数组中"
                )
            if not _valid_name(op.name):
                raise CounterRenderError(
                    f"节点 {node.id!r} 的 {field}[{i}] 使用了非法计数器名称 "
                    f"{op.name!r}：名称不能为空，且只能包含字母、数字、连字符、"
                    "下划线，且不能以数字或连字符开头"
                )

    for i, frag in enumerate(node.content):
        _validate_fragment(frag, f"节点 {node.id!r} 的 content[{i}]")

    for child in node.children:
        _validate_tree(child, seen)


def _valid_name(name: str) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if name[0].isdigit() or name[0] == "-":
        return False
    return all(c.isalnum() or c in "-_" for c in name)


def _render_content(node: CounterElement, scope: _Scope) -> str:
    parts: list[str] = []
    for frag in node.content:
        if frag.kind == "text":
            parts.append(frag.value)
        elif frag.kind == "counter":
            style = frag.style or "decimal"
            parts.append(
                _format_value(scope.counter_value(frag.name), style)
            )
        else:  # counters
            style = frag.style or "decimal"
            values = scope.counters_values(frag.name)
            parts.append(
                frag.separator.join(_format_value(v, style) for v in values)
            )
    return "".join(parts)


def render_counter_tree(req: CounterRenderRequest) -> CounterRenderResponse:
    _validate_tree(req.root, set())

    scope = _Scope()
    out: list[CounterNodeOut] = []
    key_counter = 0

    def walk(node: CounterElement) -> None:
        nonlocal key_counter
        key_counter += 1
        node_key = key_counter

        enter: list[CounterScopeEventOut] = []
        # 顺序：reset -> set -> increment；各组内按书写顺序
        for op in node.counter_reset:
            enter.append(
                scope.reset(op.name, op.value if op.value is not None else 0, node_key)
            )
        for op in node.counter_set:
            enter.extend(
                scope.set_value(
                    op.name, op.value if op.value is not None else 0, node_key
                )
            )
        for op in node.counter_increment:
            enter.extend(
                scope.increment(
                    op.name, op.value if op.value is not None else 1, node_key
                )
            )

        text = _render_content(node, scope)

        node_out = CounterNodeOut(id=node.id, text=text, enter=enter)
        out.append(node_out)  # 先序：进入时即占位列结果

        for child in node.children:
            walk(child)

        node_out.exit = scope.exit_node(node_key)

    walk(req.root)
    return CounterRenderResponse(nodes=out)
