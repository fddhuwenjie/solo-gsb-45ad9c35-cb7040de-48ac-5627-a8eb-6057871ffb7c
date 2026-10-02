"""计算引擎：/compute 的编排层。

求解逻辑全部在不可变整树分析内核（analysis.py）中：一次解析规则集，
对目标元素链求解，再把内核结果渲染为旧版 ComputeResponse。
"""
from __future__ import annotations

from .analysis import RuleSet, solve_chain
from .models import ComputeRequest, ComputeResponse


def compute(req: ComputeRequest) -> ComputeResponse:
    node = solve_chain(RuleSet.build(req.rules, req.layers), req.element, req.ancestors)
    return ComputeResponse(
        computed=node.computed(),
        custom_properties=node.custom_properties(),
        invalid=node.invalid(),
        evidence=node.evidence(),
        custom_property_trace=node.custom_trace(),
    )
