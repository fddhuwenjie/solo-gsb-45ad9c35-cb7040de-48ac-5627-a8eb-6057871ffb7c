"""请求 / 响应的数据模型（Pydantic）。"""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

Origin = Literal["ua", "user", "author"]


class ElementInfo(BaseModel):
    """DOM 中一个元素的快照。祖先链按 根 -> ... -> 父 的顺序给出。"""

    tag: str = ""
    id: Optional[str] = None
    classes: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)


class Declaration(BaseModel):
    property: str
    value: str
    important: bool = False


class Rule(BaseModel):
    """一条结构化样式规则。layer 为 null 表示未分层声明。"""

    selector: str
    declarations: list[Declaration]
    origin: Origin = "author"
    layer: Optional[str] = None
    order: Optional[int] = None  # 源码次序，缺省取规则在数组中的下标


class ComputeRequest(BaseModel):
    element: ElementInfo
    ancestors: list[ElementInfo] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)  # 层声明顺序：早 -> 晚
    rules: list[Rule] = Field(default_factory=list)
    # 样式变更预演补丁：按 (rule_index, declaration_index) 定位到 rules 中某条
    # 规则块内的某条声明，可改 value / important / layer（layer 显式给 null
    # 表示把声明改为未分层）。补丁一次性整体生效：任一定位非法即整次 422。
    patches: list["DeclarationPatch"] = Field(default_factory=list)


class DeclarationPatch(BaseModel):
    """对一条既有声明的定位式修改。

    rule_index 为规则在请求 rules 数组中的下标，declaration_index 为声明在
    该规则 declarations 中的下标。value / important / layer 至少给一个；
    layer 缺省表示不改层，显式 null 表示改为未分层。
    """

    rule_index: int
    declaration_index: int
    value: Optional[str] = None
    important: Optional[bool] = None
    layer: Optional[str] = None

    @field_validator("rule_index", "declaration_index", mode="before")
    @classmethod
    def _reject_non_int(cls, v):
        # 与其它端点的下标字段同理：布尔 / 浮点不能混作下标
        if isinstance(v, bool) or isinstance(v, float):
            raise ValueError("补丁下标必须为非负整数")
        return v

    @field_validator("value")
    @classmethod
    def _value_not_null(cls, v):
        # 缺省 value 表示不改；显式 null 不允许（补丁不能删除声明的值）
        if v is None:
            raise ValueError("value 不能为 null；不修改 value 时省略该字段")
        return v

    @field_validator("important")
    @classmethod
    def _important_not_null(cls, v):
        if v is None:
            raise ValueError("important 不能为 null；只能给 true / false，不修改时省略")
        return v

    def layer_explicitly_set(self) -> bool:
        return "layer" in self.model_fields_set


# ---------- 响应 ----------


class CandidateOut(BaseModel):
    """参与某属性层叠竞争的一条候选声明。"""

    selector: str
    origin: str
    layer: Optional[str]
    important: bool
    specificity: list[int]
    source_order: int
    rule_index: int  # 规则在请求 rules 数组中的下标（补丁定位用）
    declaration_index: int  # 声明在规则 declarations 中的下标（补丁定位用）
    property: str
    value: str


class EliminatedOut(BaseModel):
    """一条被淘汰的候选及其被淘汰的阶段与原因。"""

    candidate: CandidateOut
    stage: str  # importance / origin / layer / specificity / source_order
    detail: str


class PropertyEvidenceOut(BaseModel):
    """普通属性的层叠证据。"""

    property: str
    status: str  # computed | invalid
    computed_value: Optional[str]
    winner: CandidateOut
    var_steps: list[str] = Field(default_factory=list)
    eliminated: list[EliminatedOut] = Field(default_factory=list)


class CustomPropTraceOut(BaseModel):
    """自定义属性沿祖先链逐元素的解析轨迹。"""

    element: str
    property: str
    status: str  # ok | invalid | inherited
    declared_value: Optional[str] = None  # 本元素层叠胜出的原始值（未替换 var）
    resolved: Optional[str] = None
    note: str = ""
    winner: Optional[CandidateOut] = None
    eliminated: list[EliminatedOut] = Field(default_factory=list)


