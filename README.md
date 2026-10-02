# CSS Cascade Explainer

CSS 层叠解释 API（Python + FastAPI）。`POST /compute` 接收元素祖先链、层声明顺序与结构化样式规则，
按 CSS 级联规则计算每个属性的胜出声明，解析自定义属性与 `var()`，并返回**逐步淘汰证据**。

## 快速开始（一条命令）

```bash
docker compose up --build
```

服务启动后监听 **8000** 端口：

- `POST /compute` — 层叠计算；携带 `patches` 时返回样式变更预演（基线 / 补丁后结果、逐项差异、因果轨迹与未生效补丁）
- `POST /stylesheet-audit` — 样式表清理审计：在 DOM 样本树上逐节点复用选择器与层叠 / `var()` 求值，找出可同时删除而不改变任何节点 computed、custom_properties 或 invalid 状态的声明批次
- `POST /dom-impact` — DOM 变更样式影响分析：对指定节点做一次原子变更（增删 class、设置 / 删除 attribute、修改 tag），整树重算后返回所有 computed、custom_properties 或 invalid 变化的节点及前后值，并归因选择器命中 / 层叠胜出者 / 继承 / `var()` 链
- `POST /selector-overlap` — 不提供 DOM 样本，判断两条选择器能否命中同一元素
- `POST /keyframe-sample` — CSS 数值属性关键帧采样
- `POST /counter-render` — CSS 计数器元素树渲染（counter-reset/set/increment 与 counter()/counters()）
- `POST /math-evaluate` — CSS 数学表达式求值（calc/min/max/clamp 与四则运算）
- `POST /grid-tracks` — 单行 CSS Grid 列宽求解（px/%/fr/minmax 轨道与项目 min_content）
- `POST /color-mix` — 按 CSS Color 规则混合两种颜色（srgb/srgb-linear/hsl/oklab/oklch 与 hue 弧线）
- `POST /gamut-map` — 把 CSS oklch() 颜色安全映射进 sRGB 色域（固定 L/h/alpha，二分最大色域内 C）
- `GET /health` — 健康检查
- `GET /docs` — Swagger UI 交互文档

不使用 Docker 时本地运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 整树分析内核

`/compute`、`/stylesheet-audit`、`/dom-impact` 与 `/compute` 预演（patches）
共享同一个**不可变整树分析内核**（`app/analysis.py`），不再各自维护节点
状态签名与差异判断：

- **`RuleSet`**：一次解析的规则集（选择器、特异性、源码次序、层声明顺序）。
  审计的逐条试删、DOM 变更的基线 / 变更后、预演的基线 / 补丁后 / 留一法
  复算，都复用同一份解析结果（补丁只改声明，经 `with_declarations` 替换
  声明表，选择器骨架不重新解析）；
- **`SolveOptions`**：一次求解的变体参数（审计的声明删除集合、预演的
  声明级层覆盖），不改变已解析的规则骨架；
- **`solve_chain` / `solve_forest`**：按先序为每个节点产出不可变的
  **`NodeAnalysis`**——普通属性与自定义属性的层叠胜出者与全部候选定位、
  `computed` / `custom_properties` / `invalid`（含失效原因）、自定义属性
  沿祖先链的继承与 `var()` 依赖记录（解析事件、依赖图、循环集合）；
- **`NodeAnalysis.signature()`**：三处前后对比（审计试删、DOM 变更、
  补丁预演）共享的节点状态签名——每个属性记录「胜出声明定位 +
  `computed`/`invalid`（或 `ok`/`invalid`）状态 + 取值 + 失效原因」。

因此**胜出者改变但取值相同**、失效原因变化（如循环依赖失效变为 var()
解析失败）都计为变化；完全未变的节点不会进入任何一方的结果。

## 请求示例

```bash
curl -X POST http://localhost:8000/compute \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

`examples/request.json` 内容（也是下面响应示例的输入）：

```json
{
  "element": {"tag": "p", "id": "intro", "classes": ["text"], "attributes": {"data-role": "lead"}},
  "ancestors": [
    {"tag": "html"},
    {"tag": "body", "classes": ["dark"]},
    {"tag": "div", "id": "app"}
  ],
  "layers": ["reset", "base", "components"],
  "rules": [
    {"origin": "ua", "selector": "p", "declarations": [
      {"property": "color", "value": "black"},
      {"property": "margin", "value": "1em 0"}
    ]},
    {"origin": "author", "layer": "reset", "selector": "p", "declarations": [
      {"property": "margin", "value": "0"}
    ]},
    {"origin": "author", "layer": "base", "selector": "div p", "declarations": [
      {"property": "color", "value": "blue"},
      {"property": "--gap", "value": "8px"}
    ]},
    {"origin": "author", "selector": "#app > p.text", "declarations": [
      {"property": "color", "value": "red"},
      {"property": "padding", "value": "var(--gap, 4px)"}
    ]},
    {"origin": "author", "layer": "components", "selector": "p[data-role=lead]", "declarations": [
      {"property": "color", "value": "green", "important": true}
    ]}
  ]
}
```

响应节选：

```json
{
  "computed": {"color": "green", "margin": "0", "padding": "8px"},
  "custom_properties": {"--gap": "8px"},
  "invalid": [],
  "evidence": [
    {
      "property": "color",
      "status": "computed",
      "computed_value": "green",
      "winner": {"selector": "p[data-role=lead]", "important": true, "specificity": [0, 1, 1], "...": "..."},
      "eliminated": [
        {"candidate": {"selector": "#app > p.text", "value": "red", "...": "..."},
         "stage": "importance", "detail": "important 声明优先于普通声明"},
        {"candidate": {"selector": "div p", "value": "blue", "...": "..."},
         "stage": "importance", "detail": "important 声明优先于普通声明"},
        {"candidate": {"selector": "p", "origin": "ua", "value": "black", "...": "..."},
         "stage": "importance", "detail": "important 声明优先于普通声明"}
      ]
    },
    {
      "property": "margin",
      "computed_value": "0",
      "eliminated": [
        {"candidate": {"selector": "p", "origin": "ua", "value": "1em 0", "...": "..."},
         "stage": "origin", "detail": "来源优先级 author > user > ua：author 胜过 ua"}
      ]
    }
  ]
}
```

## 循环依赖与 invalid 示例

```bash
curl -X POST http://localhost:8000/compute \
  -H 'Content-Type: application/json' \
  -d '{
    "element": {"tag": "p"},
    "rules": [
      {"selector": "p", "declarations": [
        {"property": "--a", "value": "var(--b)"},
        {"property": "--b", "value": "var(--a)"},
        {"property": "color", "value": "var(--a)"},
        {"property": "margin", "value": "var(--a, 1px)"},
        {"property": "padding", "value": "var(--missing)"}
      ]}
    ]
  }'
```

响应要点：`--a` / `--b` 因循环依赖失效；`color`（引用失效变量、无 fallback）与
`padding`（引用未定义变量、无 fallback）被标记为 `invalid`，`computed` 中值为 `null`；
`margin` 有 fallback，计算为 `1px`。

```json
{
  "computed": {"color": null, "margin": "1px", "padding": null},
  "invalid": [
    {"property": "color", "value": "var(--a)",
     "reason": "var() 引用的自定义属性 --a 已失效（循环依赖或自身解析失败），且声明没有 fallback"},
    {"property": "padding", "value": "var(--missing)",
     "reason": "var() 引用的自定义属性 --missing 未定义，且声明没有 fallback"}
  ]
}
```

## POST /compute 样式变更预演（patches）

在原请求上额外携带 `patches` 数组，可对**既有声明**做定位式修改并预演结果。
每条补丁以 `rule_index`（规则在 `rules` 中的下标）+ `declaration_index`
（声明在该规则 `declarations` 中的下标）定位，可修改 `value`、`important`
或 `layer`（三者至少给一个；`layer` 显式给 `null` 表示改为**未分层**，
缺省表示不改层）。补丁**一次性整体生效**：任一定位非法（下标越界、
定位重复、未携带任何修改等）整次请求返回 **422，不产生部分结果**。

不带 `patches`（或为空数组）的旧请求与响应完全保持兼容：顶层仍是原有的
`computed` / `custom_properties` / `invalid` / `evidence` /
`custom_property_trace`（即基线结果）；携带补丁时额外附带 `preview` 字段。

8000 端口示例（请求体见 `examples/preview.json`）：

```bash
curl -X POST http://localhost:8000/compute \
  -H 'Content-Type: application/json' \
  -d @examples/preview.json
