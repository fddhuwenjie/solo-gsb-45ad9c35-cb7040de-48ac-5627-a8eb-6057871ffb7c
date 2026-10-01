"""CSS 选择器解析与匹配。

支持：标签、*、#id、.class、[attr]、[attr=value]、后代组合符（空格）、子组合符（>）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import ElementInfo


class SelectorSyntaxError(ValueError):
    def __init__(self, message: str, selector: str):
        super().__init__(f"选择器语法错误：{message}（选择器 {selector!r}）")
        self.selector = selector


@dataclass
class Compound:
    """一个复合选择器（不含组合符）。"""

    tag: str | None = None
    id: str | None = None
    classes: list[str] = field(default_factory=list)
    attrs: list[tuple[str, str | None]] = field(default_factory=list)  # (名称, 值)；值为 None 表示仅存在性

    def specificity(self) -> tuple[int, int, int]:
        a = 1 if self.id else 0
        b = len(self.classes) + len(self.attrs)
        c = 1 if self.tag and self.tag != "*" else 0
        return (a, b, c)

    def matches(self, el: ElementInfo) -> bool:
        if self.tag and self.tag != "*" and self.tag != el.tag.lower():
            return False
        if self.id is not None and el.id != self.id:
            return False
        if any(cls not in el.classes for cls in self.classes):
            return False
        for name, value in self.attrs:
            if name not in el.attributes:
                return False
            if value is not None and el.attributes[name] != value:
                return False
        return True


_ID_RE = re.compile(r"#([\w-]+)")
_CLASS_RE = re.compile(r"\.([\w-]+)")
_ATTR_RE = re.compile(r"\[([^\[\]]*)\]")
_TAG_RE = re.compile(r"[a-zA-Z][\w-]*|\*")


def _parse_attr(content: str, selector: str) -> tuple[str, str | None]:
    content = content.strip()
    if not content:
        raise SelectorSyntaxError("空的属性选择器", selector)
    if "=" not in content:
        return content, None
    name, _, value = content.partition("=")
    name, value = name.strip(), value.strip()
    if not name or not value:
        raise SelectorSyntaxError(f"属性选择器格式错误：[{content}]", selector)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return name, value


def parse_compound(text: str, selector: str) -> Compound:
    comp = Compound()
    pos = 0
    m = _TAG_RE.match(text, pos)
    if m:
        comp.tag = m.group(0).lower()
        pos = m.end()
    while pos < len(text):
        ch = text[pos]
        if ch == "#":
            m = _ID_RE.match(text, pos)
            if m and comp.id is None:
                comp.id = m.group(1)
            elif m:
                raise SelectorSyntaxError("一个复合选择器中只允许一个 #id", selector)
        elif ch == ".":
            m = _CLASS_RE.match(text, pos)
            if m:
                comp.classes.append(m.group(1))
        elif ch == "[":
            m = _ATTR_RE.match(text, pos)
            if m:
                comp.attrs.append(_parse_attr(m.group(1), selector))
        else:
            m = None
        if not m:
            raise SelectorSyntaxError(f"无法解析的片段 {text[pos:]!r}", selector)
        pos = m.end()
    if comp.tag is None and comp.id is None and not comp.classes and not comp.attrs:
        raise SelectorSyntaxError(f"空的复合选择器 {text!r}", selector)
    return comp


def tokenize(selector: str) -> list[str]:
    """把选择器切成 token：复合选择器字符串、'>'、' '（后代组合符）。"""
    tokens: list[str] = []
    buf: list[str] = []
    depth = 0
    i, n = 0, len(selector)

    def flush() -> None:
        if buf:
            tokens.append("".join(buf))
            buf.clear()

    while i < n:
        ch = selector[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth < 0:
                raise SelectorSyntaxError("存在未匹配的 ']'", selector)
        if depth == 0 and ch == ">":
            flush()
            if not tokens or tokens[-1] in (" ", ">"):
                raise SelectorSyntaxError("'>' 前缺少选择器", selector)
            tokens.append(">")
            i += 1
            while i < n and selector[i].isspace():
                i += 1
            continue
        if depth == 0 and ch.isspace():
            flush()
            j = i
            while j < n and selector[j].isspace():
                j += 1
            if j < n and selector[j] == ">":
                i = j  # '>' 下一轮处理，空白不视为后代组合符
                continue
            if tokens and tokens[-1] not in (" ", ">"):
                tokens.append(" ")
            i = j
            continue
        buf.append(ch)
        i += 1
    if depth != 0:
        raise SelectorSyntaxError("属性选择器 '[' 未闭合", selector)
    flush()
    if tokens and tokens[-1] in (" ", ">"):
        raise SelectorSyntaxError("选择器以组合符结尾", selector)
    return tokens


def parse_selector(selector: str) -> tuple[list[Compound], list[str]]:
    """返回 (复合选择器列表, 组合符列表)。组合符列表长度 = 复合选择器数 - 1。"""
    tokens = tokenize(selector)
    if not tokens:
        raise SelectorSyntaxError("空选择器", selector)
    compounds: list[Compound] = []
    combinators: list[str] = []
    for idx, tok in enumerate(tokens):
        if idx % 2 == 0:
            if tok in (" ", ">"):
                raise SelectorSyntaxError("组合符位置错误", selector)
            compounds.append(parse_compound(tok, selector))
        else:
            if tok not in (" ", ">"):
                raise SelectorSyntaxError(f"两个复合选择器之间缺少组合符：{tok!r}", selector)
            combinators.append(tok)
    return compounds, combinators


def selector_specificity(compounds: list[Compound]) -> tuple[int, int, int]:
    a = b = c = 0
    for comp in compounds:
        ca, cb, cc = comp.specificity()
        a, b, c = a + ca, b + cb, c + cc
    return (a, b, c)


def selector_matches(
    compounds: list[Compound],
    combinators: list[str],
    element: ElementInfo,
    ancestors: list[ElementInfo],
) -> bool:
    """从右向左匹配。ancestors 按 根 -> 父 排列。"""
    els = [element] + list(reversed(ancestors))  # 自身 -> 父 -> ... -> 根
    if not compounds or not compounds[-1].matches(els[0]):
        return False
    pos = 0
    for i in range(len(compounds) - 1, 0, -1):
        comb = combinators[i - 1]
        target = compounds[i - 1]
        if comb == ">":
            pos += 1
            if pos >= len(els) or not target.matches(els[pos]):
                return False
        else:  # 后代组合符：向上找最近的匹配祖先
            found = False
            for p in range(pos + 1, len(els)):
                if target.matches(els[p]):
                    pos = p
                    found = True
                    break
            if not found:
                return False
    return True