class InvalidDeclOut(BaseModel):
    property: str
    value: str
    reason: str


class ComputeResponse(BaseModel):
    computed: dict[str, Optional[str]]
    custom_properties: dict[str, str]
    invalid: list[InvalidDeclOut]
    evidence: list[PropertyEvidenceOut]
    custom_property_trace: list[CustomPropTraceOut]


# ---------- 样式变更预演（POST /compute 携带 patches 时） ----------


class PatchRefOut(BaseModel):
    """对一条补丁的回显与定位。"""

    index: int  # 补丁在请求 patches 数组中的下标
    rule_index: int
    declaration_index: int


class PreviewSnapshotOut(BaseModel):
    """一个世界（基线或补丁后）的目标元素计算快照。"""

    computed: dict[str, Optional[str]]
    custom_properties: dict[str, str]
    invalid: list[InvalidDeclOut]


class PropertyChangeOut(BaseModel):
    """一项发生变化的普通属性或自定义属性。

    kind：
      - winner_changed：层叠胜出声明换了（含被改声明自身夺魁 / 失魁）；
      - var_changed：胜出声明是同一条，因其 var() 依赖的自定义属性
        （值 / 循环 / fallback 走向）变化而改值；
      - edited：胜出声明是同一条，补丁直接修改了它自己的 value；
    status_changed 标记 computed ↔ invalid 状态切换（可与任一 kind 并存）。
    对自定义属性，before_winner 仅在目标元素本就有该属性的胜出声明时非空
    （纯继承得到的自定义属性无本元素 winner）。
    """

    property: str
    kind: Literal["winner_changed", "var_changed", "edited"]
    is_custom: bool
    status_changed: bool
    before_status: str  # 普通属性 computed | invalid；自定义属性 ok | missing
    after_status: str
    before_value: Optional[str]
    after_value: Optional[str]
    before_winner: Optional[CandidateOut] = None
    after_winner: Optional[CandidateOut] = None
    before_reason: Optional[str] = None
    after_reason: Optional[str] = None
    responsible_patches: list[PatchRefOut] = Field(default_factory=list)
    trail: list[str] = Field(default_factory=list)  # 因果轨迹：被改声明 -> 最终属性


class ShadowedPatchOut(BaseModel):
    """未影响最终结果的补丁（其所在位置的声明在补丁前后都不决定任何结果，
    或移除该补丁后目标元素结果与全量补丁一致）。"""

    patch: PatchRefOut
    reason: str


class ComputePreview(BaseModel):
    """预演结果：基线与补丁后快照、逐项差异与未生效补丁。"""

    baseline: PreviewSnapshotOut
    patched: PreviewSnapshotOut
    changes: list[PropertyChangeOut]
    shadowed_patches: list[ShadowedPatchOut] = Field(default_factory=list)
    applied_patches: list[PatchRefOut] = Field(default_factory=list)


class ComputePreviewResponse(ComputeResponse):
    """携带 patches 时的响应：在原有字段（等于基线结果）之外另附 preview。"""

    preview: ComputePreview


# ---------- POST /selector-overlap ----------


class OverlapRequest(BaseModel):
    selector_a: str
    selector_b: str


class OverlapConflictOut(BaseModel):
    """不可满足时的冲突证据。

    kind:
      - compound：两段被对齐到同一节点，但其复合约束（tag / id / 属性）互斥；
        segment_b 为 null 时表示单侧某段内部自相矛盾。
      - child：子组合符（>）的刚性把两段强制压到同一节点（可经最近祖先吸附
        使弹性后代段占位后逐层传播），无后代间隙可规避，而两段约束互斥。
    段下标按选择器书写顺序（0 起，最右一段即目标段）。
    """

    kind: Literal["compound", "child"]
    segment_a: Optional[int] = None
    segment_b: Optional[int] = None
    field: Optional[str] = None  # tag / id / attr:<name>；纯结构性结论时为 null
    value_a: Optional[str] = None
    value_b: Optional[str] = None
    reason: str


