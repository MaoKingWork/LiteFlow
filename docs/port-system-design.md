# 节点端口系统设计（Port System）

为 AgentKit 的 Step 引入**显式输入/输出端口**：每个节点声明自己的输入与输出端口（变量名 + 类型），既支持 Python 风格的自动类型推断（零配置易用），又支持显式类型契约（高级、稳定）。借鉴 Blender 节点的"显式连线"思想，但落地为 YAML 声明 + Python 类型提示的双层形态。

> 开发阶段设计，**不考虑向后兼容**。`output` 单字段作为单输出端口的语法糖保留，仅为减少冗余，非兼容性目的。

---

## 1. 设计目标与原则

| 目标 | 落地方式 |
|------|----------|
| 易于使用（低下限） | 不声明端口时行为完全同现状；最简写法仅 `output: x` |
| 易于配置 | 端口支持列表 / dict / 单字段多级简写，新手到高级平滑过渡 |
| 易于拓展 | 新增 Step 类型只需实现 `_emit_outputs`，端口校验逻辑集中在 `BaseStep.execute` |
| 高上限 | 显式连线（`from`）、类型契约、多输出拆分、JSON Schema、静态连线检查 |
| 安全 | 类型字符串用 ast 有限解析器（同 `template.py` 安全模型），不调用 `eval`；运行时双重校验 |
| 可靠稳定 | 输入端口执行前校验（required + 类型），输出端口执行后校验（required + 类型），失败即清晰报错 |
| 模块化 | 端口逻辑独立于 `core/ports.py`，仅依赖标准库 + 可选 pydantic，无循环依赖 |
| 无冗余 | 端口名即 Context key；`output` 是 `outputs` 的语法糖；连线信息内嵌端口，不设独立 `links` 段 |

**核心定位**：端口系统是叠加在现有「`{{var}}` 模板 + `output` 写 Context」数据流之上的**声明式契约层**，不替换数据流，只为其增加"端口定义 + 类型校验 + 连线校验"。声明端口即启用契约；不声明即退化为现状。

---

## 2. 核心概念

### 2.1 Port（端口）

端口是 Step 与外界交换数据的具名端点。每个端口有：

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 端口名 = 变量名 = Context key。Step 内通过此名引用输入值，输出值写入此 key |
| `type` | `PortType \| None` | 类型契约。`None` = 不校验，运行时自动推断（Python 鸭子类型） |
| `required` | `bool` | 输入：是否必须有来源值；输出：是否必须产出。默认 `True` |
| `strict` | `bool` | 类型校验是否严格。`True`（默认）拒绝隐式转换（`"5"` 声明 `int` → 报错）；`False` 允许 pydantic 宽容转换。详见 2.3 |
| `default` | `Any` | `required=False` 时的默认值（输入端口） |
| `description` | `str` | 文档说明，供可视化与校验报告使用 |

### 2.2 InputPort / OutputPort

```python
@dataclass
class InputPort(Port):
    from_: str | None = None   # 来源 Context key；None 时等于 name

@dataclass
class OutputPort(Port):
    pass   # 输出端口的 name 即写入的 Context key，无需 from
```

**连线语义**：输入端口的 `from` 指向数据来源（上游 Step 的输出端口 name，或工作流 `inputs` 传入的 key），`name` 是本 Step 内的变量名。`from` 默认等于 `name`——此时端口名与来源同名，最常见的情形零配置。

```yaml
# name == from（默认，零配置）
inputs:
  - name: orders

# name != from（Blender 风格重命名/解耦）
inputs:
  - name: orders        # 本 Step 内叫 orders
    from: fetched_orders  # 从 Context 的 fetched_orders 取
```

### 2.3 PortType（类型表达）

端口类型支持三种表达，对应「自动推断 → 显式类型 → 完整 Schema」的上限阶梯：

