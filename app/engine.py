"""/compute 计算入口：薄封装。

真正的层叠、自定义属性继承 / 循环 / var() 求解与证据生成全部在不可变
整树内核 kernel.py 中完成，与 /stylesheet-audit、/dom-impact 和补丁
预演共享同一套解析、节点状态与签名。本模块只负责把 ComputeRequest
交给内核并拼装旧版 ComputeResponse。
"""
from __future__ import annotations

from .kernel import ParsedRule, RuleSet, analyze_chain
from .models import ComputeRequest, ComputeResponse

# 兼容性别名：preview / audit 等模块及外部调用方仍可从 engine 导入这些名字
__all__ = [
    "compute",
    "solve",
    "parse_rules",
    "ParsedRule",
    "RuleSet",
    "_solve",
    "_parse_rules",
]


def parse_rules(rules) -> tuple[ParsedRule, ...]:
    """预解析所有规则的选择器 / 特异性 / 源码次序（只解析一次，复算共享）。"""
    return RuleSet.parse(rules).parsed


def solve(
    element,
    ancestors,
    layers,
    rules,
    layer_overrides: dict[tuple[int, int], str | None] | None = None,
    disabled: frozenset[tuple[int, int]] | set[tuple[int, int]] | None = None,
    parsed: tuple[ParsedRule, ...] | None = None,
):
    """对一条祖先链求解，返回内核的不可变 NodeAnalysis。"""
    ruleset = RuleSet.parse(
        rules,
        layers,
        layer_overrides=layer_overrides,
        disabled=disabled,
        parsed=parsed,
    )
    return analyze_chain(element, ancestors, ruleset)


def compute(req: ComputeRequest) -> ComputeResponse:
    node = solve(req.element, req.ancestors, req.layers, req.rules)
    return ComputeResponse(
        computed=node.computed,
        custom_properties=node.custom_properties,
        invalid=node.invalid,
        evidence=node.evidence,
        custom_property_trace=node.custom_property_trace,
    )


# 旧下划线名字（_solve / _parse_rules）的兼容别名
_solve = solve
_parse_rules = parse_rules