class OverlapAlignmentRow(BaseModel):
    """逐深度对齐的一行（响应中按 根 -> 目标 顺序给出）。

    depth 为距目标的层数（0 即目标）；segment_a / segment_b 为该深度上
    两侧各自落位的段下标（原序），未落位为 null。combinator_a / combinator_b
    为该段朝目标方向到下一段的组合符（">" 或 " "，末段为 null）。
    """

    depth: int
    segment_a: Optional[int] = None
    segment_b: Optional[int] = None
    combinator_a: Optional[str] = None
    combinator_b: Optional[str] = None
    element: dict  # 见证节点，只包含选择器要求的 tag/id/classes/attributes 字段


class OverlapResponse(BaseModel):
    satisfiable: bool
    selector_a: str
    selector_b: str
    element: Optional[dict] = None
    ancestors: list[dict] = Field(default_factory=list)  # 根 -> 父
    alignment: list[OverlapAlignmentRow] = Field(default_factory=list)
    conflict: Optional[OverlapConflictOut] = None


# ---------- POST /keyframe-sample ----------


class KeyframeIn(BaseModel):
    """一个数值关键帧：offset ∈ [0,1]，value 为「数字 + 单位」（如 12px / 50%）。"""

    offset: float
    value: str


Direction = Literal["normal", "reverse", "alternate", "alternate-reverse"]
FillMode = Literal["none", "forwards", "backwards", "both"]


class KeyframeSampleRequest(BaseModel):
    """CSS 数值属性关键帧采样请求。

    keyframes 的偏移必须严格递增并覆盖 0、1；所有取值单位一致。
    easing 仅支持 "linear" 或 "cubic-bezier(x1,y1,x2,y2)"。
    """

    keyframes: list[KeyframeIn]
    duration: float
    delay: float = 0.0
    iteration_count: int = 1
    direction: Direction = "normal"
    fill_mode: FillMode = "none"
    easing: str = "linear"
    time: float

    @field_validator("iteration_count", mode="before")
    @classmethod
    def _positive_integer(cls, v):
        # 在 Pydantic 默认的 bool->int 归并发生之前显式拒绝布尔值；
        # 非整数值交由 int 字段校验拒绝，正整数由引擎复查
        if isinstance(v, bool):
            raise ValueError("iteration_count 必须为正整数，不能是布尔值")
        return v


class KeyframeSampleResponse(BaseModel):
    """采样结果。

    phase：before / active / after；iteration 为 0 起轮次；segment 为
    命中的帧段（其起点关键帧下标）；local_progress 为段内插值进度，
    eased_progress 为经缓动反解后的进度；value 为插值结果字符串。
    无填充的 before / after 阶段除 phase 外均为 null。
    """

    phase: str
    iteration: Optional[int]
    segment: Optional[int]
    local_progress: Optional[float]
    eased_progress: Optional[float]
    value: Optional[str]


# ---------- POST /counter-render ----------


class CounterOperation(BaseModel):
    """节点上的一条计数器操作（counter-reset / counter-set / counter-increment）。

    value 缺省时 reset / set 取 0、increment 取 1。
    """

    op: Literal["reset", "set", "increment"]
    name: str
    value: Optional[int] = None

    @field_validator("value", mode="before")
    @classmethod
    def _reject_non_int(cls, v):
        # 与 iteration_count 同理：在 Pydantic 的 bool -> int 归并前显式拒绝布尔；
        # 1.0 等浮点也拒绝，计数器操作值必须是整数
        if isinstance(v, bool) or isinstance(v, float):
            raise ValueError("计数器操作值必须为整数")
        return v


class ContentFragment(BaseModel):
    """content 的一个片段：字面文本、counter(name, style) 或
    counters(name, separator, style)。style 缺省为 decimal。"""

    kind: Literal["text", "counter", "counters"]
    value: str = ""  # text：字面文本
    name: Optional[str] = None  # counter / counters：计数器名
    separator: Optional[str] = None  # counters：连接分隔符
    style: Optional[str] = None  # decimal / decimal-leading-zero /
    # lower-alpha(lower-latin) / upper-alpha(upper-latin)

    @model_validator(mode="before")
    @classmethod
    def _string_is_text(cls, data):
        # content 数组中的裸字符串视作 text 片段
        if isinstance(data, str):
            return {"kind": "text", "value": data}
        return data