| 表达 | 形式 | 适用 |
|------|------|------|
| 不声明 | `type:` 省略 | 自动推断，不校验（低下限） |
| 类型字符串 | `type: list[str]` | Python 风格显式类型（常用） |
| JSON Schema | `schema: {type: object, ...}` | 完整结构契约（高上限） |

#### 类型字符串语法

```
str | int | float | bool | list | dict | any
list[str] | list[dict] | dict[str, int]
str | None | str | int | list[str] | None
```

- 基础类型名映射到 Python 内建类型。
- `[]` 表示参数化容器，`|` 表示联合。
- `any` 等价于不校验。
- **解析方式**：用 ast 有限解析器（白名单节点：`Name` / `Subscript` / `Tuple` / `BinOp(Add=Union)` / `Constant(None)`），禁止属性访问与任意调用，与 `template.py` 的 `eval_expression` 共用同一安全模型。不调用 `eval`。

#### JSON Schema 表达

```yaml
outputs:
  - name: result
    schema:
      type: object
      properties:
        name: {type: string}
        age: {type: integer}
      required: [name]
```

校验复用现有 `_schema_to_model`（`yaml/loader.py`）生成的 pydantic 模型，或直接用 pydantic `TypeAdapter` 从 schema 构造。无 schema 库时退化为"仅校验顶层 type"。

#### 校验与转换

`PortType.validate(value, *, strict=True) -> Any`：
- 类型字符串 / Schema：用 pydantic `TypeAdapter` 校验。
  - `strict=True`（**默认**）：严格模式，拒绝隐式类型转换。`"5"` 声明 `int` → 报错（而非静默转为 `5`）。类型不匹配立即抛 `PortTypeError`，含端口名、期望类型、实际类型与值摘要。符合"失败即清晰报错"——在契约系统中，静默修正数据往往比直接报错更危险。
  - `strict=False`：宽松模式，允许 pydantic 默认的类型强制（`"5"` → `5`）。仅在端口显式声明 `strict: false` 时启用，用于需要容错的场景。
- 未声明类型：原样返回，不校验（自动推断）。

---

## 3. YAML 配置形式

### 3.1 简写阶梯（低下限 → 高上限）

```yaml
# 阶梯 0：完全不声明端口（现状，零下限）
- id: a
  type: llm
  prompt: "{{x}}"
  output: y

# 阶梯 1：output 单字段（语法糖 = 单输出端口）
- id: a
  type: llm
  output: y
# 等价于 outputs: [{name: y}]

# 阶梯 2：输出带类型（dict 简写）
- id: a
  type: llm
  outputs:
    y: str
# 等价于 outputs: [{name: y, type: str}]

# 阶梯 3：输出列表简写
- id: a
  type: llm
  outputs: [y, z]
# 等价于 outputs: [{name: y}, {name: z}]

# 阶梯 4：完整端口声明
- id: a
  type: llm
  outputs:
    - name: y
      type: str
      required: true
      description: 分析结论
```

`inputs` 支持 dict 简写 `{name: type}` 与列表简写 `[name]`，规则同 `outputs`。

### 3.2 完整输入输出端口示例

```yaml
steps:
  - id: fetch
    type: tool
    tool: db.query
    params:
      sql: "SELECT * FROM orders WHERE date='{{date}}'"
    outputs:
      - name: orders
        type: list[dict]
        description: 订单原始记录

  - id: analyze
    type: llm
    agent: analyzer
    inputs:
      - name: orders          # 本 Step 内变量名
        from: orders          # 来源 key（默认等于 name，这里显式写出）
        type: list[dict]
        required: true
      - name: threshold
        from: cfg_threshold
        type: int
        required: false
        default: 10
    outputs:
      - name: analysis
        type: str
      - name: facts
        type: list[dict]
    prompt: |
      订单: {{orders}}
      阈值: {{threshold}}
      请生成摘要，并提取关键事实。

  - id: render
    type: tool
    tool: report.render
    params:
      summary: "{{analysis}}"
      facts: "{{facts}}"      # resolve_value 整体引用，保留 list[dict] 结构
```