```

上例在 `examples/request.json` 的基础上打了 4 条补丁：

| 补丁下标 | 定位 | 修改 |
| --- | --- | --- |
| 0 | 规则 4 第 0 条（components 层的 important `color: green`） | `important: false` |
| 1 | 规则 2 第 1 条（`--gap: 8px`） | `value: "12px"` |
| 2 | 规则 2 第 2 条（`--ring: var(--gap)`） | 追加 fallback：`value: "var(--gap, 2px)"` |
| 3 | 规则 0 第 1 条（UA 的 `margin: 1em 0`） | `value: "2em 0"` |

```json
{
  "preview": {
    "baseline": {
      "computed": {"color": "green", "margin": "0", "outline": "8px", "padding": "8px"},
      "custom_properties": {"--gap": "8px", "--ring": "8px"},
      "invalid": []
    },
    "patched": {
      "computed": {"color": "red", "margin": "0", "outline": "12px", "padding": "12px"},
      "custom_properties": {"--gap": "12px", "--ring": "12px"},
      "invalid": []
    },
    "changes": [
      {"property": "--gap", "is_custom": true, "kind": "edited",
       "before_status": "ok", "after_status": "ok",
       "before_value": "8px", "after_value": "12px", "responsible_patches": [{"index": 0, "...": "..."}]},
      {"property": "--ring", "is_custom": true, "kind": "var_changed",
       "before_value": "8px", "after_value": "12px", "trail": ["...因果轨迹..."]},
      {"property": "color", "kind": "winner_changed",
       "before_value": "green", "after_value": "red",
       "before_winner": {"rule_index": 4, "declaration_index": 0, "...": "..."},
       "after_winner": {"rule_index": 3, "declaration_index": 0, "...": "..."}},
      {"property": "outline", "kind": "var_changed", "before_value": "8px", "after_value": "12px"},
      {"property": "padding", "kind": "var_changed", "before_value": "8px", "after_value": "12px"}
    ],
    "shadowed_patches": [
      {"patch": {"index": 2, "rule_index": 2, "declaration_index": 2},
       "reason": "补丁作用的声明在补丁后仍是胜出者，但对它的修改既未改变其胜出地位，也未传导为最终取值变化（--gap 始终有效，追加的 fallback 从未改道）"},
      {"patch": {"index": 3, "rule_index": 0, "declaration_index": 1},
       "reason": "声明在层叠竞争中落选（reset 层的 margin: 0 始终胜出），补丁无效"}
    ],
    "applied_patches": [{"index": 0, "...": "..."}, {"index": 1, "...": "..."}]
  }
}
```

### 差异分类

`changes[]` **只列出发生变化的普通属性与自定义属性**（未变化的不列出），
普通属性与自定义属性以 `is_custom` 区分，每项给出基线 / 补丁后的状态与值、
两侧层叠胜出声明（`before_winner` / `after_winner`）以及 `responsible_patches`
（对该差异实际负责的补丁下标，留一法判定）。差异 `kind`：

- **`winner_changed`**：层叠胜出声明发生了换人（被改声明自身夺魁，或它被削弱后
  第三方声明夺魁；`status_changed` 标记是否同时发生 computed ↔ invalid 切换）；
- **`var_changed`**：胜出声明是**同一条**，但因其 `var()` 依赖的自定义属性
  变化而改值 / 失效——包括依赖值变化、变量链形成或解除循环、fallback 改道
  （原本走主分支改为走 fallback，或原本走 fallback 重新走主分支）；
- **`edited`**：胜出声明是同一条，补丁直接把它自己的 `value` 改为不含
  `var()` 的字面值（与变量解析无关的直接改值）。

普通属性的 `before_status` / `after_status` 为 `computed` / `invalid`，
失效原因分别在 `before_reason` / `after_reason`；自定义属性为
`ok` / `invalid` / `missing`（`missing` 表示该侧元素链上完全没有此变量）。
纯继承得到、本元素无胜出声明的自定义属性，其 `before_winner` / `after_winner`
为 `null`。

### 因果轨迹（trail）

每项差异的 `trail[]` 是一条**从被改声明连到最终属性**的中文因果链，沿
「层叠胜出 → 逐元素自定义属性解析（var 替换 / 循环失效 / fallback 改道）
→ 继承 → 普通属性取值」传播；发生变化的一跳以 `【基线】…` / `【补丁后】…`
对照给出，未变化的跳直接叙述。例如把 `--a: 1px` 改为 `--a: var(--b)`、
同时 `--b: var(--a)` 形成循环后，`padding: var(--a)` 的轨迹为：

```text
补丁作用于 规则 0 第 0 条声明（--a）：value '1px' → 'var(--b)'
p 上的自定义属性 --a：该声明在 p 上为 --a 的层叠胜出者
p 上的自定义属性 --b：【补丁后】p 上 --a ↔ --b 形成循环依赖，相关自定义属性同时失效
最终普通属性 padding：【基线】var(--a) 直接替换为 '1px'
最终普通属性 padding：【补丁后】var(--a) 引用的变量因循环依赖而失效且无可用 fallback，声明失效
```

### 未生效补丁（shadowed_patches）

经留一法判定**未影响结果**的补丁单独列入 `shadowed_patches[]`，并给出原因：
规则选择器未命中目标元素链、声明在层叠竞争中落选、声明本就胜出但修改不改变
其胜出地位也不传导到取值（如给始终有效的变量追加 fallback）、或其作用被其他
补丁完全覆盖。`applied_patches[]` 则回显本次接收的全部补丁（按补丁数组下标）。

### 预演校验（HTTP 422）

- `rule_index` 越界（含负数、非整数、布尔）或 `declaration_index` 超出该规则
  的声明数；
- 两条补丁定位到同一条声明；
- 补丁未携带任何修改（`value` / `important` / `layer` 均缺省）。

## POST /stylesheet-audit：样式表清理审计

给定一棵 **DOM 样本树**（每个节点含全树唯一的 `id`、`tag`、`classes`、
`attributes`、`children`）、层声明顺序 `layers` 与现有结构化规则 `rules`，
审计哪些声明可以**同时删除**而不改变样本树上任何节点的 `computed`、
`custom_properties` 或 `invalid` 状态。

判定过程与 `/compute` 完全同一套引擎（见上文「整树分析内核」）：逐节点以
「根 → … → 父」祖先链做选择器匹配，按来源 / 层 / important / 特异性 /
源码次序层叠，自定义属性沿树**继承**、检测**循环依赖**并按 **fallback**
解析 `var()`。审计按 `rule_index`、`declaration_index` 顺序**逐条把声明
加入删除候选并重新计算整棵树**：删除后所有节点的共享签名（与
`/dom-impact`、`/compute` 预演同一份）均与基线一致，才真正纳入批次
（贪心累积，后续试删在已删批次之上进行）：

- 普通属性：胜出声明定位 `(rule_index, declaration_index)`、
  `computed` / `invalid` 状态、取值与失效原因；
- 自定义属性：本元素胜出声明定位（纯继承为 `null`）、
  `ok` / `invalid` / `missing` 状态、解析值与失效原因
  （**含只继承、本元素未声明的**）。

因此删除后出现新的失效（变量缺失、循环形成 / 解除、fallback 改道失败）、
原本 invalid 被解除、层叠胜出者换人（**即使继任者取值恰好相同**）、或经
继承 / `var()` 链传导的任何取值变化，都会阻断该声明进入批次。

```bash
curl -X POST http://localhost:8000/stylesheet-audit \
  -H 'Content-Type: application/json' \
  -d @examples/stylesheet-audit.json
```

`examples/stylesheet-audit.json` 的样本树为 `div#app.dark > (h1#title,
p.text#lead > code#code)`。响应（节选）：

```json
{
  "nodes_checked": 4,
  "deleted": [
    {"rule_index": 1, "declaration_index": 0, "property": "--gap", "selector": "div"},
    {"rule_index": 3, "declaration_index": 0, "property": "color", "selector": "p.text"},
    {"rule_index": 3, "declaration_index": 1, "property": "color", "selector": "p.text"},
    {"rule_index": 7, "declaration_index": 0, "property": "color", "selector": "aside"}
  ],
  "retained": [
    {"rule_index": 0, "declaration_index": 0, "property": "margin",
     "conflict_node": "app", "conflict_property": "margin",
     "reason": "winner_changed",
     "detail": "margin 的胜出声明（规则 0 第 0 条，原值 '0'）被删除后该属性不再有任何声明胜出"},
    {"rule_index": 1, "declaration_index": 1, "property": "--ring",
     "conflict_node": "app", "conflict_property": "--ring",
     "reason": "winner_changed",
     "detail": "自定义属性 --ring（原来源：规则 1 第 1 条，值 '12px'）在删除后于该节点链上彻底消失（missing）"}
  ]
}
```

上例体现的要点：

- 规则 1 的 `--gap: 8px` 可删：它在所有命中节点都被更高特异性的
  `div.dark { --gap: 12px }` 遮蔽，删除后 `--ring: var(--gap)` 仍解析为
  `12px`、`code` 的 `color: var(--gap)` 也不变——自定义属性继承与 `var()`
  依赖被完整复算；
- 规则 3 的两条 `color` 落选声明可删（分别败给同规则后者与属性选择器规则），
  但胜出的 `color: navy` / `teal` 必须保留；
- 即使没有任何属性引用 `--unused: 2px`，它仍必须保留：它出现在 `div` 的
  `custom_properties` 中，删除会改变该节点的自定义属性集合；
- 规则 7 的 `aside { color: pink }` 可删：选择器不命中样本树任何节点。

### 响应字段

- `nodes_checked`：参与比对的样本节点数（先序遍历）；
- `deleted[]`：**实际可删批次**，按 `rule_index`、`declaration_index` 排序，
  每项回显 `property` 与 `selector`。该批次可一次性整体删除，删除后整棵树的
  computed / custom_properties / invalid 与基线逐节点一致；
- `retained[]`：每个保留声明同样按定位排序，并给出首个阻断证据：
  - `conflict_node`：按**先序遍历**找到的首个发生变化的节点 `id`
    （同一声明在多个节点胜出时取最早者，通常即胜出节点自身）；
  - `conflict_property`：首个冲突的普通属性或自定义属性名；
  - `reason`：阻断类别——`winner_changed`（胜出者换人或属性消失 / 新增）、
    `new_invalid`（删除后新出现失效）、`invalid_cleared`（原失效被解除）、
    `value_changed`（胜出者不变但 `var()` / 继承取值变化）、
    `status_changed`（自定义属性 ok ↔ invalid 切换）、
    `reason_changed`（胜出声明、状态与取值不变，但失效原因变化，
    如循环依赖失效变为 `var()` 解析失败）；
  - `detail`：中文阻断说明（含基线 / 删除后的胜出定位与取值）。

### 审计校验（HTTP 422）