class CounterElement(BaseModel):
    """计数器渲染元素树的一个节点。

    counter_reset / counter_set / counter_increment 为计数器操作列表；
    content 为片段数组，元素可为裸字符串（字面文本）或片段对象。
    """

    id: object
    counter_reset: list[CounterOperation] = Field(default_factory=list)
    counter_set: list[CounterOperation] = Field(default_factory=list)
    counter_increment: list[CounterOperation] = Field(default_factory=list)
    content: list[ContentFragment] = Field(default_factory=list)
    children: list["CounterElement"] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _id_type(cls, v):
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ValueError("节点 id 必须为非空字符串或整数")
        if isinstance(v, str) and not v.strip():
            raise ValueError("节点 id 不能为空字符串")
        return v

    @field_validator(
        "counter_reset", "counter_set", "counter_increment", mode="before"
    )
    @classmethod
    def _wrap_single_op(cls, v):
        # 允许只写一条操作时给对象而非数组
        if isinstance(v, dict):
            return [v]
        return v


class CounterRenderRequest(BaseModel):
    root: CounterElement


class CounterScopeEventOut(BaseModel):
    """一条计数器作用域变更轨迹。

    phase：
      - enter：节点处理（reset → set → increment）阶段发生的变更，
        type 为 reset / set / increment / create；
      - exit：节点子树处理完毕、作用域退出时发生的变更，
        type 为 restore（恢复被遮蔽的外层实例）/ destroy（实例销毁）。

    type 含义：
      - reset：reset 新建实例并压栈，shadow 为被遮蔽的外层值（无外层为 null）；
      - create：set / increment 引用了不存在的计数器，先以 0 建立实例，
        其后紧接同一条操作的 set / increment 事件；
      - set / increment：写入或累加值，value 为变更后的值；
      - restore：退出时弹出 reset 实例，外层实例恢复为 value；
      - destroy：退出时弹出（reset 或惰性建立的）实例，栈中再无该计数器。
    """

    phase: Literal["enter", "exit"]
    type: Literal["reset", "create", "set", "increment", "restore", "destroy"]
    name: str
    value: Optional[int] = None
    shadow: Optional[int] = None


class CounterNodeOut(BaseModel):
    """先序遍历中一个节点的渲染结果。"""

    id: object
    text: str
    enter: list[CounterScopeEventOut] = Field(default_factory=list)
    exit: list[CounterScopeEventOut] = Field(default_factory=list)


class CounterRenderResponse(BaseModel):
    nodes: list[CounterNodeOut]


# ---------- POST /math-evaluate ----------


class MathContext(BaseModel):
    """CSS 数学表达式的换算上下文（均可选；表达式用到对应单位时必填，
    缺失即 422）。提供的值必须为非负有限数值。

    - font_size：1em 对应的 px；
    - root_font_size：1rem 对应的 px；
    - viewport_width / viewport_height：100vw / 100vh 对应的 px；
    - percentage_basis：100% 对应的 px。
    """

    font_size: Optional[float] = None
    root_font_size: Optional[float] = None
    viewport_width: Optional[float] = None
    viewport_height: Optional[float] = None
    percentage_basis: Optional[float] = None


class MathEvaluateRequest(BaseModel):
    expression: str
    context: MathContext = Field(default_factory=MathContext)


class MathAstNodeOut(BaseModel):
    """表达式 AST 节点。

    number 节点带 value / unit（原始单位，小写，无单位为 ""）；
    unary 带 op 与 operand；binary 带 op 与 left / right；
    function 带 name 与 args。id 按先序从 1 编号，与 trace 的 node_id 对应。
    """

    id: int
    type: Literal["number", "unary", "binary", "function"]
    value: Optional[float] = None
    unit: Optional[str] = None
    op: Optional[str] = None
    operand: Optional["MathAstNodeOut"] = None
    left: Optional["MathAstNodeOut"] = None
    right: Optional["MathAstNodeOut"] = None
    name: Optional[str] = None
    args: list["MathAstNodeOut"] = Field(default_factory=list)