### 3.3 字段语义

| 字段 | 适用 | 说明 |
|------|------|------|
| `name` | 输入/输出 | 端口名 = 变量名 = Context key。必填 |
| `from` | 输入 | 来源 Context key，默认等于 `name` |
| `type` | 输入/输出 | 类型字符串。与 `schema` 互斥 |
| `schema` | 输入/输出 | JSON Schema。与 `type` 互斥 |
| `required` | 输入/输出 | 默认 `true`。输入：缺来源值则报错；输出：未产出则报错 |
| `strict` | 输入/输出 | 默认 `true`。`true` 拒绝隐式类型转换；`false` 允许宽容转换（如 `"5"`→`5`） |
| `default` | 输入 | `required: false` 时的默认值 |
| `description` | 输入/输出 | 文档，供可视化与校验报告 |

**Step 级配置**（不属于单个端口，作用于整个 Step 的模板解析行为）：

| 字段 | 默认 | 说明 |
|------|------|------|
| `strict_scope` | `false` | `true` 时封闭输入作用域：模板中引用未在 `inputs` 声明的变量直接抛 `UndefinedError`，切断对全局 Context 的回退。默认 `false` 保持易用（工作流级输入、`{{#each}}` 的 `this`/`index` 等无需声明即可用）；静态校验始终扫描未声明引用并报 warning |

---

## 4. Python SDK

```python
from agentkit.core.ports import InputPort, OutputPort, PortType
from agentkit.steps.llm_step import LLMStep

step = LLMStep(
    id="analyze",
    agent=agent,
    prompt="分析: {{orders}}",
    inputs=[
        InputPort(name="orders", from_="fetched_orders", type=PortType.parse("list[dict]")),
        InputPort(name="threshold", type=PortType.parse("int"), required=False, default=10),
    ],
    outputs=[
        OutputPort(name="analysis", type=PortType.parse("str")),
        OutputPort(name="facts", type=PortType.parse("list[dict]")),
    ],
)
```

`type` 也可直接传 Python 类型对象（`str` / `list[dict]` / `int | None`），`PortType.parse` 同时接受字符串与类型对象。

---

## 5. 运行时数据流

### 5.1 端口绑定（execute 前）

`BaseStep.execute` 在调用 `run` 前，统一完成输入端口绑定：

1. 遍历 `self.inputs`：
   - 按 `from`（默认 `name`）从 `ctx` 取值。
   - `required=True` 且缺失 → 抛 `PortBindingError`，含端口名与来源 key。
   - `required=False` 且缺失 → 用 `default`（未设 `default` 则 `None`）。
   - 声明了 `type` → `PortType.validate(value, strict=port.strict)` 校验；失败抛 `PortTypeError`。默认 `strict=True` 拒绝隐式转换。
2. 构造 `port_bindings: dict[name, value]`，挂到 `self._port_bindings`。

### 5.2 作用域注入（模板解析）

为支持 `name != from` 的重命名，`BaseStep` 提供模板解析辅助方法：

```python
class BaseStep:
    def _render(self, template, ctx):
        """模板解析：优先用端口绑定叠加作用域。"""
        if not self._port_bindings:
            return resolve_value(template, ctx)
        if self.strict_scope:
            # 封闭模式：仅允许端口绑定变量，切断全局 Context 回退
            scope = _ClosedScopeContext(self._port_bindings)
        else:
            scope = _PortScopeContext(ctx, self._port_bindings)
        return resolve_value(template, scope)
```

`_PortScopeContext` 复用 `template.py` 现有 `_ScopeContext` 思路：`get` 优先返回端口绑定值，其余透传父 Context。子类（`LLMStep` / `ToolStep`）将现有 `resolve_template(prompt, ctx)` / `resolve_value(params, ctx)` 改为 `self._render(prompt, ctx)`——**仅一处改动，无冗余**。