- 节点 `id` 缺失、为空字符串、为布尔，或树中存在**重复 id**；
- 节点 `tag` 缺失 / 为空白；`children` 或 `classes` 为 `null`
  （叶子节点给空数组 `[]`）；
- 规则选择器语法非法（与 `/compute` 同一套选择器语法）；
- 声明的 `property` 为空白。

既有接口（如不带 patches 的 `/compute`）及其余接口、
`docker compose up --build` 启动方式均保持不变。

## POST /dom-impact：DOM 变更后的样式影响分析

给定一棵带唯一 `id` 的 **DOM 样本树**（节点含 `id`、`tag`、`classes`、
`attributes`、`children`）、层声明顺序 `layers`、现有结构化规则 `rules`，
以及一次对指定节点的**原子变更** `mutation`。接口先在原树上按层叠、继承与
`var()` 规则求**基线**，再原子应用变更并**重新求解整棵树**，只返回
`computed`、`custom_properties` 或 `invalid` 发生变化的节点，逐项给出
变更前后的状态、取值、失效原因与胜出声明，并区分变化的传播原因。未受影响
节点不列出。

变更类型（六类互斥操作，一次只做一件事）：

| `type` | 必填字段 | 含义 |
| --- | --- | --- |
| `add_class` | `node_id`、`class_name` | 为节点添加一个 class |
| `remove_class` | `node_id`、`class_name` | 从节点移除一个 class |
| `set_attribute` | `node_id`、`name`、`value` | 设置（新增或改值）一个 attribute |
| `remove_attribute` | `node_id`、`name` | 删除一个 attribute |
| `set_tag` | `node_id`、`tag` | 修改节点标签名 |

`id` 与 `class` 是节点标识字段，不能借 `set_attribute` / `remove_attribute`
修改（改 class 请用 `add_class` / `remove_class`）。

```bash
curl -X POST http://localhost:8000/dom-impact \
  -H 'Content-Type: application/json' \
  -d @examples/dom-impact.json
```

`examples/dom-impact.json` 的样本树为
`div#app.dark > (h1#title, p.text#lead[data-role=lead] > code#code)`，
变更为从根节点移除 class `dark`。响应（节选）：

```json
{
  "nodes_checked": 4,
  "target_id": "app",
  "mutation": {"type": "remove_class", "node_id": "app", "class_name": "dark"},
  "affected_nodes": [
    {
      "node_id": "app",
      "changes": [
        {
          "property": "--gap", "is_custom": true,
          "reasons": ["selector_match", "cascade_winner"],
          "before": {"status": "ok", "value": "12px", "winner": {"selector": "div.dark", "...": "..."}},
          "after":  {"status": "ok", "value": "8px",  "winner": {"selector": "div", "...": "..."}},
          "detail": "自定义属性 --gap：ok = '12px' → ok = '8px'；选择器命中发生变化：规则 2 第 0 条声明（选择器 'div.dark'）不再命中该元素；层叠胜出声明由规则 2 第 0 条（'12px'）改由规则 1 第 0 条（'8px'）胜出"
        }
      ]
    },
    {
      "node_id": "title",
      "changes": [
        {"property": "--gap", "is_custom": true, "reasons": ["inheritance"],
         "before": {"status": "ok", "value": "12px", "winner": null},
         "after":  {"status": "ok", "value": "8px", "winner": null},
         "detail": "...本元素未声明该自定义属性，变化经继承自祖先传导"}
      ]
    },
    {
      "node_id": "lead",
      "changes": [
        {"property": "padding", "is_custom": false, "reasons": ["var_chain"],
         "before": {"status": "computed", "value": "12px"},
         "after":  {"status": "computed", "value": "8px"},
         "detail": "普通属性 padding：computed = '12px' → computed = '8px'；层叠胜出声明相同，但 var() 依赖的自定义属性取值 / 失效状态变化..."},
        {"property": "--gap", "is_custom": true, "reasons": ["inheritance"], "...": "..."}
      ]
    },
    {
      "node_id": "code",
      "changes": [
        {"property": "color", "is_custom": false, "reasons": ["var_chain"],
         "before": {"status": "computed", "value": "12px"},
         "after":  {"status": "computed", "value": "8px"}, "...": "..."},
        {"property": "--gap", "is_custom": true, "reasons": ["inheritance"], "...": "..."}
      ]
    }
  ]
}
```

上例完整展示了四类传播原因：

- **选择器命中 + 层叠胜出者**（`selector_match`、`cascade_winner`）：根节点
  移除 `dark` 后 `div.dark { --gap: 12px }` 不再命中，`--gap` 胜出者交还给
  `div { --gap: 8px }`；`p[data-role=lead]`、`h1` 等不涉及的规则不受影响；
- **继承**（`inheritance`）：自定义属性对**所有后代**继承，因此即使
  `h1#title` 没有任何引用 `--gap` 的普通属性，它的 `custom_properties`
  仍从 `12px` 变为 `8px`，所以 `title` 节点也会列出（纯继承属性两侧
  `winner` 均为 `null`）；
- **var() 链**（`var_chain`）：`lead` 的 `padding: var(--gap, 4px)` 与
  `code` 的 `color: var(--gap)` 胜出声明始终是同一条，但引用的自定义
  属性沿继承链改值，computed 随之变化。

### 变化项的状态与原因

每项变化的 `before` / `after` 包含：

- `status`：普通属性为 `computed` / `invalid` / `absent`（该侧无胜出声明）；
  自定义属性为 `ok` / `invalid` / `missing`（该侧祖先链上不存在）；
- `value`：解析值（`invalid` / `absent` / `missing` 时为 `null`）；
- `reason`：失效时的中文原因（变量缺失、循环依赖、fallback 失败等）；
- `winner`：该侧的胜出声明（`rule_index` / `declaration_index` 等，
  与 `/compute` 同款）；自定义属性纯继承 / 缺失时为 `null`。

`reasons` 为传播原因（可并存，按主次排序）：

- `selector_match`：变更使规则选择器对该元素的命中情况改变（含变更发生在
  **祖先**上、经后代 / 子组合符传导到后代元素的情况）；
- `cascade_winner`：该属性的层叠胜出声明换人——包括属性消失 / 新增。
  **继任者取值恰好相同也会列出**（胜出声明定位是被比对状态的一部分）；
- `inheritance`：自定义属性本元素无胜出声明，变化经继承自祖先传导
  （含从有值继承变为 `missing`）；
- `var_chain`：胜出声明相同，但取值 / 失效状态经 `var()` 依赖链变化
  （包括 computed ↔ invalid 翻转、fallback 改道、循环依赖形成 / 解除）。

### 嵌套继承示例

自定义属性沿祖先链继承并可经 `var()` 逐级串联。下例 `--a` 在根上声明，
`section` 的 `--b` 引用它，`p` 的 `margin` 再引用 `--b`；给根添加
`hot` 类后，变化沿「层叠 → 继承 → var() 链 → 再继承」传导三层：

```bash
curl -X POST http://localhost:8000/dom-impact \
  -H 'Content-Type: application/json' \
  -d '{
    "root": {"id": "gp", "tag": "div", "children": [
      {"id": "mid", "tag": "section", "children": [
        {"id": "leaf", "tag": "p", "children": []}
      ]}
    ]},
    "rules": [
      {"selector": "div", "declarations": [{"property": "--a", "value": "2px"}]},
      {"selector": "div.hot", "declarations": [{"property": "--a", "value": "9px"}]},
      {"selector": "section", "declarations": [{"property": "--b", "value": "var(--a)"}]},
      {"selector": "p", "declarations": [{"property": "margin", "value": "var(--b)"}]}
    ],
    "mutation": {"type": "add_class", "node_id": "gp", "class_name": "hot"}
  }'
```

响应要点：`gp` 的 `--a` 因 `selector_match` + `cascade_winner` 由 `2px`
变为 `9px`；`mid` 的 `--a` 标 `inheritance`、`--b` 标 `var_chain`（本元素
有胜出声明，变化来自其 `var(--a)` 依赖）；`leaf` 的 `--b` 标
`inheritance`、`margin` 标 `var_chain` 变为 `9px`。树中不涉及的节点
（如无相关规则的兄弟子树）不列入 `affected_nodes`。

### fallback 改道与循环解除示例

```bash
curl -X POST http://localhost:8000/dom-impact \
  -H 'Content-Type: application/json' \
  -d '{
    "root": {"id": "r", "tag": "div", "classes": ["on"], "children": [
      {"id": "k", "tag": "p", "children": []}
    ]},
    "rules": [
      {"selector": "div.on", "declarations": [{"property": "--x", "value": "5px"}]},
      {"selector": "p", "declarations": [{"property": "margin", "value": "var(--x, 1px)"}]}
    ],
    "mutation": {"type": "remove_class", "node_id": "r", "class_name": "on"}
  }'
```

移除 `on` 后 `--x` 从链上消失（`r` 上 `ok → missing`，原因
`selector_match` + `cascade_winner`；`k` 上标 `inheritance`），
`k` 的 `margin` 胜出声明不变、由主分支**改道 fallback**，标
`var_chain`，`computed = '5px' → '1px'`。反向添加 `on` 则 fallback
重新并回主分支（`'1px' → '5px'`）。

循环解除同理：`p.c { --a: var(--b); --b: var(--a) }` 与
`p { --a: 1px; color: var(--a, 7px) }` 下，移除 `c` 使环解除——
`--a` 由 `invalid` 恢复为 `1px`（胜出者交还 `p` 规则），`--b` 变为
`missing`，`color` 由 fallback `7px` 并回主分支 `1px`；添加 `c`
则反向形成循环。这些场景在 `/stylesheet-audit` 中同样被共享签名覆盖：
删除会解除循环的声明（失效原因由「循环依赖」变为「var() 解析失败」）
会以 `reason_changed` 阻断，不会进入删除批次。