class MathTraceStepOut(BaseModel):
    """逐节点求值轨迹的一步（后序，最后一步即根节点结果）。

    kind：number / positive / negate / add / subtract / multiply /
    divide / calc / min / max / clamp。value / unit 为该节点规范化后的
    结果（长度一律为 px，无单位时 unit 为 ""）。
    """

    node_id: int
    kind: str
    detail: str
    value: float
    unit: str


class MathEvaluateResponse(BaseModel):
    expression: str
    value: float
    unit: str  # "px" 或 ""（无单位）
    ast: MathAstNodeOut
    trace: list[MathTraceStepOut]


# ---------- POST /grid-tracks ----------


class GridItemIn(BaseModel):
    """单行 Grid 中的一个项目。

    start 为起始列号（1 起），span 为跨越列数（正整数），
    min_content 为项目的最小内容宽度（px，非负）。
    """

    start: int
    span: int = 1
    min_content: float = 0.0
    name: Optional[str] = None

    @field_validator("start", "span", mode="before")
    @classmethod
    def _reject_non_int(cls, v):
        # 与计数器操作值同理：布尔不能并入整数，跨度必须是整数
        if isinstance(v, bool) or isinstance(v, float):
            raise ValueError("start / span 必须为整数")
        return v


class GridTracksRequest(BaseModel):
    """单行 CSS Grid 列宽求解请求。

    columns 每项为一条轨道定义：px（如 "120px"）、百分比（如 "25%"）、
    fr（如 "1fr"）或 minmax(px|%, px|%|fr)（如 "minmax(100px, 1fr)"）。
    gap 为列间间距（px，非负）。
    """

    container_width: float
    gap: float = 0.0
    columns: list[str]
    items: list[GridItemIn] = Field(default_factory=list)


class GridTrackOut(BaseModel):
    """一列轨道的求解结果。

    base 为基础尺寸（px），cap 为增长上限（px；null 表示无上限），
    final_size 为最终尺寸。fr 为该轨道参与 fr 分配的因子（不参与为 0）。
    """

    index: int
    definition: str
    kind: str  # px / % / fr / minmax
    base: float
    cap: Optional[float] = None
    fr: float = 0.0
    final_size: float


class GridOverflowOut(BaseModel):
    """无法满足 min_content 的项目及其溢出缺口。

    available 为跨列轨道最终尺寸之和（不含列间 gap），
    gap 为 min_content - available 的缺口。
    """

    item_index: int
    name: Optional[str] = None
    start: int
    span: int
    min_content: float
    available: float
    gap: float


class GridMinContentRoundOut(BaseModel):
    """单个项目 min_content 分配中的一轮。

    shortfall 为本轮开始时的剩余缺口；growable 为本轮候选可增长列
    （0 起下标）；share 为每列等额份额；capped 为本轮触及上限、份额
    被收回的列；redistributed 为接收回流份额、参与下一轮的列。
    """

    shortfall: float
    growable: list[int]
    share: float
    capped: list[int] = Field(default_factory=list)
    redistributed: list[int] = Field(default_factory=list)


class GridFrRoundOut(BaseModel):
    """fr 剩余空间迭代分配中的一轮。

    free_space 为本轮待分配空间（扣除间距、固定轨道与已固定 fr 轨道的
    基础尺寸）；active 为仍按 fr 弹性增长的列；fr_sum 为其 fr 因子之和；
    fr_size 为每 1fr 对应的尺寸；frozen 为因比例份额低于基础尺寸而在
    本轮固定的列；targets 为各活动列按 max(基础, fr*fr_size) 的目标尺寸。
    """

    free_space: float
    active: list[int]
    fr_sum: float
    fr_size: float
    frozen: list[int]
    targets: dict[str, float]


class GridTraceStepOut(BaseModel):
    """逐轮轨迹的一步。

    phase 为 min_content 时描述单个项目的最小内容缺口分配
    （item_index / start / span / rounds / unmet）；phase 为 fr 时
    描述剩余空间的 fr 迭代分配（rounds 为逐轮记录）。
    """

    phase: Literal["min_content", "fr"]
    item_index: Optional[int] = None
    name: Optional[str] = None
    start: Optional[int] = None
    span: Optional[int] = None
    min_content: Optional[float] = None
    rounds: list[Union[GridMinContentRoundOut, GridFrRoundOut]] = Field(default_factory=list)
    unmet: Optional[float] = None
    initial_free_space: Optional[float] = None