- 不声明端口时：`_port_bindings` 为空，`_render` 退化为 `resolve_value`，行为完全同现状。
- 声明端口时：`{{orders}}` 优先取端口绑定值（已校验类型、已按 `from` 取源）。
- `strict_scope: true` 时：`_render` 改用 `_ClosedScopeContext`，仅允许端口绑定中的变量，引用未声明变量直接抛 `UndefinedError`，切断全局回退。适合需要严格隔离的高可靠场景；默认 `false` 保持易用（工作流级输入、`{{#each}}` 的 `this`/`index` 无需声明）。

### 5.3 输出校验（execute 后）

`run` 完成后，`execute` 统一校验输出端口：

1. 遍历 `self.outputs`：
   - `required=True` 且 `ctx` 中无 `name` → 抛 `PortBindingError`。
   - 声明了 `type` → `PortType.validate(ctx.get(name), strict=port.strict)`；失败抛 `PortTypeError`。默认 `strict=True` 拒绝隐式转换。
2. 单输出端口：`run` 内 `ctx.set(name, value)` 即可。
3. 多输出端口：由各 Step 类型的 `_emit_outputs` 负责拆分写入（见第 7 节）。

> SKIP / DEFAULT 兜底（现有 `execute` 逻辑）：只对单输出端口填 `None`；多输出端口不自动填，保持失败语义清晰。

---

## 6. 静态校验（validator 扩展）

`yaml/validator.py` 新增端口相关校验，在编译前发现连线错误：

| 校验项 | 规则 |
|--------|------|
| 端口名唯一 | 同一 Step 的输入端口名互斥；输出端口名互斥 |
| `type` / `schema` 互斥 | 同一端口不可同时声明 |
| 输出端口与 `output` 互斥 | `output` 与 `outputs` 不可同时出现（避免歧义） |
| 输入 `from` 来源存在 | `from` 指向的 key 应为：工作流 `inputs` 声明的 key，或某前序 Step（含同级并行/条件分支可见的）的输出端口名。无法确认时降级为 warning（动态来源可能运行时才写入） |
| 类型兼容性 | 若上游输出端口与下游输入端口都声明了 `type`，检查兼容：`any` 兼容一切；完全相同兼容；`str|int` 兼容 `str`；其余不兼容记 warning（避免误报，运行时再严格校验） |
| 多输出语义 | `llm` + `output_format: text` 仅允许单输出端口；`tool` 多输出端口要求返回 `dict` |
| 模板变量引用扫描 | 扫描 `prompt`/`params` 中的 `{{var}}`，var 未在 `inputs` 声明、非工作流 `inputs`、非前序 Step 输出端口名时记 warning（动态来源可能运行时才写入，避免误报）。`strict_scope: true` 的 Step 升级为 error |
| 并行端口冲突提示 | `parallel` 分支输出端口名重复时，报错明确提示"请为各分支指定不同 name" |

校验报告复用现有 `ValidationReport`（errors / warnings）。

---

## 7. 各 Step 类型的端口语义

端口绑定与校验集中在 `BaseStep.execute`，各子类只需实现"如何把执行结果写入输出端口"。

### 7.1 tool — ToolStep

- 输入：`params` 仍用 `{{var}}` 模板，经 `self._render` 解析（支持端口作用域）。
- 输出：
  - 单输出端口（含 `output` 简写）：`call` 返回值整体写入。
  - 多输出端口：`call` 必须返回 `dict`，按端口 `name` 取字段分别写入；缺失的 `required` 端口报错，非 required 端口填 `None`。
  - 聚合语义：单输出端口 = 完整返回值写入一个 key，下游可用 `{{result.field}}` 点号路径访问字段；多输出端口 = 拆分。二者择一，不自动叠加。

```yaml
- id: fetch
  type: tool
  tool: db.query
  params: {sql: "..."}
  outputs:
    - name: rows
      type: list[dict]
    - name: count
      type: int
# Tool 返回 {"rows": [...], "count": 42} → 自动拆分
```