### 校验（HTTP 422，不产生部分结果）

所有结构 / 语法 / 语义校验都在任何求解之前完成，任一失败整次请求 422：

- 变更目标 `node_id` 不存在于树中；树中存在**重复 id**；id 为布尔 / 空串；
- **空操作**：`add_class` 时 class 已存在、`remove_class` 时不存在、
  `set_attribute` 时同名字段已是同值、`remove_attribute` 时字段不存在、
  `set_tag` 时与现 tag 相同（按小写比较）；
- 操作缺少必填字段（如 `add_class` 不给 `class_name`），或 `class_name` /
  `name` / `tag` 为空白；显式给 `null` 同样拒绝；
- 试图通过 attribute 操作修改 `id` / `class`；
- 规则选择器语法非法，或声明 `property` 为空白（与 `/stylesheet-audit`
  同一校验口径）。

注意：变更本身合法、但它对样式没有任何影响时（如添加一个没有任何规则
使用的 class）返回 **200**，只是 `affected_nodes` 为空数组；只有操作级
的空操作才返回 422。

## POST /selector-overlap：选择器可命中性判断

不提供 DOM 样本，仅根据两条选择器本身判断**是否存在**一个元素能被二者同时命中。
两侧复合段按子组合符（`>`）与后代组合符（空格）对齐到同一条 根 → 目标 的见证
节点链，合并 tag / id / class / 属性约束；搜索**节点数最少**的链，多解时按规范化
后的 根 → 目标 元素序列字典序取唯一结果，并用 `/compute` 同款匹配器复核两条选择器
确实命中。

请求体仅两个字段：`selector_a`、`selector_b`。选择器语法错误时沿用 `/compute`
的响应格式（HTTP 422）。

### 可满足示例

```bash
curl -X POST http://localhost:8000/selector-overlap \
  -H 'Content-Type: application/json' \
  -d '{"selector_a": "div.foo > section > p",
       "selector_b": "div.bar > section > p"}'
```

两侧都要求目标为 `p`、父为 `section`、祖父为 `div`，祖父的 `.foo` 与 `.bar`
可合并到同一节点：

```json
{
  "satisfiable": true,
  "selector_a": "div.foo > section > p",
  "selector_b": "div.bar > section > p",
  "element": {"tag": "p"},
  "ancestors": [
    {"tag": "div", "classes": ["bar", "foo"]},
    {"tag": "section"}
  ],
  "alignment": [
    {"depth": 2, "segment_a": 0, "segment_b": 0,
     "combinator_a": ">", "combinator_b": ">",
     "element": {"tag": "div", "classes": ["bar", "foo"]}},
    {"depth": 1, "segment_a": 1, "segment_b": 1,
     "combinator_a": ">", "combinator_b": ">",
     "element": {"tag": "section"}},
    {"depth": 0, "segment_a": 2, "segment_b": 2,
     "combinator_a": null, "combinator_b": null,
     "element": {"tag": "p"}}
  ],
  "conflict": null
}
```

`ancestors` 按 **根 → 父** 排列，`element` 为目标；见证节点只保留选择器要求的
字段。`alignment[]` 逐深度给出两侧段下标（按选择器书写顺序，0 起；`null` 表示
该侧此深度无段）、该段朝目标方向到下一段的组合符（`">"` / `" "` / `null`）。

后代组合符允许两侧祖先段错开深度，例如 `div p` 与 `section p` 可满足，最小链为
`div → section → p`（`div`、`section` 分居两层）。

### 不可满足示例：复合约束冲突

目标段必然对齐到同一元素；若两侧目标段对同一字段给出互斥要求，即为
`compound` 冲突：

```bash
curl -X POST http://localhost:8000/selector-overlap \
  -H 'Content-Type: application/json' \
  -d '{"selector_a": "p#x", "selector_b": "p#y"}'
```

```json
{
  "satisfiable": false,
  "selector_a": "p#x",
  "selector_b": "p#y",
  "element": null,
  "ancestors": [],
  "alignment": [],
  "conflict": {
    "kind": "compound",
    "segment_a": 0,
    "segment_b": 0,
    "field": "id",
    "value_a": "x",
    "value_b": "y",
    "reason": "目标段 selector_a[0] 要求 id 属性为 'x'，selector_b[0] 要求 id 属性为 'y'；命中同一元素时两侧目标段必须同时约束该元素，复合约束冲突"
  }
}
```

### 不可满足示例：子关系强制对齐冲突

若自目标起两侧连续的子组合符（`>`）把两段强制压到同一节点、中间没有后代间隙
可规避，而两段约束互斥，则为 `child` 冲突，并给出被强制对齐的两侧段下标与字段：

```bash
curl -X POST http://localhost:8000/selector-overlap \
  -H 'Content-Type: application/json' \
  -d '{"selector_a": "ul > li > a", "selector_b": "ol > li > a"}'
```

`li` 与目标 `a` 相容，但两条链都要求 `li` 的父节点恰好再上一层，于是 `ul` 与
`ol` 被迫落在同一节点，标签互斥：

```json
{
  "satisfiable": false,
  "selector_a": "ul > li > a",
  "selector_b": "ol > li > a",
  "element": null,
  "ancestors": [],
  "alignment": [],
  "conflict": {
    "kind": "child",
    "segment_a": 0,
    "segment_b": 0,
    "field": "tag",
    "value_a": "ul",
    "value_b": "ol",
    "reason": "自目标起两侧连续的子组合符 '>' 把 selector_a[0] 与 selector_b[0] 强制对齐到同一节点，但前者要求标签为 'ul'、后者要求为 'ol'，子关系强制对齐冲突（无后代间隙可规避）"
  }
}
```

`>` 刚性也会经**最近祖先吸附**逐层传播：弹性后代段（空格边）若匹配对侧被强制
落位的节点，只能吸附到该层占位，其后的 `>` 边便把更高层的段也压到同一深度。
例如 `div.x[data-k=2] > p p` 与 `span > p#a.y > p`：selector_a 的中间段 `p`
被吸附到 `p#a.y` 所在的深度 1 占位，两侧根段 `div` 与 `span` 随之被 `>` 推到
同一层，标签互斥。诊断同样返回 `kind: "child"` 与互斥字段，`reason` 会逐步
说明吸附占位与刚性传播过程。

单侧单段内部互斥（如 `[data-x=1][data-x=2]`）同样报 `compound`，此时
`segment_b` / `segment_a` 中不涉及的一侧为 `null`。

## POST /keyframe-sample：CSS 数值属性关键帧采样

给定单个**数值属性**的关键帧（偏移严格递增且覆盖 0、1，取值单位一致）、
`duration`、`delay`、正整数 `iteration_count`、`direction`
（`normal` / `reverse` / `alternate` / `alternate-reverse`）、`fill_mode`
（`none` / `forwards` / `backwards` / `both`）、`linear` 或
`cubic-bezier(x1,y1,x2,y2)` 缓动及查询时刻 `time`，按 Web Animations 语义
求该时刻的属性值，并返回**阶段、轮次、帧段、段内局部进度、缓动进度、取值**。

8000 端口示例（请求体见 `examples/keyframe-sample.json`）：

```bash
curl -X POST http://localhost:8000/keyframe-sample \
  -H 'Content-Type: application/json' \
  -d @examples/keyframe-sample.json
```

```json
{
  "keyframes": [
    {"offset": 0, "value": "0px"},
    {"offset": 0.5, "value": "100px"},
    {"offset": 1, "value": "200px"}
  ],
  "duration": 1000,
  "delay": 200,
  "iteration_count": 2,
  "direction": "alternate",
  "fill_mode": "both",
  "easing": "cubic-bezier(0.42, 0, 0.58, 1)",
  "time": 700
}
```

`time=700` 位于活动区间 `[200, 2200)` 的第 0 轮，轮内进度 0.5（中间帧边界，
内部边界归以该帧为起点的下一段），响应：

```json
{
  "phase": "active",
  "iteration": 0,
  "segment": 1,
  "local_progress": 0.0,
  "eased_progress": 0.0,
  "value": "100px"
}
```

### 阶段、轮次与边界规则

活动区间为 `[delay, delay + duration * iteration_count)`：

- 查询时刻早于 `delay` 为 `before`，晚于活动终点为 `after`；
- 活动区间内按 `floor((time - delay) / duration)` 定位 0 起轮次，
  **内部边界归下一轮**（如两轮时 `time = delay + duration` 属第 1 轮起点）；
- 活动区间**末边界**（`time = delay + duration * iteration_count`）取
  **末轮终点**（第 `iteration_count - 1` 轮、局部进度 1）；
- `reverse` 整体反向；`alternate` 偶数轮（0 起）正向、奇数轮反向；
  `alternate-reverse` 首轮反向、之后交替。

### 填充

- `before`：`fill_mode` 为 `backwards` 或 `both` 时取**首轮起点**
  （轮次 0、局部进度 0，方向变换随之生效），否则除 `phase` 外一律 `null`；
- `after`：`fill_mode` 为 `forwards` 或 `both` 时取**末轮终点**
  （轮次 `iteration_count - 1`、局部进度 1），否则除 `phase` 外一律 `null`。

例如上例 `time=0`（before）取首轮起点 `0px`；`time=2300`（after）取末轮
（第 1 轮，alternate 下为反向轮）终点，即关键帧起点 `0px`；
`fill_mode: "none"` 时两种情况均返回：

```json
{
  "phase": "after", "iteration": null, "segment": null,
  "local_progress": null, "eased_progress": null, "value": null
}
```