class GridTracksResponse(BaseModel):
    container_width: float
    gap: float
    total_gap: float
    processing_order: list[int]  # 项目处理顺序（输入下标；跨度升序、同跨度原序）
    tracks: list[GridTrackOut]
    used_width: float  # 各列最终尺寸 + 间距合计
    container_overflow: float  # used_width 超出容器宽度的部分（否则 0）
    overflow: list[GridOverflowOut] = Field(default_factory=list)
    trace: list[GridTraceStepOut] = Field(default_factory=list)


# ---------- POST /color-mix ----------


ColorMixSpace = Literal["srgb", "srgb-linear", "hsl", "oklab", "oklch"]
HueInterpolation = Literal["shorter", "longer", "increasing", "decreasing"]


class ColorMixRequest(BaseModel):
    """color-mix() 风格的两色混合请求。

    color_a / color_b 接受 hex（#rgb/#rgba/#rrggbb/#rrggbbaa）、
    rgb()/rgba()、hsl()/hsla()；weight_a / weight_b 为百分比数值（如 30），
    均省略时各取 50，只省略一个时补到 100。interpolation_space 缺省 oklab；
    hue_interpolation 仅在极坐标空间（hsl / oklch）下有效，缺省 shorter。
    """

    color_a: str
    color_b: str
    weight_a: Optional[float] = None
    weight_b: Optional[float] = None
    interpolation_space: ColorMixSpace = "oklab"
    hue_interpolation: Optional[HueInterpolation] = None


class ColorMixInputOut(BaseModel):
    """一侧输入色的解析结果。"""

    raw: str
    syntax: str  # hex / rgb / hsl
    given_weight: Optional[float]  # 请求中给定的权重（null 为省略）
    normalized_weight: float  # 归一化后的最终权重
    srgb: list[float]  # 该色在 sRGB 中的分量（0..1，未裁剪）
    alpha: float


class ColorMixNodeOut(BaseModel):
    """转换轨迹中的一个空间节点。

    space 为空间名；coords 为该空间坐标（缺失分量为 null）；alpha 为该
    节点处 alpha；premultiplied 标记 coords 是否已乘 alpha。
    """

    space: str
    coords: list[Optional[float]]
    missing: list[bool]
    alpha: float
    premultiplied: bool = False


class ColorMixInterpolationOut(BaseModel):
    """插值空间中的完整处理记录。

    start / end：两侧在目标空间的起点 / 终点节点（先转换，再预乘 alpha）；
    hue_adjustment：极坐标空间的 hue 调整记录（矩形空间为 null）；
    mixed：插值后、未还原预乘的目标空间节点；
    result：还原预乘并应用权重和缩放后的目标空间节点。
    """

    space: str
    hue_interpolation: Optional[str]
    start: ColorMixNodeOut
    end: ColorMixNodeOut
    hue_adjustment: Optional[dict] = None
    mixed: ColorMixNodeOut
    result: ColorMixNodeOut


class ColorMixResponse(BaseModel):
    color_a: ColorMixInputOut
    color_b: ColorMixInputOut
    weights: dict  # 权重补齐、归一化与权重和缩放的说明与数值
    interpolation_space: str
    hue_interpolation: Optional[str]
    conversions: list[ColorMixNodeOut]  # 两侧「输入空间 -> sRGB -> 目标空间」的完整转换轨迹
    interpolation: ColorMixInterpolationOut
    back_conversion: list[ColorMixNodeOut]  # 目标空间 -> sRGB 的逐跳坐标
    clipped: list[bool]  # 各 sRGB 通道在转回后是否被裁剪
    rgba: dict  # 最终 rgba：整数 r/g/b（0..255）、alpha（0..1）与 css 字符串
    trace: list[str]  # 中文逐步轨迹


# ---------- POST /gamut-map ----------


class GamutMapRequest(BaseModel):
    """OKLCH -> sRGB 色域映射请求。

    color 为 CSS oklch()：L 取 0..1 数值或百分比（100% = 1），
    C 取数值或百分比（100% = 0.4），H 为角度（deg/rad/grad/turn，
    裸数字按 deg）或 none，可带斜杠 alpha（0..1 或百分比）。
    tolerance 为二分查找的终止区间宽度，缺省 1e-5，
    必须在 [1e-7, 1e-3] 内。
    """

    color: str
    tolerance: float = 1e-5