### 7.2 llm — LLMStep

- 输入：`prompt` 用 `{{var}}`，经 `self._render` 解析。
- 输出：
  - `output_format: text`：仅允许单输出端口，整段文本写入。
  - `output_format: json`：
    - 单输出端口：整个 JSON 值写入（现状）。
    - 多输出端口：JSON 须为 `dict`，按端口 `name` 取字段分别写入；缺失的 required 端口报错。
  - `agent.output_model`（pydantic）：仍优先，解析为模型实例后写入单输出端口；多输出端口时按模型字段名拆分。

```yaml
- id: analyze
  type: llm
  agent: analyzer
  output_format: json
  outputs:
    - name: summary
      type: str
    - name: keywords
      type: list[str]
  prompt: "返回 JSON: {summary, keywords}"
# LLM 返回 {"summary": "...", "keywords": [...]} → 自动拆分到两端口
```

### 7.3 condition / loop / parallel — 容器型

- 容器型 Step 本身通常不声明端口，其 `output`（若有）汇总子步骤结果。
- 子步骤的端口照常生效，校验递归进行。
- `parallel` 的 `branches` 各自的输出端口写入同一 Context，端口名须唯一。复用同一 Step 类型时，通过实例级 `outputs` 起不同 `name` 区分（端口名是实例配置，非类型固定）：

```yaml
branches:
  - id: fetch_a
    type: tool
    tool: http.get
    outputs: [{name: resp_a}]   # 实例级命名，避免冲突
  - id: fetch_b
    type: tool
    tool: http.get
    outputs: [{name: resp_b}]
```

  冲突时校验报错明确提示"同级 output key 重复，请为各分支指定不同 name"。不引入自动前缀等魔法行为——用户声明什么 name 就写入什么 key。

### 7.4 skill — SkillStep

- 输入：`prompt` 用 `{{var}}`，经 `self._render` 解析。
- 输出：同 `llm`。Skill 的 `output_model` 优先级最高。

### 7.5 自定义 Step

继承 `BaseStep`，在 `run` 内 `ctx.set(port_name, value)` 即可。输出端口校验由 `execute` 自动完成。多输出时实现 `_emit_outputs` 可选：

```python
@register_step("split")
class SplitStep(BaseStep):
    type = "split"
    async def run(self, ctx):
        data = self._render("{{source}}", ctx)   # 用 _render 享端口作用域
        ctx.set("head", data[:10])
        ctx.set("tail", data[10:])
        return ctx
```

---

## 8. 模块结构

```
agentkit/
├── core/
│   ├── ports.py            # 【新】Port / InputPort / OutputPort / PortType / 异常
│   └── ...
├── steps/
│   ├── base.py             # 改：集成端口绑定 + 输出校验 + _render
│   ├── tool_step.py        # 改：_render 替换 resolve_value；多输出拆分
│   ├── llm_step.py         # 改：_render 替换 resolve_template；多输出拆分
│   ├── skill_step.py       # 改：同 llm
│   └── ...
├── yaml/
│   ├── loader.py           # 改：编译 inputs/outputs 端口声明
│   └── validator.py        # 改：端口静态校验
└── ...
```

### 8.1 `core/ports.py` 关键 API

```python
@dataclass
class Port:
    name: str
    type: PortType | None = None
    required: bool = True
    strict: bool = True              # 默认严格：拒绝隐式类型转换
    default: Any = MISSING
    description: str = ""

@dataclass
class InputPort(Port):
    from_: str | None = None      # None → 等于 name

@dataclass
class OutputPort(Port):
    pass

class PortType:
    """端口类型：封装 pydantic TypeAdapter，支持字符串/Schema/类型对象。"""
    @classmethod
    def parse(cls, spec: str | type | dict) -> "PortType": ...
    def validate(self, value: Any, *, strict: bool = True) -> Any: ...
    def is_compatible(self, other: "PortType") -> bool: ...
    def __repr__(self) -> str: ...

class PortBindingError(Exception): ...   # 缺失/未产出
class PortTypeError(Exception): ...       # 类型校验失败
```