### 帧段、缓动与取值

按方向变换后的关键帧空间进度定位帧段 `segment`（起点关键帧下标；帧边界
归入以该帧为起点的段，仅末边界取末段终点），计算段内 `local_progress`，
再对 `cubic-bezier` **由 x 反解参数 t、求 y** 得 `eased_progress`
（`linear` 时二者相同；y 控制点允许超出 `[0,1]` 以支持回弹过冲），
最后在相邻关键帧数值间线性插值，输出时拼回原单位（单位大小写不敏感，
输出归一为小写）。

### 校验（HTTP 422）

以下情况返回 422 并在 `detail` 中说明原因：

- `duration` 非正；
- 关键帧少于 2 个、偏移未覆盖 0 与 1、非严格递增（无序或重复）；
- 取值不是单个 CSS 数值（如 `red`、`1px 2px`）或关键帧间**单位冲突**；
- 任一数值（含 delay / duration / time / 贝塞尔控制点）为非有限值
  （`NaN` / `Infinity`）；
- `iteration_count` 不是正整数（含布尔值）；
- `easing` 既非 `linear` 也非合法 `cubic-bezier(...)`，或其
  **x 控制点不在 `[0,1]`** 内。

## POST /counter-render：CSS 计数器元素树渲染

接收一棵带唯一 `id` 与 `children` 的元素树，按 CSS 计数器语义模拟
`counter-reset` / `counter-set` / `counter-increment` 与
`counter()` / `counters()`，先序遍历渲染每个节点的 `content`，并返回
逐节点的**作用域变更轨迹**。

```bash
curl -X POST http://localhost:8000/counter-render \
  -H 'Content-Type: application/json' \
  -d @examples/counter-render.json
```

### 请求模型

- `root`：树根节点。每个节点字段：
  - `id`（必填）：唯一标识，非空字符串或整数，全树不得重复（`1` 与 `"1"` 类型不同，不算重复）；
  - `counter_reset` / `counter_set` / `counter_increment`：操作数组，单条时也可直接写对象；
    每条为 `{"op": "reset|set|increment", "name": "章节号", "value": 0}`，
    `value` 缺省时 reset / set 取 **0**、increment 取 **1**（必须为整数）；
  - `content`：片段数组，片段可以是**裸字符串**（字面文本）或对象：
    - `{"kind": "text", "value": "字面文本"}`；
    - `{"kind": "counter", "name": "n", "style": "decimal"}`；
    - `{"kind": "counters", "name": "n", "separator": ".", "style": "decimal"}`，
      `style` 缺省为 `decimal`；
  - `children`：子节点数组。
- 支持的计数格式（`style`）：
  - `decimal`（默认）：`…, 9, 10, 11, …`；
  - `decimal-leading-zero`：`00, 01, …, 09, 10, 11, …`（仅 0–9 补前导零）；
  - `lower-alpha`（别名 `lower-latin`）：`a, b, …, z, aa, ab, …`；
  - `upper-alpha`（别名 `upper-latin`）：`A, B, …, Z, AA, AB, …`；
  - 字母格式遇到非正整数（如 0、-1）超出样式范围时，按 CSS 规范回退为十进制。
- 计数器名称不能为空，只能包含字母、数字、连字符、下划线，且不能以数字或连字符开头。

### 处理顺序与作用域

对每个节点按 **reset → set → increment → 渲染 content** 的顺序处理，然后先序递归
`children`：

- **reset**：以给定值（缺省 0）新建一个只在**当前节点子树**内生效的实例并压栈，
  遮蔽同名外层实例；节点子树处理完毕后弹栈——外层实例**恢复**（`restore`），
  若外层本不存在则**销毁**（`destroy`）。同一节点多次 reset 同名计数器产生嵌套实例，
  退出时按**逆序**弹出。
- **set / increment**：作用于当前最内层（栈顶）实例；若计数器尚不存在，**先以 0
  建立实例**（轨迹中先出现 `create` 再出现 `set` / `increment`），该实例同样在退出
  当前节点时销毁。若外层已存在实例，则按 CSS 语义直接改写外层实例，不产生新作用域。
- **counter**：读取最内层值；**counters**：把栈内各层值按**从外到内**顺序格式化后
  用 `separator` 连接。
- `content` 中引用不存在的计数器返回 422（包括惰性实例随子树退出销毁后的引用）。

### 响应

`nodes[]` 按**先序**排列，每项包含节点 `id`、渲染出的 `text`，以及作用域变更轨迹：
`enter[]` 为本节点 reset/set/increment 阶段的变更，`exit[]` 为本节点子树退出时的
恢复 / 销毁。轨迹事件字段：

| type | phase | 含义 |
| --- | --- | --- |
| `reset` | enter | 新建实例压栈；`value` 为新值，`shadow` 为被遮蔽的外层值（无则 null） |
| `create` | enter | set / increment 引用缺失计数器，先以 0 建立实例 |
| `set` / `increment` | enter | 写入 / 累加后的值见 `value` |
| `restore` | exit | reset 实例弹出，外层实例恢复为 `value` |
| `destroy` | exit | 实例弹出后栈中再无该计数器（reset 或惰性建立） |

下例展示嵌套编号与退出恢复：根 reset `chapter=0`，第一章 increment 到 1，章内
reset `section=0`，小节 increment 后 `counters()` 得到 `"1.01"`；`note` 节点上
set 缺失的 `footnote` 先以 0 建立再置 4（渲染为 `e`），该实例只在其子树可见：

```bash
curl -X POST http://localhost:8000/counter-render \
  -H 'Content-Type: application/json' \
  -d '{
    "root": {
      "id": "book",
      "counter_reset": [{"op": "reset", "name": "chapter"}],
      "children": [
        {"id": "ch1",
         "counter_increment": [{"op": "increment", "name": "chapter"}],
         "content": [{"kind": "counter", "name": "chapter"}],
         "children": [
           {"id": "sec-wrap",
            "counter_reset": [{"op": "reset", "name": "section"}],
            "children": [
              {"id": "s1",
               "counter_increment": [{"op": "increment", "name": "section"}],
               "content": [{"kind": "counters", "name": "chapter", "separator": "."},
                           ".", {"kind": "counters", "name": "section", "separator": ".",
                                 "style": "decimal-leading-zero"}]}
            ]}
         ]},
        {"id": "after", "content": [{"kind": "counter", "name": "chapter"}]}
      ]
    }
  }'
```

```json
{
  "nodes": [
    {"id": "book", "text": "",
     "enter": [{"phase": "enter", "type": "reset", "name": "chapter",
                "value": 0, "shadow": null}],
     "exit": [{"phase": "exit", "type": "destroy", "name": "chapter",
               "value": null, "shadow": null}]},
    {"id": "ch1", "text": "1",
     "enter": [{"phase": "enter", "type": "increment", "name": "chapter",
                "value": 1, "shadow": null}],
     "exit": []},
    {"id": "sec-wrap", "text": "",
     "enter": [{"phase": "enter", "type": "reset", "name": "section",
                "value": 0, "shadow": null}],
     "exit": [{"phase": "exit", "type": "destroy", "name": "section",
               "value": null, "shadow": null}]},
    {"id": "s1", "text": "1.01", "enter": [], "exit": []},
    {"id": "after", "text": "1", "enter": [], "exit": []}
  ]
}
```

### 校验（HTTP 422）

以下情况返回 422 并在 `detail` 中说明原因：

- 节点 `id` 缺失、为空、类型非法，或全树重复；
- 计数器名称非法（空、以数字 / 连字符开头、含非法字符）；
- 计数格式不在四种支持样式内；
- `counters` 片段缺少 `separator`，或 counter/counters 片段缺少名称；
- 操作值不是整数（如 `1.5`、`true`）；
- 渲染时 `counter()` / `counters()` 引用了当前作用域内不存在的计数器
  （惰性建立的实例退出子树后同样不再可见）。

## POST /math-evaluate：CSS 数学表达式求值

计算 CSS 数学表达式：支持 `calc()`、`min()`、`max()`、`clamp()`、括号与
`+`、`-`、`*`、`/`；运算数支持无单位数与 `px` / `em` / `rem` / `vw` /
`vh` / `%`。所有带单位运算数按 `context` 换算并**规范化为 px 长度**，响应
返回规范化数值与单位、表达式 AST 与**逐节点求值轨迹**（后序，最后一步即
根节点结果，`node_id` 与 AST 节点一一对应）。

8000 端口示例（请求体见 `examples/math-evaluate.json`）：

```bash
curl -X POST http://localhost:8000/math-evaluate \
  -H 'Content-Type: application/json' \
  -d @examples/math-evaluate.json
```

请求体：

```json
{
  "expression": "calc(min(100%, 480px) / 2 + 1em)",
  "context": {
    "font_size": 16,
    "root_font_size": 16,
    "viewport_width": 1024,
    "viewport_height": 768,
    "percentage_basis": 800
  }
}
```

`min(100%, 480px)` 把百分比按 `percentage_basis=800` 换算为 `800px` 后
取较小值 `480px`，再 `/ 2 + 1em`（`font_size=16`）得到 `256px`。响应：

