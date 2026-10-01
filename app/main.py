"""FastAPI 应用入口。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .engine import compute
from .audit import AuditValidationError, audit_stylesheet
from .color_mix import ColorMixError, mix_colors
from .counters import CounterRenderError, render_counter_tree
from .dom_impact import DomImpactValidationError, analyze_dom_impact
from .gamut_map import GamutMapError, gamut_map
from .grid_tracks import GridTracksError, solve_grid_tracks
from .keyframe import KeyframeValidationError, sample_keyframes
from .math_eval import MathEvaluationError, evaluate_math
from .models import (
    ColorMixRequest,
    ColorMixResponse,
    ComputePreviewResponse,
    ComputeRequest,
    ComputeResponse,
    CounterRenderRequest,
    CounterRenderResponse,
    DomImpactRequest,
    DomImpactResponse,
    GamutMapRequest,
    GamutMapResponse,
    GridTracksRequest,
    GridTracksResponse,
    KeyframeSampleRequest,
    KeyframeSampleResponse,
    MathEvaluateRequest,
    MathEvaluateResponse,
    OverlapRequest,
    OverlapResponse,
    StylesheetAuditRequest,
    StylesheetAuditResponse,
)
from .overlap import selector_overlap
from .preview import PatchError, compute_with_preview
from .selector import SelectorSyntaxError

app = FastAPI(
    title="CSS Cascade Explainer",
    version="1.0.0",
    description="CSS 层叠解释 API：按来源 / 层 / important / 特异性 / 源码次序计算声明胜出者，"
    "解析自定义属性与 var()，并给出逐步淘汰证据；另含选择器可命中性判断、"
    " CSS 数值属性关键帧采样、CSS 计数器元素树渲染、CSS 数学表达式求值、"
    "单行 CSS Grid 列宽求解、color-mix() 风格两色混合、OKLCH -> sRGB 色域映射，"
    "在 DOM 样本树上逐节点审计可安全删除声明批次的样式表清理，"
    "以及对节点做一次原子 DOM 变更后的整树样式影响分析。",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/compute",
    response_model=None,
    responses={200: {"model": ComputePreviewResponse}},
)
def compute_endpoint(req: ComputeRequest) -> ComputeResponse:
    """层叠计算；请求携带非空 patches 时做样式变更预演，在基线结果之外另附
    preview（基线 / 补丁后快照、逐项差异、因果轨迹与未生效补丁）。
    补丁任一定位非法则整次请求 422，不产生部分结果。"""
    try:
        if req.patches:
            return compute_with_preview(req)
        return compute(req)
    except SelectorSyntaxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/stylesheet-audit", response_model=StylesheetAuditResponse)
def stylesheet_audit_endpoint(req: StylesheetAuditRequest) -> StylesheetAuditResponse:
    """样式表清理审计：在 DOM 样本树上逐节点复用选择器匹配与层叠 / var()
    求值，按 rule_index、declaration_index 顺序逐条试删并重新计算整棵树，
    返回实际可同时删除的声明批次，以及每个保留声明的首个冲突节点、属性与
    阻断原因。删除后出现新失效、解除原失效或改变胜出者的声明不纳入批次。
    重复 id、空节点或非法规则返回 422。"""
    try:
        return audit_stylesheet(req)
    except SelectorSyntaxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AuditValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/dom-impact", response_model=DomImpactResponse)
def dom_impact_endpoint(req: DomImpactRequest) -> DomImpactResponse:
    """DOM 变更样式影响分析：对树中指定节点做一次原子变更（添加 / 移除 class、
    设置 / 删除 attribute、修改 tag），先求基线再整树重算，返回所有 computed、
    custom_properties 或 invalid 发生变化的节点及其前后值，并区分选择器命中、
    层叠胜出者、继承或 var() 链传播原因，未受影响节点不列出。
    目标不存在、重复 id、空操作或非法规则返回 422，不产生部分结果。"""
    try:
        return analyze_dom_impact(req)
    except SelectorSyntaxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (DomImpactValidationError, AuditValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/selector-overlap", response_model=OverlapResponse)
def selector_overlap_endpoint(req: OverlapRequest) -> OverlapResponse:
    """不提供 DOM 样本，判断两条选择器能否命中同一目标元素。"""
    try:
        return selector_overlap(req.selector_a, req.selector_b)
    except SelectorSyntaxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/keyframe-sample", response_model=KeyframeSampleResponse)
def keyframe_sample_endpoint(req: KeyframeSampleRequest) -> KeyframeSampleResponse:
    """在查询时刻对单个 CSS 数值属性按关键帧与缓动函数采样。"""
    try:
        return sample_keyframes(req)
    except KeyframeValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/counter-render", response_model=CounterRenderResponse)
def counter_render_endpoint(req: CounterRenderRequest) -> CounterRenderResponse:
    """按 reset -> set -> increment -> 渲染顺序先序遍历元素树，解析
    counter() / counters() 并返回逐节点文本与作用域变更轨迹。"""
    try:
        return render_counter_tree(req)
    except CounterRenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/math-evaluate", response_model=MathEvaluateResponse)
def math_evaluate_endpoint(req: MathEvaluateRequest) -> MathEvaluateResponse:
    """计算 CSS 数学表达式：calc() / min() / max() / clamp() 与 + - * /，
    按 context 把 em / rem / vw / vh / % 规范化为 px，返回规范化数值、
    单位、AST 与逐节点求值轨迹。"""
    try:
        return evaluate_math(req)
    except MathEvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/grid-tracks", response_model=GridTracksResponse)
def grid_tracks_endpoint(req: GridTracksRequest) -> GridTracksResponse:
    """求解单行 CSS Grid 的列宽：解析 px / % / fr / minmax() 轨道，按跨度
    从短到长处理项目的 min_content 缺口（触顶份额回流重分），再把剩余空间
    迭代分配给 fr 轨道（份额低于基础尺寸的轨道先固定再重算），返回各列
    基础尺寸、上限、最终尺寸与逐轮轨迹。"""
    try:
        return solve_grid_tracks(req)
    except GridTracksError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/color-mix", response_model=ColorMixResponse)
def color_mix_endpoint(req: ColorMixRequest) -> ColorMixResponse:
    """按 CSS Color 的 color-mix() 规则混合两种颜色：解析 hex/rgb()/hsl()，
    在 srgb / srgb-linear / hsl / oklab / oklch 空间中以预乘 alpha 插值
    （极坐标空间按 shorter/longer/increasing/decreasing 弧线处理 hue），
    权重补齐归一化后转回 sRGB 逐通道裁剪，返回最终 rgba、目标空间坐标、
    权重处理与转换、插值轨迹。"""
    try:
        return mix_colors(req)
    except ColorMixError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/gamut-map", response_model=GamutMapResponse)
def gamut_map_endpoint(req: GamutMapRequest) -> GamutMapResponse:
    """把 CSS oklch() 颜色安全映射进 sRGB 色域：原色已在色域内时原样
    返回，否则固定 L、h 与 alpha，在 [0, 原 C] 上二分查找仍处于色域内
    的最大 C（区间宽度不大于 tolerance 时结束），避免逐通道硬裁剪造成
    色相偏移。返回原始与映射后的 OKLCH、未裁剪及最终 sRGB、色度损失、
    是否映射与二分每轮上下界。"""
    try:
        return gamut_map(req)
    except GamutMapError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