### 8.2 `BaseStep` 改动点

```python
class BaseStep:
    def __init__(self, *, inputs=None, outputs=None, output=None,
                 strict_scope=False, ...):
        self.inputs: list[InputPort] = inputs or []
        self.outputs: list[OutputPort] = outputs or []
        if output and not self.outputs:        # output 语法糖
            self.outputs = [OutputPort(name=output)]
        elif output and self.outputs:
            raise ValueError("output 与 outputs 不可同时声明")
        self.strict_scope: bool = strict_scope  # True 时封闭输入作用域
        self._port_bindings: dict[str, Any] = {}

    async def execute(self, ctx, hooks, *, retry_policy):
        self._bind_inputs(ctx)                 # 新：输入校验/绑定
        ...                                     # 现有 run + 重试逻辑
        self._validate_outputs(ctx)            # 新：输出校验
        ...

    def _render(self, template, ctx): ...       # 新：端口作用域模板解析
    def _bind_inputs(self, ctx): ...
    def _validate_outputs(self, ctx): ...
    def _emit_outputs(self, ctx, result): ...   # 子类重写：多输出拆分
```

### 8.3 `yaml/loader.py` 编译

```python
def _compile_ports(spec) -> list[InputPort] | list[OutputPort]:
    """把 YAML 端口声明编译为 InputPort/OutputPort 列表。

    支持简写：
      [a, b]                 → [{name: a}, {name: b}]
      {a: str, b: int}       → [{name: a, type: str}, {name: b, type: int}]
      [{name: a, type: str}] → 原样
    """
```

`_compile_step` 各分支新增 `inputs=_compile_ports(step_dict.get("inputs"))`、`outputs=_compile_ports(step_dict.get("outputs"))`。

---

## 9. 设计决策与权衡

| 决策 | 选择 | 理由 |
|------|------|------|
| 端口系统定位 | 契约叠加层，非数据流重写 | 现有 `{{var}}` + Context 数据流已成熟稳定；端口为其加契约，不破坏。低下限 |
| 连线表达 | 端口内嵌 `from`，无独立 `links` 段 | 无冗余；连线信息与端口定义合一；YAML 更紧凑 |
| 端口名与 Context key 关系 | 端口名 = Context key | 零冗余；`from` 提供重命名能力满足解耦需求 |
| `output` 字段去留 | 保留为 `outputs` 语法糖 | 减少单输出场景冗余；非兼容性目的（开发阶段本可删，但保留更简洁） |
| 类型校验失败策略 | 默认抛错终止 | 安全可靠稳定；可通过 `required: false` + `default` 显式容错 |
| 类型字符串解析 | ast 有限解析器 | 复用 `template.py` 安全模型，不引入 `eval` |
| 自动推断 vs 显式 | `type` 省略即自动推断 | Python 风格易用；显式 `type` 供高级场景；两者同一端口无缝切换 |
| 多输出拆分 | 按 JSON/dict 字段名映射端口名 | 与 LLM JSON 输出、Tool dict 返回天然契合，无额外映射配置 |
| 静态 `from` 来源检查 | 确认存在则 OK，不确定降级 warning | 动态写入（loop/parallel/condition 分支）可能静态无法确认，避免误报 |
| 类型校验严格度 | 默认 `strict: true`，拒绝隐式转换 | 契约系统要求"失败即清晰报错"；静默修正数据比报错更危险。需容错时显式 `strict: false` |
| 作用域封闭 | 默认不封闭 + 静态扫描 warning；可选 `strict_scope: true` 运行时封闭 | 完全封闭会强制声明所有变量（含工作流输入、`{{#each}}` 的 `this`/`index`），配置冗余骤增；静态扫描已覆盖大部分幽灵依赖 |
| 并行端口冲突 | 保持手动命名，不自动加前缀 | 端口名是实例配置非类型固定；自动前缀引入魔法行为违背显式原则；冲突时清晰报错提示 |
| 多输出原子性 | 单输出=聚合，多输出=拆分，不引入额外机制 | 单输出+点号路径 `{{result.field}}` 已满足聚合访问；"既要又要"属少数，YAGNI |