```json
{
  "expression": "calc(min(100%, 480px) / 2 + 1em)",
  "value": 256.0,
  "unit": "px",
  "ast": {
    "id": 1, "type": "function", "name": "calc",
    "args": [
      {"id": 2, "type": "binary", "op": "+",
       "left": {"id": 3, "type": "binary", "op": "/",
                "left": {"id": 4, "type": "function", "name": "min", "args": [
                  {"id": 5, "type": "number", "value": 100.0, "unit": "%"},
                  {"id": 6, "type": "number", "value": 480.0, "unit": "px"}
                ]},
                "right": {"id": 7, "type": "number", "value": 2.0, "unit": ""}},
       "right": {"id": 8, "type": "number", "value": 1.0, "unit": "em"}}
    ]
  },
  "trace": [
    {"node_id": 5, "kind": "number",
     "detail": "100% 按 percentage_basis=800 换算为 800px", "value": 800.0, "unit": "px"},
    {"node_id": 6, "kind": "number", "detail": "长度 480px", "value": 480.0, "unit": "px"},
    {"node_id": 4, "kind": "min", "detail": "min(800px, 480px) = 480px", "value": 480.0, "unit": "px"},
    {"node_id": 7, "kind": "number", "detail": "无单位数 2", "value": 2.0, "unit": ""},
    {"node_id": 3, "kind": "divide", "detail": "480px / 2 = 240px", "value": 240.0, "unit": "px"},
    {"node_id": 8, "kind": "number",
     "detail": "1em 按 font_size=16 换算为 16px", "value": 16.0, "unit": "px"},
    {"node_id": 2, "kind": "add", "detail": "240px + 16px = 256px", "value": 256.0, "unit": "px"},
    {"node_id": 1, "kind": "calc", "detail": "calc() 透明包装：256px", "value": 256.0, "unit": "px"}
  ]
}
```

### 语法

- 最外层可写 `calc(...)`，也可直接写裸表达式（`1px + 2px`）；
- 运算符优先级：一元 `+` / `-` > `*` / `/` > `+` / `-`，括号改变结合顺序；
  数字支持科学计数法（`1.5e-1`），单位与函数名大小写不敏感（AST 归一为小写），
  `+` / `-` 两侧不要求空白；
- `calc()` 接受 1 个表达式参数；`min()` / `max()` 接受 1 个以上逗号分隔参数；
  `clamp(最小值, 首选值, 最大值)` 必须是 3 个参数，按 CSS 语义取
  `max(最小值, min(首选值, 最大值))`（下限大于上限时下限胜出）。

### 单位换算与 context

所有长度统一规范化为 **px**，`context` 各项均可选，但表达式用到对应单位时
必须提供，否则 422：

| 单位 | 换算方式 | 所需 context 字段 |
| --- | --- | --- |
| `px` | 无需换算 | — |
| `em` | `值 × font_size` | `font_size` |
| `rem` | `值 × root_font_size` | `root_font_size` |
| `vw` | `值 × viewport_width / 100` | `viewport_width` |
| `vh` | `值 × viewport_height / 100` | `viewport_height` |
| `%` | `值 × percentage_basis / 100` | `percentage_basis` |

提供的 context 值必须为非负有限数值。无单位数不做换算，结果 `unit` 为 `""`。

### 维度规则

- 加、减与 `min` / `max` / `clamp` 的全部参数必须**同维**
  （长度与长度、无单位数与无单位数），如 `calc(1px + 1)` 为维度冲突；
- 乘法**最多一侧带维度**：长度 × 无单位数为长度，长度 × 长度为维度冲突；
- 除法的**除数必须是非零无单位数**，商的维度与被除数相同
  （`6px / 2 = 3px`；除以带维度值或以 0 为除数均为 422）。

### 校验（HTTP 422）

以下情况返回 422 并在 `detail` 中说明原因：

- **语法错误**：空表达式、非法字符、多余记号、括号 / 参数不闭合、
  不支持的函数（仅 calc/min/max/clamp）或单位（仅 px/em/rem/vw/vh/%）、
  `calc()` 参数数不为 1、`clamp()` 参数数不为 3、嵌套超过 100 层；
- **缺少换算上下文**：用到 `em` / `rem` / `vw` / `vh` / `%` 但 context
  未提供对应字段；
- **维度冲突**：加减或函数参数不同维、乘法两侧均带维度、除数带维度；
- **除零**：除数为 0（包括子表达式求值结果为 0）；
- **非有限结果**：字面值溢出或运算得到 `NaN` / `Infinity`。

## POST /grid-tracks：单行 CSS Grid 列宽求解

求解单行（单列轨道、项目不换行）CSS Grid 的各列宽度。请求给出 `container_width`
（容器宽 px）、`gap`（列间距 px）、`columns`（列轨道定义）与 `items`（带定位的
项目）。轨道定义支持四种写法（单位大小写不敏感，但必须显式给出）：

- `px` 固定轨道：`120px`；
- 百分比轨道：`25%`（相对 `container_width` 换算为 px）；
- `fr` 弹性轨道：`1fr`、`2fr`；
- `minmax(下限, 上限)`：下限只能是 `px` / `%`，上限可为 `px` / `%` / `fr`，
  如 `minmax(100px, 1fr)`、`minmax(10%, 30%)`；下限大于上限时按 CSS 语义
  把下限收缩为上限。

每个项目为 `{"start": 起始列号(1 起), "span": 跨越列数, "min_content": 最小内容宽,
"name": 可选名称}`。求解分两阶段，响应返回各列的基础尺寸 `base`、增长上限 `cap`
（`null` 表示无上限）、fr 因子与最终尺寸 `final_size`，以及完整逐轮轨迹 `trace`。

8000 端口示例（请求体见 `examples/grid-tracks.json`）：

```bash
curl -X POST http://localhost:8000/grid-tracks \
  -H 'Content-Type: application/json' \
  -d @examples/grid-tracks.json
```

```json
{
  "container_width": 1000,
  "gap": 10,
  "columns": ["100px", "minmax(0px, 120px)", "minmax(0px, 1fr)", "1fr"],
  "items": [
    {"name": "hero", "start": 3, "span": 2, "min_content": 300},
    {"name": "tag", "start": 2, "span": 1, "min_content": 80}
  ]
}
```

### 阶段一：项目最小内容缺口

项目按**跨度从短到长**处理，同跨度按**输入顺序**处理（顺序见响应的
`processing_order`，为输入下标数组）。对每个项目，其跨列轨道当前尺寸之和不足
`min_content` 时，把缺口**等额分给**跨列中仍可增长的轨道（尺寸小于自身 `cap`、
含上限为 `fr` 的无上限轨道）：

- 若某轨道分到的份额会超过其上限，则把它补足到上限、移出分配集合，**其份额转给
  其余可增长轨道**后重算一轮（`trace[].rounds[]` 逐轮记录 `shortfall` 缺口、
  `growable` 候选列、等额 `share`、本轮触顶的 `capped` 列与接收回流的
  `redistributed` 列）；
- 全部候选轨道触顶后仍不足，项目无法满足，在 `overflow[]` 中列出
  `item_index` / `name` / 定位 / `min_content` / `available` / 缺口 `gap`，
  该轨迹步的 `unmet` 给出剩余缺口。

上例中跨度 1 的 `tag` 先处理，把第 2 列从 0 撑到 80；跨度 2 的 `hero` 后处理，
缺口 300 由第 3、4 列各分 150。

### 阶段二：剩余空间按 fr 迭代分配

扣除间距与全部非弹性轨道（px / % / 无 fr 因子的 minmax）后，把剩余空间按 fr
因子比例分给弹性轨道（含上限为 fr 的 minmax），每 fr 份额
`fr_size = 剩余空间 / fr 总和`，各弹性轨道目标尺寸为 `fr * fr_size`：

- 若某活动轨道按比例算出的尺寸**低于其基础尺寸**（基础尺寸可能在阶段一被
  min_content 撑大），先把该轨道按基础尺寸**固定**、移出弹性集合，扣除其基础
  尺寸后对其余轨道**重算份额**，直至所有活动轨道份额都不低于基础尺寸；
- 逐轮过程见 `trace` 中 `phase: "fr"` 的步：`free_space` / `active` /
  `fr_sum` / `fr_size` / `frozen`（本轮固定的列）/ `targets`（各列目标尺寸）。

上例扣除 `100 + 80 + 3×10 = 210` 后剩余 790，由第 3、4 列两个 1fr 均分：

```json
{
  "processing_order": [1, 0],
  "tracks": [
    {"index": 0, "definition": "100px", "kind": "px",
     "base": 100.0, "cap": 100.0, "fr": 0.0, "final_size": 100.0},
    {"index": 1, "definition": "minmax(0px, 120px)", "kind": "minmax",
     "base": 0.0, "cap": 120.0, "fr": 0.0, "final_size": 80.0},
    {"index": 2, "definition": "minmax(0px, 1fr)", "kind": "minmax",
     "base": 0.0, "cap": null, "fr": 1.0, "final_size": 395.0},
    {"index": 3, "definition": "1fr", "kind": "fr",
     "base": 0.0, "cap": null, "fr": 1.0, "final_size": 395.0}
  ],
  "used_width": 1000.0,
  "container_overflow": 0.0,
  "overflow": [],
  "trace": ["...阶段一两个项目的 min_content 步与阶段二的 fr 步（逐轮）..."]
}
```

固定轨道自身超出容器时，最终尺寸之和（含间距）记入 `used_width`，超出部分记入
`container_overflow`。

### 触顶回流与固定重算示例

`minmax(0px, 100px)` 触顶后份额回流：容器 1000、列
`["minmax(0px,100px)", "minmax(0px,1fr)", "1fr"]`，项目跨前两列、
`min_content=300`：首轮各分 150，第 1 列触顶 100，50 回流给第 2 列（阶段一末
为 `[100, 200, 0]`）；阶段二 900 由两个 1fr 均分，最终 `[100, 450, 450]`。

份额低于基础尺寸时固定重算：容器 1000、列
`["minmax(600px,1fr)", "1fr", "1fr"]`：首轮每 fr 份额 1000/3≈333，低于第 1 列
基础尺寸 600，固定第 1 列后剩余 400 由另两列均分，最终 `[600, 200, 200]`，
`trace` 的 fr 步包含两轮记录。