class OklchOut(BaseModel):
    """一个 OKLCH 颜色节点；C 为 0 或 hue 缺失（none）时 h 为 null。"""

    l: float
    c: float
    h: Optional[float]
    alpha: float


class GamutMapRoundOut(BaseModel):
    """二分查找的一轮。

    lo / hi 为进入本轮时的下界 / 上界，mid 为试取的中点，
    in_gamut 标记该中点是否仍在 sRGB 色域内（在则 lo 升为 mid，
    否则 hi 降为 mid）。
    """

    lo: float
    hi: float
    mid: float
    in_gamut: bool


class GamutMapResponse(BaseModel):
    input: str  # 原始输入串（去首尾空白后）
    tolerance: float
    original: OklchOut  # 原始 OKLCH
    mapped: OklchOut  # 映射后的 OKLCH（L、h、alpha 与原色一致）
    was_mapped: bool  # 是否发生了映射（原色已在 sRGB 色域内时为 false）
    chroma_loss: float  # 色度损失：原 C - 映射后 C
    srgb_unclamped: list[float]  # 原色直接转出的 sRGB（未裁剪，可越界）
    srgb: list[float]  # 最终 sRGB（映射后，色域内）
    rgba: dict  # 最终 rgba：整数 r/g/b（0..255）、alpha（0..1）与 css 字符串
    rounds: list[GamutMapRoundOut]  # 二分查找的每轮上下界（未映射时为空）
    trace: list[str]  # 中文逐步轨迹


# ---------- POST /stylesheet-audit ----------


class AuditElement(BaseModel):
    """样式表清理审计的 DOM 样本树节点。

    id 全树唯一（非空字符串或整数）；tag 必填且非空；classes / attributes
    缺省为空；children 缺省为空数组（不允许 null）。
    """

    id: object
    tag: str
    classes: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    children: list["AuditElement"] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _id_type(cls, v):
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ValueError("节点 id 必须为非空字符串或整数")
        if isinstance(v, str) and not v.strip():
            raise ValueError("节点 id 不能为空字符串")
        return v

    @field_validator("tag")
    @classmethod
    def _tag_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("节点 tag 不能为空")
        return v

    @field_validator("classes", mode="before")
    @classmethod
    def _classes_must_be_list(cls, v):
        if v is None:
            raise ValueError("classes 不能为 null，无类名时给空数组 []")
        return v

    @field_validator("children", mode="before")
    @classmethod
    def _children_must_be_list(cls, v):
        if v is None:
            raise ValueError("children 不能为 null，叶子节点给空数组 []")
        return v


class StylesheetAuditRequest(BaseModel):
    """样式表清理审计请求。

    root 为 DOM 样本树根（节点含唯一 id、tag、classes、attributes、children）；
    layers 为层声明顺序（早 -> 晚）；rules 为现有结构化规则（同 /compute）。
    """

    root: AuditElement
    layers: list[str] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)


class DeletedDeclarationOut(BaseModel):
    """实际纳入删除批次的一条声明（按 rule_index、declaration_index 排序）。"""

    rule_index: int
    declaration_index: int
    property: str
    selector: str


class RetainedDeclarationOut(BaseModel):
    """必须保留的一条声明及其首个阻断证据。

    reason 代码：
      - winner_changed：删除后该属性的层叠胜出声明换人（含该属性整体消失）；
      - new_invalid：删除后原本 computed 的属性 / 自定义属性变为 invalid；
      - invalid_cleared：删除后原本 invalid 的状态被解除；
      - value_changed：胜出声明相同，但 var() / 继承链取值发生变化；
      - status_changed：自定义属性 ok ↔ invalid 切换（胜出声明相同）；
      - reason_changed：胜出声明、状态与取值不变，但失效原因变化
        （如循环依赖失效变为 var() 解析失败）。
    conflict_node 为按先序遍历找到的首个冲突节点 id；
    custom_properties / computed / invalid 三类节点状态任一变化即视为阻断。
    判定签名与 /dom-impact、/compute 预演共享：胜出声明定位 + 状态 +
    取值 + 失效原因。
    """

    rule_index: int
    declaration_index: int
    property: str
    selector: str
    conflict_node: object
    conflict_property: str
    reason: str
    detail: str