---

## 10. 完整示例

```yaml
name: order_report
inputs:
  - date

agents:
  - name: analyzer
    model: deepseek
    system: "你是数据分析助手"
    output_model:
      type: object
      properties:
        summary: {type: string}
        risks: {type: array, items: {type: string}}
      required: [summary]

steps:
  - id: fetch
    type: tool
    tool: db.query
    params:
      sql: "SELECT * FROM orders WHERE date='{{date}}'"
    outputs:
      - name: orders
        type: list[dict]

  - id: analyze
    type: llm
    agent: analyzer
    inputs:
      - name: orders
        type: list[dict]
      - name: date
        type: str
    outputs:
      - name: summary
        type: str
      - name: risks
        type: list[str]
        required: false
    output_format: json
    prompt: |
      日期: {{date}}
      订单: {{orders}}
      返回 JSON: {summary, risks}

  - id: notify
    type: tool
    tool: sink.wecom
    params:
      content: "摘要: {{summary}}; 风险: {{risks}}"
    outputs:
      - name: notify_result
```

校验链路：
- 静态：`fetch.orders` → `analyze.inputs[orders]`（类型 `list[dict]` 兼容）；`analyze.summary` → `notify.params.content` 引用存在。
- 运行时输入：`analyze` 执行前校验 `orders` 存在且为 `list[dict]`，`date` 存在且为 `str`。
- 运行时输出：`analyze` 执行后校验 `summary` 存在且为 `str`；`risks` 若 LLM 未返回则填 `None`（`required: false`）。

---

## 11. 安全性

- **类型字符串解析**：ast 白名单解析器，仅放行 `Name` / `Subscript` / `Tuple` / `BinOp` / `Constant(None)`，禁止属性访问与任意调用。与 `template.py` 共用安全模型。
- **JSON Schema**：用 pydantic 构造模型校验，不执行任意代码。
- **运行时校验**：输入端口在 `run` 前强制校验，杜绝"上游类型漂移导致下游隐蔽 bug"。
- **严格类型模式**：`strict: true`（默认）拒绝隐式转换，类型不匹配立即报错，避免静默修正导致语义漂移。
- **作用域封闭**：`strict_scope: true` 时切断全局 Context 回退，消除幽灵依赖；默认配合静态扫描 warning 覆盖大部分场景。
- **不可变 Context**：端口绑定值取自 Context 只读视图，`_render` 通过 `_PortScopeContext` / `_ClosedScopeContext` 叠加作用域，不修改 Context。
- **校验失败即停**：默认抛错，避免错误数据沿管线传播。

---

## 12. 落地步骤建议

1. `core/ports.py`：实现 `Port` / `InputPort` / `OutputPort` / `PortType` / 异常，含类型字符串 ast 解析与 pydantic 校验。单元测试覆盖类型解析与校验。
2. `steps/base.py`：`__init__` 接收端口；`execute` 集成 `_bind_inputs` / `_validate_outputs`；新增 `_render` 与 `_PortScopeContext`。
3. `steps/tool_step.py` / `llm_step.py` / `skill_step.py`：`resolve_value`/`resolve_template` 替换为 `self._render`；实现 `_emit_outputs` 多输出拆分。
4. `yaml/loader.py`：`_compile_ports` + 各分支注入端口。
5. `yaml/validator.py`：端口静态校验。
6. 更新 `README.md`：新增「端口系统」章节，字段表与示例。
7. 测试：端口绑定、类型校验、多输出拆分、静态连线检查、零端口退化。