### 校验（HTTP 422）

以下情况返回 422 并在 `detail` 中说明原因：

- `container_width` / `gap` / `min_content` 为负或非有限值，或间距合计超出容器宽；
- 轨道语法错误：空定义、缺单位（裸数字）、未知单位 / 函数、参数个数错误、
  `minmax()` 下限使用 fr 或上限非法、多余逗号 / 嵌套函数；
- 项目 `start` / `span` 不是正整数（含布尔、浮点）、`start < 1`，或
  `start + span - 1` 超出列数越界；
- `columns` 为空。

## POST /color-mix：按 CSS Color 规则混合两种颜色

按 CSS Color 4/5 的 `color-mix()` 语义混合两种颜色。输入颜色接受：

- hex：`#rgb` / `#rgba` / `#rrggbb` / `#rrggbbaa`（大小写不敏感）；
- `rgb()` / `rgba()`：旧式逗号语法（`rgba(255, 0, 0, 0.5)`）与现代空格 +
  斜杠语法（`rgb(255 0 0 / 50%)`），通道可同为 0–255 数值或同为百分比；
- `hsl()` / `hsla()`：`hsl(240, 100%, 50%)` 或 `hsl(0.5turn 100% 50% / .5)`，
  hue 支持 `deg` / `rad` / `grad` / `turn`，hue 也可写 `none` 表示缺失。

请求字段：`color_a`、`color_b`（必填）、`weight_a` / `weight_b`（可选百分比数值）、
`interpolation_space`（`srgb` / `srgb-linear` / `hsl` / `oklab` / `oklch`，
缺省 `oklab`）、`hue_interpolation`（仅极坐标空间 `hsl` / `oklch` 可用：
`shorter` / `longer` / `increasing` / `decreasing`，缺省 `shorter`）。

8000 端口示例（请求体见 `examples/color-mix.json`）：

```bash
curl -X POST http://localhost:8000/color-mix \
  -H 'Content-Type: application/json' \
  -d @examples/color-mix.json
```

### 权重规则

- 两个权重都省略时各取 **50%**；只省略一个时补到 **100%**（如 `weight_a=25`
  则 B 为 75%；补出负值返回 422）；
- 给定的权重按**和归一化**（和 120% 时 30/90 → 0.25/0.75）；
- 权重和**小于 100%** 时，结果 alpha 同比乘以「和 / 100」（如 20%+30% 时
  alpha 再乘 0.5）；和不少于 100% 时不缩放；
- 负权重或权重和为 0 返回 422。

### 转换、插值与 hue 弧线

两侧颜色先逐跳转换到目标插值空间（经 sRGB 与 linear-sRGB 中转，完整坐标轨迹
见响应的 `conversions`），颜色通道以**预乘 alpha** 线性插值（hue 通道不预乘），
alpha 本身按权重插值；插值后还原预乘，再经 `back_conversion` 转回 sRGB，
超出 `[0,1]` 的通道**逐通道裁剪**（`clipped` 逐通道标记）。

极坐标空间（`hsl` / `oklch`）按 CSS Color 4 处理 hue：

- `shorter`（缺省）：取小于 180° 的弧；`longer`：取大于 180° 的弧
  （两侧 hue 相同也绕整周）；
- `increasing`：终点 hue 单调不减；`decreasing`：终点 hue 单调不增；
- 无彩色一侧（hsl 的 `s=0` 或 `l=0%/100%`、oklch 的 `c=0`）**缺失 hue**
  取另一侧 hue；两侧都缺失时按 0° 处理且结果 hue 仍缺失。

例如 sRGB 空间 50% 红 + 50% 蓝：

```bash
curl -X POST http://localhost:8000/color-mix \
  -H 'Content-Type: application/json' \
  -d '{"color_a": "#ff0000", "color_b": "#0000ff",
       "interpolation_space": "srgb"}'
```

响应（节选）：

```json
{
  "color_a": {"raw": "#ff0000", "syntax": "hex", "given_weight": null,
              "normalized_weight": 0.5, "srgb": [1.0, 0.0, 0.0],
              "alpha": 1.0},
  "color_b": {"raw": "#0000ff", "syntax": "hex", "given_weight": null,
              "normalized_weight": 0.5, "srgb": [0.0, 0.0, 1.0],
              "alpha": 1.0},
  "weights": {"given": [null, null], "completed": [50.0, 50.0], "sum": 100.0,
              "normalized": [0.5, 0.5], "alpha_scale": 1.0},
  "interpolation_space": "srgb",
  "hue_interpolation": null,
  "conversions": [
    {"space": "srgb", "coords": [1.0, 0.0, 0.0], "missing": [false, false, false],
     "alpha": 1.0, "premultiplied": false},
    {"space": "srgb", "coords": [0.0, 0.0, 1.0], "missing": [false, false, false],
     "alpha": 1.0, "premultiplied": false}
  ],
  "interpolation": {
    "space": "srgb", "hue_interpolation": null,
    "start": {"space": "srgb", "coords": [1.0, 0.0, 0.0],
              "missing": [false, false, false], "alpha": 1.0, "premultiplied": true},
    "end": {"space": "srgb", "coords": [0.0, 0.0, 1.0],
            "missing": [false, false, false], "alpha": 1.0, "premultiplied": true},
    "hue_adjustment": null,
    "mixed": {"space": "srgb", "coords": [0.5, 0.0, 0.5],
              "missing": [false, false, false], "alpha": 1.0, "premultiplied": true},
    "result": {"space": "srgb", "coords": [0.5, 0.0, 0.5],
               "missing": [false, false, false], "alpha": 1.0, "premultiplied": false}
  },
  "back_conversion": [
    {"space": "srgb", "coords": [0.5, 0.0, 0.5], "missing": [false, false, false],
     "alpha": 1.0, "premultiplied": false}
  ],
  "clipped": [false, false, false],
  "rgba": {"r": 128, "g": 0, "b": 128, "alpha": 1.0,
           "css": "rgba(128, 0, 128, 1)",
           "srgb_unclamped": [0.5, 0.0, 0.5], "srgb": [0.5, 0.0, 0.5]},
  "trace": [
    "两个权重均省略：各取 50%",
    "给定权重 50% / 50%（和 100%）按和归一化：0.500000 / 0.500000",
    "权重和不少于 100%：alpha 不再缩放",
    "解析颜色 A '#ff0000'（hex）：sRGB=(1.0, 0.0, 0.0)，alpha=1",
    "解析颜色 B '#0000ff'（hex）：sRGB=(0.0, 0.0, 1.0)，alpha=1",
    "插值空间：srgb；hue 弧线：shorter",
    "颜色 A 经 srgb 转换到 srgb，坐标 [1.0, 0.0, 0.0]",
    "颜色 B 经 srgb 转换到 srgb，坐标 [0.0, 0.0, 1.0]",
    "预乘 alpha 后按 0.500000/0.500000 线性插值：[1.0, 0.0, 0.0] -> [0.0, 0.0, 1.0]，混合得 [0.5, 0.0, 0.5]（alpha=1）",
    "还原预乘：目标空间坐标 [0.5, 0.0, 0.5]，alpha=1",
    "转回 sRGB：[0.5, 0.0, 0.5]（无需裁剪）"
  ]
}
```

不同插值空间给出不同结果：同样的红 + 蓝中点，`srgb` 为 `rgb(128 0 128)`、
`srgb-linear` 为 `rgb(188 0 188)`（线性光中点更亮）、缺省 `oklab` 为
`rgb(140 83 162)`。hsl 空间中 `hsl(0 100% 50%)` 与 `hsl(240 100% 50%)`：
`shorter` 经品红（中点 hue 300°），`longer` 经绿（中点 hue 120°）。

### 校验（HTTP 422）

以下情况返回 422 并在 `detail` 中说明原因：

- 颜色不是合法 hex / `rgb()` / `rgba()` / `hsl()` / `hsla()`（含长度非法的
  hex、参数个数错误、rgb 通道数值 / 百分比混用、s/l 非百分比、未知颜色函数）；
- 权重为负、非有限值，补齐后权重为负（给定一侧超过 100%），或权重和为 0；
- 在矩形空间（`srgb` / `srgb-linear` / `oklab`）指定 `hue_interpolation`。

## POST /gamut-map：OKLCH -> sRGB 色域映射

设计系统常以 OKLCH 描述颜色，但 sRGB 屏幕装不下高色度颜色；把越界通道
**逐通道硬裁剪**会改变通道间比例、造成色相偏移。本端点把 CSS `oklch()`
颜色安全落到 sRGB：**原色已在色域内时原样返回**；否则**固定 L、h 与
alpha**，在 `[0, 原 C]` 上**二分查找仍处于色域内的最大 C**，区间宽度
不大于 `tolerance` 时结束。

请求字段：

- `color`（必填）：CSS `oklch()`，L 取 0..1 数值或百分比（`100% = 1`），
  C 取数值或百分比（`100% = 0.4`），H 为角度（`deg`/`rad`/`grad`/`turn`，
  裸数字按 `deg`）或 `none`，可带斜杠 alpha（0..1 或百分比）；
- `tolerance`（可选，缺省 `1e-5`）：二分终止的区间宽度，必须在
  `[1e-7, 1e-3]` 内。

8000 端口示例（请求体见 `examples/gamut-map.json`）：

```bash
curl -X POST http://localhost:8000/gamut-map \
  -H 'Content-Type: application/json' \
  -d @examples/gamut-map.json
```

```json
{
  "color": "oklch(70% 0.3 30 / 0.8)",
  "tolerance": 0.00001
}
```