class StylesheetAuditResponse(BaseModel):
    nodes_checked: int
    deleted: list[DeletedDeclarationOut]
    retained: list[RetainedDeclarationOut]


# ---------- POST /dom-impact ----------


class DomMutation(BaseModel):
    """对指定节点的一次原子 DOM 变更（六类互斥操作之一）。

    - add_class / remove_class：给出 class_name（类名不能为空）；
    - set_attribute：给出 name（不能为 id / class）与 value（字符串，可为空串）；
    - remove_attribute：给出 name（不能为 id / class）；
    - set_tag：给出 tag（非空白）。

    空操作（类已存在 / 不存在、属性已是同值 / 不存在、tag 与现 tag 相同）
    返回 422。
    """

    type: Literal[
        "add_class",
        "remove_class",
        "set_attribute",
        "remove_attribute",
        "set_tag",
    ]
    node_id: object
    class_name: Optional[str] = None
    name: Optional[str] = None
    value: Optional[str] = None
    tag: Optional[str] = None

    @field_validator("node_id", mode="before")
    @classmethod
    def _id_type(cls, v):
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ValueError("变更目标 node_id 必须为非空字符串或整数")
        if isinstance(v, str) and not v.strip():
            raise ValueError("变更目标 node_id 不能为空字符串")
        return v

    @field_validator("class_name", "name", "tag", "value")
    @classmethod
    def _not_null(cls, v):
        # 缺省表示该操作不使用此字段；显式 null 不允许
        if v is None:
            raise ValueError("字段不能为 null；该操作不需要时请省略")
        return v


class DomImpactRequest(BaseModel):
    """DOM 变更样式影响分析请求。

    root / layers / rules 结构同 /stylesheet-audit；mutation 为一次对树中
    某节点的原子变更。先在原树上求基线，再应用变更、整树重算，
    返回所有 computed / custom_properties / invalid 发生变化的节点。
    """

    root: AuditElement
    layers: list[str] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    mutation: DomMutation


class MutationOut(BaseModel):
    """实际应用的变更回显。"""

    type: str
    node_id: object
    class_name: Optional[str] = None
    name: Optional[str] = None
    value: Optional[str] = None
    tag: Optional[str] = None


class PropertyValueOut(BaseModel):
    """一个属性在某一侧（基线 / 变更后）的状态与取值。

    普通属性 status：computed | invalid | absent（该侧无胜出声明）；
    自定义属性 status：ok | invalid | missing（该侧祖先链上不存在）。
    invalid 时 reason 为失效原因，value 为 null。
    """

    status: str
    value: Optional[str] = None
    reason: Optional[str] = None
    winner: Optional[CandidateOut] = None  # 自定义属性纯继承 / missing 时为 null


class PropertyImpactOut(BaseModel):
    """单个节点上一个发生变化的属性（普通属性或自定义属性）。

    reasons 为该变化的传播原因（按主次排序，可并存）：
      - selector_match：变更使规则选择器对该元素的命中情况发生改变
        （或使祖先链改变，经由选择器的后代 / 子组合符传导）；
      - cascade_winner：该属性的层叠胜出声明换人（含属性消失 / 新增）；
      - inheritance：自定义属性本元素无胜出声明，变化经继承自祖先传导；
      - var_chain：胜出声明相同，值 / 失效状态经 var() 依赖链变化。
    """

    property: str
    is_custom: bool
    reasons: list[str]
    before: PropertyValueOut
    after: PropertyValueOut
    detail: str


class NodeImpactOut(BaseModel):
    """一个受影响节点：其 computed / custom_properties / invalid 中
    发生变化的属性逐项对比；未受影响节点不列出。"""

    node_id: object
    changes: list[PropertyImpactOut]


class DomImpactResponse(BaseModel):
    nodes_checked: int
    target_id: object
    mutation: MutationOut
    affected_nodes: list[NodeImpactOut]