`oklch(0.7 0.3 30)` 的未裁剪 sRGB 为 `[1.173, -0.223, -0.194]`（r 超上限、
g/b 为负），需要映射；固定 L=0.7、h=30°、alpha=0.8 二分 15 轮后把 C 降到
约 0.19151。响应（`rounds` 仅列前两轮）：

```json
{
  "input": "oklch(70% 0.3 30 / 0.8)",
  "tolerance": 1e-05,
  "original": {"l": 0.7, "c": 0.3, "h": 30.0, "alpha": 0.8},
  "mapped": {"l": 0.7, "c": 0.19151001, "h": 30.0, "alpha": 0.8},
  "was_mapped": true,
  "chroma_loss": 0.10848999,
  "srgb_unclamped": [1.172537822, -0.223225101, -0.193761277],
  "srgb": [0.999996159, 0.396246666, 0.318219268],
  "rgba": {"r": 255, "g": 101, "b": 81, "alpha": 0.8,
           "css": "rgba(255, 101, 81, 0.8)"},
  "rounds": [
    {"lo": 0.0, "hi": 0.3, "mid": 0.15, "in_gamut": true},
    {"lo": 0.15, "hi": 0.3, "mid": 0.225, "in_gamut": false}
  ],
  "trace": ["...解析、色域判定、二分与最终结果的中文逐步轨迹..."]
}
```

响应字段：

- `original` / `mapped`：原始与映射后的 OKLCH（`C=0` 或 hue 写 `none`
  时 `h` 为 `null`）；映射只降低 C，`l` / `h` / `alpha` 保持不变；
- `was_mapped`：是否发生了映射（原色已在色域内时为 `false`，此时
  `mapped` 与 `original` 相同、`chroma_loss` 为 0、`rounds` 为空）；
- `chroma_loss`：色度损失，即 `原 C - 映射后 C`；
- `srgb_unclamped`：原色直接转出的 sRGB（未裁剪，可越界）；
- `srgb` / `rgba`：最终 sRGB（色域内）与其 8 位 `rgba()` 形式；
- `rounds`：二分每轮的上下界——`lo` / `hi` 为进入本轮时的区间，
  `mid` 为试取中点，`in_gamut` 标记中点是否仍在色域内
  （在则 `lo` 升为 `mid`，否则 `hi` 降为 `mid`）。

### 校验（HTTP 422）

以下情况返回 422 并在 `detail` 中说明原因：

- 非法语法：非 `oklch()` 函数、通道数不为 3、使用逗号分隔、多个斜杠、
  hue 非角度（L / C / alpha 不接受 `none`）；
- 非有限值：任一通道或 `tolerance` 为 `NaN` / `Infinity`
  （如 `1e999` 解析出的无穷）；
- L 或 alpha 越出 `[0, 1]`（越界不裁剪，直接报错）；
- C 为负；
- `tolerance` 不在 `[1e-7, 1e-3]` 内。

## /compute 请求模型

| 字段 | 说明 |
| --- | --- |
| `element` | 目标元素：`tag` / `id` / `classes` / `attributes` |
| `ancestors` | 祖先链，按 **根 → … → 父** 顺序 |
| `layers` | 层声明顺序，按 **早 → 晚**；规则引用未声明的层时按首次出现追加到末尾 |
| `rules[]` | 结构化规则：`selector`、`declarations[]`、`origin`（`ua`/`user`/`author`，默认 `author`）、`layer`（`null` 表示未分层）、`order`（源码次序，默认取数组下标） |
| `declarations[]` | `property`、`value`、`important`（默认 `false`） |
| `patches[]` | 可选，样式变更预演补丁：`rule_index` + `declaration_index` 定位，`value` / `important` / `layer` 至少给一个；任一定位非法整次 422，详见上文 |

## 选择器支持

标签（`div`）、通配（`*`）、`#id`、`.class`、`[attr]`、`[attr=value]`（值可带引号）、
后代组合符（空格）与子组合符（`>`）。特异性按 `(id 数, class+属性数, 标签数)` 计算。

## 层叠规则

普通声明按以下次序比较，前者分出胜负即停止：

1. **来源**：`author > user > ua`
2. **层**：同来源下 **未分层 > 分层**；分层中 **晚层 > 早层**
3. **特异性**：`(a, b, c)` 字典序
4. **源码次序**：`order` 大者胜；同规则内同属性后者胜

`important` 声明优先于一切普通声明，且**来源与层级顺序反转**：

- 来源反转为 `ua > user > author`（UA 的 important 最强）
- 层级反转为 **分层 > 未分层**、**早层 > 晚层**
- 之后仍按特异性、源码次序比较

每条落选声明都会在 `evidence[].eliminated[]` 中给出被淘汰的阶段
（`importance` / `origin` / `layer` / `specificity` / `source_order`）与中文说明。
`evidence[].winner` 与每条候选都带 `rule_index` / `declaration_index`，
即它在请求 `rules` / `declarations` 中的下标，也是 `patches` 的定位依据。

## 自定义属性与 var()

- 自定义属性（`--*`）沿祖先链**先继承**（取祖先的计算值），本元素的声明再按同一套层叠规则覆盖；
  逐元素的解析轨迹见响应的 `custom_property_trace`。
- `var(--name, fallback)` 递归解析，fallback 可嵌套 `var()`、可含逗号；替换步骤记录在
  `evidence[].var_steps`。
- 处于**循环依赖**中的自定义属性全部失效（含自引用 `var(--a)` 于 `--a`）。
- 引用**缺失或已失效**变量且没有 fallback 的声明标记为 `invalid`（`computed` 中对应值为 `null`，
  原因见 `invalid[]`）；有 fallback 时回退到 fallback。
- 自定义属性名大小写敏感；普通属性名按小写归一。

## 项目结构

```
app/
  main.py       FastAPI 入口（POST /compute、POST /stylesheet-audit、
                POST /dom-impact、POST /selector-overlap、
                POST /keyframe-sample、
                POST /counter-render、POST /math-evaluate、POST /grid-tracks、
                POST /color-mix、POST /gamut-map、GET /health）
  models.py     请求 / 响应模型
  selector.py   选择器解析与匹配、特异性计算
  overlap.py    选择器可命中性对齐搜索与冲突诊断
  keyframe.py   数值关键帧采样：阶段/轮次/方向、贝塞尔反解与插值
  counters.py   计数器元素树：reset/set/increment 作用域栈与
                counter()/counters() 渲染
  math_eval.py  CSS 数学表达式：calc/min/max/clamp 词法语法分析、
                单位换算、维度规则与逐节点求值轨迹
  grid_tracks.py 单行 Grid 列宽：轨道解析、min_content 缺口等额分摊
                （触顶份额回流）与 fr 剩余空间迭代分配（低份额轨道先固定）
  color_mix.py  两色混合：hex/rgb/hsl 解析、sRGB/线性 sRGB/HSL/OKLab/OKLCH
                转换、预乘 alpha 插值、hue 弧线与逐通道裁剪
  gamut_map.py  OKLCH -> sRGB 色域映射：oklch() 解析、色域判定与
                固定 L/h/alpha 的最大色域内 C 二分查找
  cascade.py    层叠排序键与淘汰证据生成
  variables.py  var() 解析替换、自定义属性继承与循环检测
  analysis.py   不可变整树分析内核：RuleSet 一次解析、SolveOptions 变体、
                先序逐节点 NodeAnalysis（层叠胜出 / 候选定位 / computed /
                custom_properties / invalid 含原因 / 继承与 var() 依赖）
                与三入口共享的节点状态签名
  engine.py     /compute 编排（内核求解结果渲染为旧版响应）
  preview.py    样式变更预演：补丁校验 / 基线与补丁后复算（共享内核与签名）/
                留一法归因 / 因果依赖图最短路与 shadowed 判定
  audit.py      样式表清理审计：样本树校验展平、按序贪心试删（内核整树
                重算 + 共享签名比对）、首个冲突节点与阻断原因诊断
  dom_impact.py DOM 变更影响分析：原子变更校验与应用、基线 / 变更后
                整树重算、逐节点共享签名比对与选择器命中 / 层叠 /
                继承 / var() 链归因
tests/test_cascade.py     /compute 端到端测试
tests/test_preview.py     /compute 样式变更预演（patches）端到端测试
tests/test_stylesheet_audit.py /stylesheet-audit 端到端测试
tests/test_dom_impact.py  /dom-impact 端到端测试
tests/test_analysis_kernel.py 整树分析内核一致性端到端测试（嵌套继承、
                          fallback 改道、循环解除、同值换胜出者、非法输入）
tests/test_overlap.py     /selector-overlap 端到端测试
tests/test_keyframe.py    /keyframe-sample 端到端测试
tests/test_counters.py    /counter-render 端到端测试
tests/test_math_eval.py   /math-evaluate 端到端测试
tests/test_grid_tracks.py /grid-tracks 端到端测试
tests/test_color_mix.py   /color-mix 端到端测试
tests/test_gamut_map.py   /gamut-map 端到端测试
examples/request.json          /compute 示例请求
examples/preview.json           /compute 样式变更预演示例请求（携带 patches）
examples/stylesheet-audit.json  /stylesheet-audit 示例请求
examples/dom-impact.json        /dom-impact 示例请求
examples/keyframe-sample.json  /keyframe-sample 示例请求
examples/counter-render.json   /counter-render 示例请求
examples/math-evaluate.json    /math-evaluate 示例请求
examples/grid-tracks.json      /grid-tracks 示例请求
examples/color-mix.json        /color-mix 示例请求
examples/gamut-map.json        /gamut-map 示例请求
Dockerfile / docker-compose.yml
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```
