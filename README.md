# LiteFlow

> 轻量、简洁，易于维护和扩展。使用简单便捷，但不限制上限。

LiteFlow 是一个智能体工作流与报告生成平台，包含两个核心组件：

- **AgentKit** — 轻量化智能体框架，采用双层架构（YAML 配置层 + Python SDK 层），围绕 8 个核心概念构建：Agent / Tool / Skill / MCP / Step / Context / Workflow / Hooks
- **Report Engine SDK** — 协议无关、配置驱动的 Markdown 报告生成引擎，通过 Pack 组织模板与规则，支持多角色多视图输出，内置 AgentKit / MCP / LangChain / FastAPI 适配器

两个组件可独立使用，也可通过适配器深度集成：AgentKit 工作流中的 ToolStep / LLMStep 可直接调用 Report Engine 生成报告，产物经运行时层自动落盘并推送事件。

## 环境要求与安装

环境要求：Python >= 3.14，使用 [uv](https://docs.astral.sh/uv/) 管理依赖。

```bash
# 安装依赖（含 agentkit 核心）
uv sync
```

可选依赖按需安装：

```bash
uv sync --extra doc-convert    # Markdown → DOCX / PDF / HTML 转换（reportlab / python-docx）
uv sync --extra server         # 可视化服务（FastAPI + uvicorn + SSE）
uv sync --extra dev            # pytest + pytest-asyncio + httpx（开发测试）
```

Report Engine SDK 作为子包随主项目安装，其独立可选依赖：

```bash
uv sync --extra doc-convert    # 含 report-engine-sdk 的 agentkit 适配器（pydantic）
# report-engine-sdk 还支持 mcp / langchain / api / s3 可选依赖，按需在 pyproject.toml 中添加
```

## 核心特性

### AgentKit

- **双层架构**：YAML 声明式配置 + Python SDK，按需选择抽象层级
- **6 种 Step 类型**：tool / llm / condition / loop / parallel / skill，覆盖常见编排场景
- **声明式输出解析**：LLMStep 支持 `output_format: text | json`，配合 `output_model` 实现"文本/JSON/Schema 校验"三级解析，复用解析失败→修复重试→降级模型完整保障链
- **端口系统**：Step 声明式输入/输出端口（变量名 + 类型契约），支持 Python 风格自动类型推断与显式类型校验（`strict` 默认拒绝隐式转换）、多输出自动拆分、作用域封闭（`strict_scope`）、静态连线校验。不声明端口时行为不变
- **流式输出**：LLMStep `stream: true` 启用 SSE 流式，通过 `on_llm_stream_*` 钩子实时推送文本增量；retry/降级模型通过 `attempt` 参数标识，前端据此重置缓冲。流式与契约链正交，解析/重试/降级语义不变
- **不可变 Context 与生态互操作**：`FrozenDict` 继承 `collections.abc.Mapping`，与 jsonschema / requests / ORM 等检查 Mapping 的库零摩擦集成；`to_mutable()` 提供递归解冻入口
- **模板引擎**：`{{var}}` 变量替换、`${ENV}` 环境变量、`{{#if}}`/`{{#each}}` 条件与循环
- **可观测性开箱即用**：自动装配日志与 Token 计量 Hooks，Step 级耗时与失败链可视化
- **LLM 客户端生命周期托管**：`async with` 自动关闭连接，避免泄漏
- **Skill 能力包**：系统提示词 + 输出契约 + 专属工具的封装单元，支持多 Skill 合并
- **MCP 自动发现**：连接 MCP Server 后自动注册其工具为框架一等公民
- **多 LLM 提供商**：内置 DeepSeek 与 MiMo 预设，兼容所有 OpenAI Chat Completions API
- **可视化服务**：内置 FastAPI + SSE 服务，支持工作流管理、运行控制、产物浏览与实时事件推送

### Report Engine SDK

- **Pack 驱动**：每个 Pack 自包含 `pack.json` + 模板 + 规则 + 变量，可独立拥有与版本化
- **两步流水线**：`evaluate`（校验 + 规则计算）→ `render`（模板渲染），也支持直接渲染跳过计算
- **多角色多视图**：通过 `view` 参数切换报告视角（如管理者视图 / 教师视图），单模板多角色输出
- **声明式规则引擎**：`rules` 管线支持声明式公式 + 命令式插件，`{"ref": ...}` 引用 Pack 共享规则
- **多适配器**：AgentKit Tool / MCP Server / LangChain Tool / FastAPI Router，协议无关核心 + 可选框架集成
- **多存储后端**：Local / Memory / S3，通过 `StorageBackend` 抽象统一接口

## 快速开始

### 方式一：YAML 声明式

编写 `workflow.yaml`：

```yaml
name: daily_report
inputs:
  - date

agents:
  - name: analyzer
    model: gpt-4o-mini
    system: |
      你是数据分析助手。根据提供的数据库查询结果，生成简洁的中文日报摘要。
    temperature: 0.2

steps:
  - id: fetch_data
    type: tool
    tool: db.query
    params:
      sql: "SELECT * FROM orders WHERE date='{{date}}'"
    output: orders_raw

  - id: analyze
    type: llm
    agent: analyzer
    prompt: |
      日期: {{date}}
      订单数据: {{orders_raw}}
      请生成日报摘要。
    output: analysis

  - id: notify
    type: tool
    tool: sink.wecom
    params:
      content: "{{analysis}}"
      webhook: "${WECOM_WEBHOOK_URL}"
    output: notify_result
```

运行：

```bash
agentkit run workflow.yaml --input date=2024-01-01
```

或在 Python 中加载并运行：

```python
import asyncio
from agentkit.yaml import load_workflow

async def main():
    wf = load_workflow("workflow.yaml")
    result = await wf.run(inputs={"date": "2024-01-01"})
    print(result.status)           # "completed"
    print(result.completed_steps)  # ["fetch_data", "analyze", "notify"]
    print(result.context.get("analysis"))

asyncio.run(main())
```

### 方式二：Python SDK

```python
import asyncio
from agentkit.core.workflow import Workflow
from agentkit.steps.tool_step import ToolStep
from agentkit.steps.llm_step import LLMStep
from agentkit.core.agent import AgentConfig

async def main():
    agent = AgentConfig(
        name="analyzer",
        model="gpt-4o-mini",
        system="你是数据分析助手。",
    )

    wf = Workflow(
        name="demo",
        steps=[
            ToolStep(id="fetch", tool="db.query", output="raw"),
            LLMStep(id="analyze", agent=agent, prompt="分析: {{raw}}", output="result"),
        ],
    )

    async with wf:
        result = await wf.run(inputs={"raw": "示例数据"})
        print(result.context.get("result"))

asyncio.run(main())
```

### 方式三：Report Engine SDK

```python
from report_engine_sdk import ReportEngine, LocalStorage

# 构建 Engine：配置目录 + 存储后端
engine = ReportEngine("src/report_engine_sdk/config", LocalStorage("./output"))

# evaluate → render 两步流水线
result = engine.evaluate("ops_report:health_check", {
    "service_name": "api-gateway",
    "uptime_pct": 99.95,
    "error_count": 3,
    "latency_ms": 45.2,
    "date_str": "2026-07-23",
})

if result.success:
    rendered = engine.render("ops_report:health_check", result.data, view="default")
    print(rendered.content)   # Markdown 报告
    print(rendered.uri)       # file:// URI
```

AgentKit 与 Report Engine SDK 深度集成示例见 `examples/run_report_demo.py`，展示 ToolStep 声明式调用报告生成、产物自动落盘与事件推送完整链路。

## 核心概念

| 概念 | 职责 | 核心类 |
|------|------|--------|
| **Agent** | 提示词 + 模型 + 输出契约 + 工具集 + 重试策略的配置载体 | `AgentConfig` |
| **Tool** | 统一工具接口，分 Source / Action / Sink 三种语义角色 | `Tool` |
| **Skill** | 能力包：系统提示词 + 输出模型 + 专属工具的封装 | `SkillManifest` |
| **MCP** | 外部 MCP Server 连接管理与工具自动发现注册 | `MCPManager` |
| **Step** | 执行单元，6 种类型，含钩子/超时/重试/trace 编排 | `BaseStep` |
| **Context** | 不可变工作流共享状态（`FrozenDict` 兼容 `Mapping`），支持快照/恢复与 trace 记录 | `Context` |
| **Workflow** | 编排器，管理 Step 序列执行、检查点、资源生命周期 | `Workflow` |
| **Hooks** | 生命周期钩子，提供日志、Token 计量等可观测性 | `LifecycleHooks` |

## Step 类型详解

所有 Step 继承 `BaseStep`，实现 `run(ctx)` 方法。通用编排（钩子触发、超时、执行级重试、trace 记录）集中在 `BaseStep.execute()`，子类无需重写。

### tool — 工具调用

执行已注册的 Tool，参数支持模板渲染。

```yaml
- id: fetch_data
  type: tool
  tool: db.query
  params:
    sql: "SELECT * FROM orders WHERE date='{{date}}'"
  output: orders_raw
```

### llm — LLM 调用

调用 LLM 生成文本，支持 Function Call 多轮对话、声明式输出解析与流式输出。

```yaml
- id: analyze
  type: llm
  agent: analyzer
  prompt: |
    日期: {{date}}
    数据: {{orders_raw}}
    请生成摘要。
  output: analysis
  # output_format: text | json   # 默认 text
  # stream: true                 # 启用流式输出（默认 false）
```

**输出解析优先级**（高→低）：

| 配置 | 解析器 | 输出类型 | 适用场景 |
|------|--------|----------|----------|
| `agent.output_model` | `PydanticParser` | pydantic 模型实例 | 严格 schema 校验，定义在 agent 段 |
| `output_format: json` | `JSONParser` | `dict` / `list` / 标量 | 需 JSON 但无 schema 约束 |
| `output_format: text`（默认） | `TextParser` | `str` | 自由文本 |

三级解析器均复用 LLMStep 输出契约保障链：解析失败→生成 retry_hint→重试→降级模型→`on_exhausted` 决策。**流式不改变契约链语义**——流式期间累积完整文本，解析/重试/降级仍按完整文本执行。

```yaml
# JSON 输出示例：下游 Step 通过 {{facts}} 直接拿到 dict
- id: structure
  type: llm
  agent: structurer
  prompt: "把 {{raw}} 整理为 JSON"
  output: facts
  output_format: json

- id: render
  type: tool
  tool: report.render
  params:
    data: "{{facts}}"    # 直接传 dict，无需中转 json_parse
```

> **字段说明**：`prompt` 为规范字段名。旧版使用的 `input` 字段仍向后兼容，但会触发 `DeprecationWarning`，建议迁移至 `prompt`。流式输出详见 [流式输出](#流式输出) 章节。

### condition — 条件分支

求值 `when` 表达式，为真执行 `then` 子步骤，为假执行 `else` 子步骤。子步骤执行完毕后自动汇合到同级下一步。

```yaml
- id: check_data
  type: condition
  when: "len(orders_raw) > 0"
  then:
    - id: has_data
      type: llm
      agent: analyzer
      prompt: "数据量: {{orders_raw}}"
      output: result
  else:
    - id: no_data
      type: tool
      tool: sink.notify
      params:
        message: "无数据"
      output: result
```

`when` 表达式支持 `{{var}}` 变量替换与比较/布尔/算术运算，经 AST 安全求值（不调用 `eval()`）。多路分支用 condition 链实现（上一个 condition 的 `else` 中放下一个 condition）。

> **引号约定**：`{{var}}` 在表达式求值时会被替换为变量的 Python 字面量（字符串自动加 `repr` 引号），因此**不要**在 `{{var}}` 外面再加引号。写 `'{{status}}' == 'ok'` 会导致双重引号并抛 `TemplateError`。正确写法是 `{{status}} == 'ok'`。

### loop — 循环

两种模式：迭代列表（`iter`）遍历元素执行内部 Step；条件重试（`until`）重复执行直到表达式为真。

```yaml
# 迭代列表
- id: batch_process
  type: loop
  iter: "{{data_list}}"
  as: item
  max: 100
  step:
    id: transform
    type: tool
    tool: transformer
    params:
      input: "{{item}}"
    output: results

# 条件重试
- id: retry_until_valid
  type: loop
  until: "{{validation_pass}} == 'true'"
  max: 3
  on_max: fail
  step:
    id: regenerate
    type: llm
    agent: generator
    prompt: "重新生成: {{draft}}"
    output: draft
```

| 字段 | 说明 |
|------|------|
| `iter` | 迭代模式：`{{var}}` 模板，解析为列表遍历 |
| `as` | 迭代模式：当前元素变量名（默认 `item`） |
| `until` | 条件重试模式：表达式为真时停止 |
| `step` | 循环体（单个 Step，非列表） |
| `max` | 最大迭代次数（默认 100，防死循环） |
| `on_max` | 达到上限策略：`fail`（抛异常）/ `continue`（跳过） |

### parallel — 并行

并发执行多个子 Step（`branches`），受 `max_concurrency` 与 `timeout` 双重保护。结果按各自 `output` 分别写入上下文。

```yaml
- id: parallel_fetch
  type: parallel
  max_concurrency: 5
  on_error: fail_fast
  branches:
    - id: fetch_a
      type: tool
      tool: db.query
      params:
        sql: "SELECT * FROM table_a"
      output: data_a
    - id: fetch_b
      type: tool
      tool: db.query
      params:
        sql: "SELECT * FROM table_b"
      output: data_b
```

| 字段 | 说明 |
|------|------|
| `branches` | 并行执行的子步骤列表 |
| `max_concurrency` | 最大并发数（默认 5） |
| `on_error` | 错误策略：`fail_fast`（首个失败即取消所有）/ `collect_all`（等待全部完成后统一报错） |
| `timeout` | 整体超时秒数（默认 60） |

### skill — Skill 调用

加载指定 Skill 的配置（系统提示词、输出契约、工具），合并后构造 Agent 执行 LLM 调用。

```yaml
- id: search_and_summarize
  type: skill
  skill: web_search
  prompt: "搜索最新 AI 框架并总结"
  output: search_result
```

## 模板引擎

模板引擎支持变量替换、环境变量、条件与循环，用于 Step 的 `prompt`、`params` 等字符串字段。

### 变量替换

```
{{variable}}        # 顶层变量
{{a.b.c}}           # 嵌套路径
{{items[0]}}        # 列表索引
```

### 环境变量

```
${API_KEY}          # 读取环境变量
${WECOM_WEBHOOK}    # 运行时从 os.environ 取值
```

### 条件块

```
{{#if data.truncated}}
内容已被截断
{{/if}}
```

`if` 接受路径表达式（非布尔表达式），按 truthy 判断。复杂布尔逻辑请使用 `condition` Step。

### 条件块 with else

```
{{#if data.available}}
数据可用: {{data.content}}
{{#else}}
数据不可用
{{/if}}
```

### 循环块

```
{{#each items}}
- [{{index}}] {{this.name}}
{{/each}}
```

循环体内可用 `{{this}}`（当前元素）和 `{{index}}`（当前索引，从 0 计）。

### 安全性

模板引擎使用 AST 解析进行表达式求值，不调用 `eval()`，避免代码注入风险。

## 端口系统

端口系统是叠加在现有 `{{var}}` + Context 数据流之上的**声明式契约层**：为 Step 增加显式输入/输出端口（变量名 + 类型），既支持 Python 风格自动类型推断（零配置），又支持显式类型契约（高级、稳定）。不声明端口时行为完全同现状（低下限）；声明端口即启用契约（高上限）。

### 简写阶梯

从零配置到完整声明，平滑过渡：

```yaml
# 阶梯 0：不声明端口（现状，零下限）
- id: a
  type: llm
  prompt: "{{x}}"
  output: y

# 阶梯 1：output 单字段（语法糖 = 单输出端口）
- id: a
  type: llm
  output: y

# 阶梯 2：dict 简写带类型
- id: a
  type: llm
  outputs: {y: str}

# 阶梯 3：列表简写
- id: a
  type: llm
  outputs: [y, z]

# 阶梯 4：完整端口声明
- id: a
  type: llm
  outputs:
    - name: y
      type: str
      required: true
      strict: true
      description: 分析结论
```

`inputs` 支持 dict 简写 `{name: type}` 与列表简写 `[name]`，规则同 `outputs`。

### 输入端口与连线

输入端口的 `from` 指定数据来源（上游输出端口名或工作流输入），`name` 是本 Step 内的变量名。`from` 默认等于 `name`——最常见情形零配置：

```yaml
steps:
  - id: fetch
    type: tool
    tool: db.query
    outputs:
      - name: orders
        type: list[dict]

  - id: analyze
    type: llm
    agent: analyzer
    inputs:
      - name: orders        # 本 Step 内变量名（默认 from=orders）
        type: list[dict]
      - name: threshold      # 重命名：本 Step 内叫 threshold
        from: cfg_threshold  # 从 Context 的 cfg_threshold 取
        type: int
        required: false
        default: 10
    outputs:
      - name: summary
        type: str
    prompt: |
      订单: {{orders}}
      阈值: {{threshold}}
```

### 类型表达

| 表达 | 形式 | 适用 |
|------|------|------|
| 不声明 | `type:` 省略 | 自动推断，不校验 |
| 类型字符串 | `type: list[str]` | Python 风格显式类型 |
| JSON Schema | `schema: {type: object, ...}` | 完整结构契约 |

类型字符串支持：`str | int | float | bool | list | dict | any`、`list[str]`、`dict[str, int]`、`str | None`。用 AST 白名单解析器（不调 `eval`）。

### 严格类型模式

`strict: true`（**默认**）拒绝隐式类型转换：`"5"` 声明 `int` → 报错而非静默转为 `5`。在契约系统中，静默修正数据比报错更危险。需容错时显式 `strict: false`。

### 作用域封闭

`strict_scope: true`（Step 级，默认 `false`）：封闭输入作用域，模板中引用未在 `inputs` 声明的变量直接抛 `UndefinedError`，切断对全局 Context 的回退。默认 `false` 保持易用（工作流级输入、`{{#each}}` 的 `this`/`index` 无需声明）；静态校验始终扫描未声明引用并报 warning。

### 多输出拆分

声明多个输出端口时，Tool 的 dict 返回 / LLM 的 JSON 输出按端口名自动拆分：

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

单输出端口保留完整聚合对象，下游可用 `{{result.field}}` 点号路径访问字段。

### 静态校验

`validate_workflow` 在编译前检查：端口名唯一、`type`/`schema` 互斥、`output`/`outputs` 互斥、`from` 来源存在（不确定时 warning）、模板变量引用扫描（幽灵依赖 warning，`strict_scope` 时 error）、并行端口冲突提示。

### 字段参考

| 字段 | 适用 | 默认 | 说明 |
|------|------|------|------|
| `name` | 输入/输出 | — | 端口名 = 变量名 = Context key |
| `from` | 输入 | =name | 来源 Context key |
| `type` | 输入/输出 | — | 类型字符串，与 `schema` 互斥 |
| `schema` | 输入/输出 | — | JSON Schema，与 `type` 互斥 |
| `required` | 输入/输出 | `true` | 输入缺来源/输出未产出时报错 |
| `strict` | 输入/输出 | `true` | 拒绝隐式类型转换 |
| `default` | 输入 | — | `required: false` 时的默认值 |
| `description` | 输入/输出 | — | 文档说明 |
| `strict_scope` | Step 级 | `false` | 封闭输入作用域 |

## Context 与互操作

Context 采用"不可变写入 + 只读读取"策略根治引用污染：`set` 时递归冻结（`dict→FrozenDict`、`list→tuple`、`set→frozenset`、任意对象→`ReadOnlyProxy`），`get` 返回只读视图，零拷贝。

### 与第三方库集成

`FrozenDict` 继承 `collections.abc.Mapping`，所有检查 `isinstance(x, Mapping)` 的库直接可用：

```python
data = ctx.get("payload")   # FrozenDict

# 检查 Mapping 的库直接接受
requests.post(url, json=data)              # ✓ requests 检查 Mapping
Template("{{name}}").render(data)          # ✓ jinja2 接受 Mapping
```

### to_mutable 递归解冻

需要把 Context 数据传给**检查 `dict` / `list` 具体类型**（而非 `Mapping`）的库时，用 `to_mutable`：

```python
from agentkit.core.context import to_mutable

data = ctx.get("payload")        # FrozenDict，内嵌 tuple
mutable = to_mutable(data)       # dict，且 tuple → list 恢复 JSON array 语义

# jsonschema 用 isinstance(x, dict) / isinstance(x, list) 严格类型检查
from jsonschema import Draft7Validator
Draft7Validator({
    "type": "object",
    "properties": {"tags": {"type": "array"}}
}).validate(mutable)             # ✓
```

与 `copy.deepcopy` 的区别：`deepcopy(FrozenDict)` 返回 dict 但 `tuple` 仍是 tuple，jsonschema 的 `type: "array"` 不认；`to_mutable` 则把 tuple 转为 list。

### 修改 Context 数据

Step 需要修改 Context 数据时，`deepcopy` 后 `set` 回去（会被再次冻结）：

```python
import copy
data = copy.deepcopy(ctx.get("data"))   # 解冻为可变 dict
data["new_key"] = "new_value"
ctx.set("data", data)                    # 自动再次冻结
```

## 流式输出

LLMStep 通过 `stream: true` 启用 SSE 流式输出。流式按 OpenAI Chat Completions `stream` 协议接收增量，客户端在流过程中累积 `tool_calls` 分片（按 `index` 拼接 `arguments` JSON），**仅在流末尾**一次性交付完整 `tool_calls`，调用方无需做分片合并。

### 设计原则

- **流式与契约链正交**：流式 = 观测（hook 推送增量），契约链 = 正确性（解析/重试/降级）。两者互不干扰，解析仍按完整文本执行。
- **Function Call 每轮流式**：因"最终答案轮"只能事后判定，Function Call 循环中每一轮 LLM 调用都流式，前端看到完整生成过程。
- **`attempt` 标识重试/降级**：解析失败重试、降级模型再试时，hook 携带递增的 `attempt` 参数，前端据此重置缓冲，避免拼接上一次失败的废文本。
- **`ChatChunk` 不暴露 `delta_reasoning_content`**：DeepSeek 等模型的思考链是模型内部状态，暴露给前端存在 prompt injection 风险；该需求属少数派，未来可通过新增字段与 hook 扩展。

### 启用方式

YAML：

```yaml
- id: chat
  type: llm
  agent: assistant
  prompt: "{{question}}"
  output: answer
  stream: true
```

Python SDK：

```python
LLMStep(id="chat", agent=agent, prompt="{{question}}", output="answer", stream=True)
```

`stream` 默认 `false`，非流式行为完全保留。`stream` 字段类型校验由 `yaml.validator` 强制为 bool。

### 流式 Hook 生命周期

每次流式 LLM 调用触发完整一轮：

```
on_llm_stream_start(attempt=N)
  → on_llm_stream_delta(delta=..., accumulated=..., attempt=N)  × N 次
  → on_llm_stream_end(full_content=..., attempt=N)
```

`attempt` 取值：`0` = 首次调用，`1+` = 解析失败后的重试或降级模型。前端在 `attempt` 递增时**必须重置缓冲**。

### 消费流式增量

实现自定义 Hook 推送到 SSE / WebSocket / 终端：

```python
from agentkit.core.hooks import LifecycleHooks

class SSEStreamHooks(LifecycleHooks):
    def __init__(self, sse_queue):
        self.sse_queue = sse_queue

    async def on_llm_stream_start(self, step, agent, *, attempt=0):
        # attempt > 0 时通知前端重置缓冲
        await self.sse_queue.put({"event": "reset", "attempt": attempt})

    async def on_llm_stream_delta(self, step, agent, delta, accumulated, *, attempt=0):
        await self.sse_queue.put({"event": "delta", "text": delta})

    async def on_llm_stream_end(self, step, agent, full_content, *, attempt=0):
        if attempt == 0:
            await self.sse_queue.put({"event": "done", "text": full_content})

hooks = SSEStreamHooks(my_queue)
wf = Workflow(name="chat", steps=[...], hooks=hooks)
```

### 客户端实现

| 客户端 | 流式支持 |
|--------|----------|
| `OpenAIClient` | 真流式：解析 SSE 行，按 `index` 累积 `tool_calls`，末尾交付完整列表 |
| `DeepSeekClient` | 继承 `OpenAIClient`，覆盖 `_build_body` 注入 thinking / reasoning_effort / json_output |
| `MiMoClient` | 继承 `OpenAIClient`，组合 `thinking.py` 叠加思考模式，支持图片/音频/视频多模态 |
| `MockClient` | 切片流式：`stream_chunk_size > 0` 时按字节切片模拟增量，否则一次性 yield |
| 自定义 `LLMClient` 子类 | 不实现 `chat_stream` 时自动退化为单次 `chat` 调用，零成本满足接口契约 |

`ChatChunk` 字段：

| 字段 | 出现时机 | 含义 |
|------|----------|------|
| `delta_content` | 中间片段 | 文本增量，调用方累加 |
| `tool_calls` | 仅末尾 chunk | 完整工具调用列表（客户端已合并分片） |
| `finish_reason` | 仅末尾 chunk | 流结束原因（`stop` / `tool_calls` / `length`） |
| `usage` | 通常末尾 chunk | token 用量（需在请求中启用 `stream_options.include_usage`） |
| `raw` | 任意 | 原始 SSE chunk（调试用） |

## 可观测性

### 自动装配 Hooks

`Workflow` 构造时若未显式传入 `hooks` 且 `auto_hooks=True`（默认），自动装配 `CompositeHooks([LoggingHooks, TokenAccountingHooks])`，使日志与 Token 计量开箱即用。

```python
# 默认行为：自动装配，无需任何配置
wf = Workflow(name="demo", steps=[...])
result = await wf.run(inputs={...})

# 显式关闭自动装配
wf = Workflow(name="demo", steps=[...], auto_hooks=False)

# 传入自定义 hooks（不会被 auto_hooks 覆盖）
wf = Workflow(name="demo", steps=[...], hooks=my_custom_hooks)
```

### Step 执行轨迹

每次 Step 执行产生一条 `StepTrace`，记录：

| 字段 | 说明 |
|------|------|
| `step_id` | Step 标识 |
| `status` | `success` / `failed` / `skipped` |
| `duration_ms` | 执行耗时（毫秒），未计时为 `None` |
| `token_usage` | Token 用量（LLMStep 填充） |
| `tool_calls` | 工具调用记录 |
| `error` | 失败时的异常信息 |
| `retry_count` | 实际重试次数 |

### TraceSummary 可视化

`TraceSummary` 将 `StepTrace` 列表渲染为 Markdown 表格，展示 Step 级耗时、Token 用量与失败链：

```python
from agentkit.core.trace_summary import TraceSummary

summary = TraceSummary.from_context(result.context)
print(summary.to_text())
```

输出示例：

```
| Step          | 状态    | 耗时      | Tokens |
|---------------|---------|-----------|--------|
| fetch_data    | success | 120.5ms   | N/A    |
| analyze       | success | 3450.2ms  | 1856   |
| notify        | success | 89.1ms    | N/A    |

总计: 3 步, 耗时 3659.8ms, Tokens 1856
```

CLI `run` 命令运行结束后自动输出 TraceSummary。

### 自定义 Hooks

继承 `LifecycleHooks` 实现自定义钩子：

```python
from agentkit.core.hooks import LifecycleHooks, CompositeHooks, LoggingHooks

class MyHooks(LifecycleHooks):
    async def before_step(self, step, ctx):
        print(f"开始执行: {step.id}")

    async def after_step(self, step, ctx, trace):
        print(f"完成: {step.id}, 耗时 {trace.duration_ms}ms")

# 组合多个 hooks
hooks = CompositeHooks([LoggingHooks(), MyHooks()])
wf = Workflow(name="demo", steps=[...], hooks=hooks)
```

可用的钩子方法：

- `before_step(step, ctx)` — Step 执行前
- `after_step(step, ctx, trace)` — Step 执行后
- `on_step_error(step, ctx, error)` — Step 失败时，返回 `ErrorAction`（RAISE / SKIP / DEFAULT / RETRY）
- `on_llm_call(agent, messages, response, usage)` — LLM 调用后
- `on_llm_stream_start(step, agent, *, attempt=0)` — 流式调用开始（含 retry/降级，前端据此重置缓冲）
- `on_llm_stream_delta(step, agent, delta, accumulated, *, attempt=0)` — 流式文本片段
- `on_llm_stream_end(step, agent, full_content, *, attempt=0)` — 流式调用结束
- `on_tool_call(tool, params, result)` — 工具调用后
- `on_mcp_call(server, tool, params, result)` — MCP 工具调用后

内置实现：

- `LoggingHooks` — 输出 Step 执行日志
- `TokenAccountingHooks` — 累计 Token 用量，通过 `.total_tokens` 属性获取
- `CompositeHooks` — 组合多个 hooks，按顺序触发

## LLM 配置

### 内置预设提供商

| 名称 | 模型 | 提供商类型 | 特性 |
|------|------|-----------|------|
| `deepseek` | deepseek-v4-pro | deepseek | 思考模式开启，reasoning_effort=high |
| `deepseek-flash` | deepseek-v4-flash | deepseek | 思考模式关闭，更快更省 |
| `mimo` | mimo-v2.5-pro | mimo | 思考模式开启，文本推理强项 |
| `mimo-omni` | mimo-v2.5 | mimo | 思考模式开启，支持图片/音频/视频理解 |

API Key 从环境变量读取：DeepSeek 用 `DEEPSEEK_API_KEY`，MiMo 用 `MIMO_API_KEY`。

### 注册自定义提供商

```python
from agentkit.llm.provider import register_provider, LLMProvider

register_provider(LLMProvider(
    name="my_local",
    base_url="http://localhost:8000/v1",
    api_key="sk-local",
    model="llama-3-70b",
    provider_type="openai",
))
```

所有提供商须兼容 OpenAI Chat Completions API（`POST {base_url}/chat/completions`）。`provider_type="openai"` 创建通用 `OpenAIClient`，`provider_type="deepseek"` 创建带深度优化的 `DeepSeekClient`，`provider_type="mimo"` 创建支持思考与多模态的 `MiMoClient`。

### 在工作流中使用

```python
from agentkit.llm.provider import create_client
from agentkit.core.workflow import Workflow

client = create_client("deepseek")  # 或 "deepseek-flash" / 自定义名
wf = Workflow(
    name="demo",
    steps=[...],
    llm_client=client,
    owns_llm_client=True,  # Workflow 负责关闭客户端
)

async with wf:
    result = await wf.run(inputs={...})
```

`owns_llm_client=True` 时，Workflow 在执行结束后自动调用 `client.close()`。使用 `async with` 可确保异常退出时也能正确释放资源。

### LLM 客户端生命周期

```python
# 方式一：async with 自动管理
async with Workflow(steps=[...], llm_client=client, owns_llm_client=True) as wf:
    result = await wf.run(inputs={...})

# 方式二：手动管理（owns_llm_client=False）
wf = Workflow(steps=[...], llm_client=client, owns_llm_client=False)
result = await wf.run(inputs={...})
# client 生命周期由调用方管理
```

### Agent 配置

```yaml
agents:
  - name: analyzer
    model: gpt-4o-mini          # 模型名
    system: |                    # 系统提示词
      你是数据分析助手。
    temperature: 0.2             # 采样温度
    max_tool_iterations: 5       # Function Call 最大轮次（0 = 用默认值 5）
    tools:                       # 工具名引用
      - db.query
      - web.search
    skills:                      # Skill 名引用
      - web_search
    fallback_model: gpt-4o       # 降级模型（可选）
    on_exhausted: raise          # 重试耗尽策略: raise | default | skip
```

## Skill 系统

Skill 是封装了系统提示词、输出契约与专属工具的能力包，支持多 Skill 合并应用到 Agent。

### Skill 包结构

```
skills/
└── web_search/
    ├── skill.yaml              # 清单文件
    ├── system_prompt.txt       # 系统提示词
    ├── output_schema.py        # Pydantic 输出模型
    └── tools.py                # 专属工具实现
```

### skill.yaml 格式

```yaml
name: web_search
version: "1.0.0"
description: "联网搜索与网页内容提取能力"

agent:
  system_file: system_prompt.txt
  output_model: output_schema.py:SearchResult
  max_tool_iterations: 5

tools:
  - module: tools.py
    functions: [search, scrape]

requires:
  mcp: ["filesystem"]

prompt_injection:
  append_to_system: |
    你拥有联网搜索能力。
```

### 加载 Skill

```python
from agentkit.skill.loader import load_skill, load_skills

# 加载单个 skill
load_skill("skills", "web_search")

# 加载目录下所有 skill
load_skills("skills")
```

`SkillLoader` 自动：
- 读取 `skill.yaml` 清单
- 加载 `system_prompt.txt` 到 `system_prompt`
- 动态导入 `output_schema.py` 解析输出模型
- 动态导入 `tools.py` 注册专属工具到全局 `ToolRegistry`
- 校验 `requires.mcp` 依赖

### 工具实现形式

`tools.py` 中的工具支持三种形式：

```python
# 1. Tool 子类 + @tool 装饰器
from agentkit.tools.base import Tool, tool

@tool(name="web_search.search")
class SearchTool(Tool):
    async def call(self, params, ctx):
        return {"results": [...]}

# 2. 裸 async 函数（自动适配为 Tool）
async def scrape(url: str) -> dict:
    """抓取网页内容"""
    return {"content": "..."}

# 3. Tool 子类实例
class CustomTool(Tool):
    name = "web_search.custom"
    async def call(self, params, ctx):
        return {}
```

### 多 Skill 合并规则

| 字段 | 合并规则 |
|------|----------|
| `system_prompt` | 按声明顺序拼接（`\n\n` 分隔） |
| `tools` | 并集（保序去重） |
| `output_model` | 取第一个非 None 的；冲突时告警 |
| `max_tool_iterations` | 取最大值 |
| `prompt_injection_append` | 按声明顺序拼接 |
| `requires_mcp` | 并集（保序去重） |

`apply_skills_to_agent` 将合并结果应用到 `AgentConfig` 时遵循 **Agent 显式声明优先** 原则：Agent 自身的 `system`、`tools`、`output_model`、`max_tool_iterations` 不会被 Skill 覆盖。

## MCP 集成

MCP（Model Context Protocol）Server 的工具在连接后自动发现并注册为框架 Tool，可被 Function Call 直接调度。

### 配置 MCP Server

在 `workflow.yaml` 中声明：

```yaml
mcp_servers:
  - name: filesystem
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]

  - name: remote_api
    transport: sse
    url: "http://localhost:3001/sse"
```

### Agent 引用 MCP

```yaml
agents:
  - name: assistant
    model: gpt-4o-mini
    mcp: ["filesystem"]    # 引用 MCP Server 名
    tools:                  # 额外的本地工具
      - db.query
```

`MCPManager.connect_all()` 后，对应 Server 发现的工具名自动注入到 `agent.tools`，对 LLM Function Call 可见。

### 健康检查

```bash
agentkit mcp health-check --yaml-path workflow.yaml
```

输出每个 Server 的连接状态、可用工具与资源列表。

## 自定义 Tool

### 方式一：@tool 装饰器

```python
from agentkit.tools.base import Tool, tool

@tool(name="db.query", role="source")
class DBQueryTool(Tool):
    """查询数据库"""

    async def call(self, params: dict, ctx) -> dict:
        sql = params["sql"]
        return {"rows": [...]}

    @property
    def param_model(self):
        from pydantic import BaseModel

        class Params(BaseModel):
            sql: str
        return Params
```

### 方式二：手动注册

```python
from agentkit.tools.base import Tool, register

class MyTool(Tool):
    name = "my_tool"
    description = "自定义工具"
    role = "action"

    async def call(self, params, ctx):
        return {"ok": True}

register(MyTool())
```

### Tool 语义角色

| 角色 | 含义 | 示例 |
|------|------|------|
| `source` | 数据源，读取外部数据 | `db.query`、`web.search` |
| `action` | 执行动作，产生副作用 | `sink.notify`、`api.call` |
| `sink` | 数据落地，写入存储 | `sink.wecom`、`sink.file` |

`role` 仅用于可观测性与编排提示，不影响接口分派。

## CLI 命令

```bash
# 运行工作流
agentkit run workflow.yaml --input date=2024-01-01

# 从检查点恢复
agentkit run workflow.yaml --resume <run_id>

# 静态校验 YAML
agentkit validate workflow.yaml

# 生成执行计划（不实际运行）
agentkit dry-run workflow.yaml

# MCP Server 健康检查
agentkit mcp health-check --yaml-path workflow.yaml

# 启动可视化服务（FastAPI + SSE）
agentkit serve --dir ./workflows --port 8000
```

`run` 命令运行结束后自动输出 TraceSummary（Step 级耗时、Token 用量、失败链）。`--input` 支持 JSON 值：`--input count=5`（解析为 int）、`--input tags='["a","b"]'`（解析为 list）。`--verbose` / `-v` 开启 DEBUG 级日志，排查网络/协议问题。

`serve` 命令启动 FastAPI 可视化服务，支持 `--host`（绑定地址）、`--port`（端口）、`--token`（鉴权 bearer token）、`--cors-origins`（CORS 允许的 origin）等参数。详见[可视化服务](#可视化服务)章节。

## 全局配置

所有默认值集中在 `agentkit.config`，通过 `get_default` / `set_default` 读写：

```python
from agentkit.config import get_default, set_default

# 读取
timeout = get_default("default_step_timeout_seconds")  # 300.0

# 运行时覆盖
set_default("default_max_tool_iterations", 10)

# 重置为内置默认
from agentkit.config import reset_default
reset_default("default_max_tool_iterations")
```

### 配置项参考

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `large_object_threshold` | 1048576 | Context 大对象阈值（字节） |
| `default_retry_count` | 1 | 默认重试次数（不含首次） |
| `default_retry_backoff` | `"fixed"` | 退避策略：`fixed` / `exponential` |
| `default_retry_base_seconds` | 5.0 | 退避基准秒数 |
| `default_max_concurrency` | 5 | ParallelStep 默认最大并发 |
| `default_parallel_timeout_seconds` | 60.0 | ParallelStep 默认超时 |
| `default_step_timeout_seconds` | 300.0 | 单个 Step 默认超时 |
| `default_max_tool_iterations` | 5 | Function Call 最大轮次 |
| `default_max_loop_iterations` | 100 | LoopStep 最大迭代次数 |
| `context_snapshot_big_object_summary_len` | 200 | 快照中大对象摘要最大长度 |
| `llm_request_timeout_seconds` | 180.0 | LLM 单次请求超时 |
| `default_llm_provider` | `"deepseek"` | 默认 LLM 提供商名 |
| `default_hooks_enabled` | `True` | 是否默认装配可观测性 Hooks |
| `executor_max_workers` | 4 | BlockingExecutor 线程池大小（同步阻塞库卸载） |
| `executor_max_processes` | 2 | BlockingExecutor 进程池大小（P0 未实现） |
| `server_host` | `"127.0.0.1"` | Server 绑定地址 |
| `server_port` | 8000 | Server 端口 |
| `server_token` | `""` | Server 鉴权 bearer token（空=仅本地） |
| `server_cors_origins` | `[]` | CORS 允许的 origin 列表（空=关闭） |
| `server_event_queue_size` | 1000 | EventBus 每订阅者队列容量 |
| `server_event_log_max_events` | 100000 | 单 run 事件日志最大事件数 |
| `server_artifact_max_size` | 104857600 | 单 artifact 最大字节（100MB） |
| `server_artifact_max_total` | 1073741824 | 单 run 产物总量最大字节（1GB） |
| `server_gc_interval_seconds` | 21600 | GCSweeper 定时扫描间隔（6h） |
| `server_gc_orphan_grace_seconds` | 86400 | GCSweeper 孤儿文件宽限期（24h） |

## 自定义 Step

继承 `BaseStep` 并实现 `run` 方法，用 `@register_step` 注册到全局注册表即可在 YAML 中按 `type` 引用：

```python
from agentkit.steps.base import BaseStep, register_step
from agentkit.core.context import Context

@register_step("echo")
class EchoStep(BaseStep):
    type = "echo"

    def __init__(self, message: str = "", **kwargs):
        super().__init__(**kwargs)
        self.message = message

    async def run(self, ctx: Context) -> Context:
        result = self.message
        if self.output:
            ctx.set(self.output, result)
        return ctx
```

YAML 中使用：

```yaml
- id: hello
  type: echo
  message: "Hello, World!"
  output: greeting
```

## 检查点与恢复

Workflow 支持 checkpoint 持久化，失败后可从断点恢复：

```python
# 运行时自动保存检查点
result = await wf.run(inputs={...})

# 若失败，用 run_id 恢复
if result.status == "failed":
    result = await wf.resume(result.run_id)
```

CLI 同样支持：

```bash
agentkit run workflow.yaml --resume <run_id>
```

## Report Engine SDK

Report Engine SDK 是协议无关、配置驱动的 Markdown 报告生成引擎。通过 **Pack** 组织模板与规则，支持多角色多视图输出，内置多种框架适配器。

### Pack 结构

每个 Pack 是一个自包含目录，独立拥有与版本化：

```
config/packs/
└── ops_report/
    ├── pack.json              # 清单：报告定义 + input_schema + rules + templates
    └── templates/
        └── health_check.md    # Jinja2 模板
```

报告的全局标识为 `"<pack_id>:<report_name>"`（如 `ops_report:health_check`），跨 Pack 唯一。

### pack.json 格式

```json
{
  "pack_id": "ops_report",
  "version": "1.0.0",
  "reports": {
    "health_check": {
      "input_schema": {
        "type": "object",
        "required": ["service_name", "uptime_pct"],
        "properties": {
          "service_name": {"type": "string"},
          "uptime_pct": {"type": "number"}
        }
      },
      "rules": [
        {"field": "status", "formula": "'healthy' if uptime_pct >= 99.9 else 'warning'"}
      ],
      "templates": {
        "default": "templates/health_check.md"
      }
    }
  }
}
```

`rules` 管线支持声明式公式（`formula`）与命令式插件（`plugin`），`{"ref": "..."}` 可引用 Pack 共享规则文件。`templates` 是多视图映射，通过 `view` 参数切换视角。

### 两步流水线

```python
from report_engine_sdk import ReportEngine, FileSystemPackProvider, LocalStorage

# 构建 Engine：配置目录 + 存储后端
engine = ReportEngine("config/packs", LocalStorage("./output"))

# 1. evaluate：校验 input_schema + 执行 rules 管线
result = engine.evaluate("ops_report:health_check", {
    "service_name": "api-gateway",
    "uptime_pct": 99.95,
    "error_count": 3,
    "latency_ms": 45.2,
    "date_str": "2026-07-23",
})
# result.success → True, result.data → 含 rules 计算结果的完整 dict

# 2. render：Jinja2 模板渲染，支持多视图
rendered = engine.render("ops_report:health_check", result.data, view="default")
# rendered.content → Markdown 字符串
# rendered.uri → file:// URI（LocalStorage 落盘时）
```

已有结构化数据时可跳过 evaluate，直接调用 `render`。

### 适配器

| 适配器 | 模块 | 说明 |
|--------|------|------|
| AgentKit Tool | `adapters.agentkit` | 包装为 agentkit `Tool`，支持 ToolStep 与 LLM Function Call |
| MCP Server | `adapters.mcp_server` | 暴露为 MCP Server 工具 |
| LangChain Tool | `adapters.langchain_tool` | 包装为 LangChain Tool |
| FastAPI Router | `adapters.api_router` | 挂载为 FastAPI 路由 |

AgentKit 适配器深度集成框架特性：`execution="thread"` 卸载到子线程避免阻塞事件循环，`role="sink"` 语义角色，注入 `ArtifactStore` 后自动落盘 + 发 `ARTIFACT_PRODUCED` 事件。

### 存储后端

| 后端 | 类 | 说明 |
|------|-----|------|
| 本地文件 | `LocalStorage` | 渲染结果落盘为 `file://` URI |
| 内存 | `MemoryStorage` | 测试用，不持久化 |
| S3 | `S3Storage` | 云存储（需 `boto3`） |

### 内置 Pack

| Pack | 报告 | 说明 |
|------|------|------|
| `ops_report` | `health_check` | 运维健康检查报告 |
| `work_report` | `daily_briefing` | 每日工作简报（多视图） |
| `teacher_eval` | `teacher` / `manager` | 教师评估（多角色多视图） |
| `learning_report` | `learning_profile` | 学习画像报告（含规则引擎） |

## 可视化服务

AgentKit 内置 FastAPI + SSE 可视化服务，提供工作流管理、运行控制、产物浏览与实时事件推送。

### 启动

```bash
# 安装 server 可选依赖
uv sync --extra server

# 启动服务
agentkit serve --dir ./workflows --port 8000

# 对外访问（需配置 token）
agentkit serve --host 0.0.0.0 --port 8000 --token my-secret
```

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查（不鉴权，容器探活用） |
| `/api/workflows` | GET | 列出 `--dir` 目录下的工作流 YAML |
| `/api/workflows/{name}` | GET | 获取工作流详情 |
| `/api/runs` | POST | 启动工作流运行 |
| `/api/runs` | GET | 列出运行记录 |
| `/api/runs/{run_id}` | GET | 获取运行状态 |
| `/api/runs/{run_id}/cancel` | POST | 取消运行（graceful / immediate） |
| `/api/runs/{run_id}/resume` | POST | 从中断点恢复运行 |
| `/api/runs/{run_id}/events` | GET | SSE 事件流（实时推送） |
| `/api/runs/{run_id}/artifacts` | GET | 列出运行产物 |

### 安全

- 默认绑定 `127.0.0.1`，仅本地可访问
- `--token` 设置 bearer token 鉴权（`/health` 豁免）
- `--cors-origins` 配置 CORS 允许的 origin

## 运行时层

运行时层（`runtime/`）为可视化服务提供运行控制、事件分发、产物管理与崩溃恢复能力，与 `core/` 完全解耦（仅通过 hooks 接入）。

| 模块 | 核心类 | 职责 |
|------|--------|------|
| `event` | `EventBus` / `EventLog` | RunEvent v1 协议 + 事件分发 + 持久化日志 |
| `event_hooks` | `EventBusHooks` | LifecycleHooks 子类，hook → 事件翻译 |
| `artifact` | `ArtifactStore` / `GCSweeper` | 产物写序协议 + 孤儿文件 GC 对账 |
| `blocking` | `BlockingExecutor` | thread / process 卸载同步阻塞库（reportlab / python-docx） |
| `run_manager` | `RunManager` | 状态机编排 + 取消编排 + 内存注册表 |
| `reconciler` | `Reconciler` | 启动对账：僵尸 run 标记 + GC + 日志完整性检查 |

崩溃恢复流程：`kill -9` 后重启 → Reconciler 标记 `interrupted` → `POST /resume` 从 checkpoint 恢复 → 跳过已完成 Step（sink 类工具不重复执行）。

## 项目结构

```
LiteFlow/
├── src/
│   ├── agentkit/                    # 智能体框架
│   │   ├── __init__.py              # 包入口，版本号
│   │   ├── cli.py                   # CLI 命令行入口（run / validate / dry-run / mcp / serve）
│   │   ├── config.py                # 全局配置中枢
│   │   ├── core/
│   │   │   ├── agent.py             # AgentConfig 配置对象
│   │   │   ├── context.py           # Context 共享状态（FrozenDict 不可变）
│   │   │   ├── cancel.py            # CancelToken 取消信号
│   │   │   ├── checkpoint.py        # 检查点存储
│   │   │   ├── hooks.py             # 生命周期钩子
│   │   │   ├── ports.py             # 端口系统（类型契约、输入绑定、输出校验）
│   │   │   ├── template.py          # 模板引擎
│   │   │   ├── trace_summary.py     # 执行轨迹汇总可视化
│   │   │   └── workflow.py          # Workflow 编排器
│   │   ├── steps/
│   │   │   ├── base.py              # BaseStep 抽象基类
│   │   │   ├── tool_step.py         # 工具调用 Step
│   │   │   ├── llm_step.py          # LLM 调用 Step
│   │   │   ├── condition_step.py    # 条件分支 Step
│   │   │   ├── loop_step.py         # 循环 Step
│   │   │   ├── parallel_step.py     # 并行 Step
│   │   │   └── skill_step.py        # Skill 调用 Step
│   │   ├── skill/
│   │   │   ├── registry.py          # SkillManifest + SkillRegistry
│   │   │   ├── loader.py            # Skill 包加载器
│   │   │   └── merger.py            # 多 Skill 合并
│   │   ├── tools/
│   │   │   ├── base.py              # Tool 抽象基类 + 注册机制
│   │   │   ├── db.py / http.py / file.py / time.py / sinks.py  # 内置工具
│   │   │   └── md_convert/          # Markdown 转换工具（DOCX / PDF / HTML）
│   │   ├── mcp/
│   │   │   ├── manager.py           # MCP 连接管理 + 自动发现
│   │   │   ├── client.py            # MCP 客户端
│   │   │   └── wrapper.py           # MCP 工具包装器
│   │   ├── llm/
│   │   │   ├── base.py              # LLMClient 抽象基类
│   │   │   ├── provider.py          # 提供商配置与注册
│   │   │   ├── openai.py            # OpenAI 兼容客户端
│   │   │   ├── deepseek.py          # DeepSeek 深度优化客户端
│   │   │   ├── mimo.py              # MiMo 思考 + 多模态客户端
│   │   │   ├── thinking.py          # 思考模式共享逻辑
│   │   │   ├── media.py             # 多模态媒体处理
│   │   │   └── mock.py              # Mock 客户端（测试用）
│   │   ├── parsers/                 # 输出解析器（text / json / pydantic）
│   │   ├── runtime/                 # 可视化运行时层
│   │   │   ├── event.py             # RunEvent v1 协议 + EventBus + EventLog
│   │   │   ├── event_hooks.py       # EventBusHooks（hook → 事件翻译）
│   │   │   ├── artifact.py          # ArtifactStore + GCSweeper
│   │   │   ├── blocking.py          # BlockingExecutor（thread/process 卸载）
│   │   │   ├── run_manager.py       # RunManager（状态机 + 取消 + 注册表）
│   │   │   └── reconciler.py        # Reconciler（启动对账 + 崩溃恢复）
│   │   ├── server/                  # FastAPI 可视化服务
│   │   │   ├── app.py               # 应用工厂 + 生命周期
│   │   │   ├── settings.py          # ServerSettings
│   │   │   ├── security.py          # 鉴权 + CORS
│   │   │   ├── sse.py               # SSE 适配
│   │   │   └── routes/              # workflows / runs / artifacts 路由
│   │   ├── yaml/
│   │   │   ├── loader.py            # YAML → SDK 对象编译器
│   │   │   └── validator.py         # YAML 静态校验器
│   │   └── tests/                   # 测试套件
│   └── report_engine_sdk/           # 报告引擎 SDK
│       ├── core/                    # 协议无关核心层
│       │   ├── engine.py            # ReportEngine facade
│       │   ├── config_provider.py   # Pack 配置提供者
│       │   ├── pack_loader.py       # Pack 加载器
│       │   ├── validator.py         # Schema 校验
│       │   ├── calculator.py        # 规则计算引擎
│       │   ├── renderer.py          # Jinja2 模板渲染
│       │   └── plugin_registry.py   # 插件注册表
│       ├── adapters/                # 框架适配器
│       │   ├── agentkit.py          # AgentKit Tool 适配器
│       │   ├── mcp_server.py        # MCP Server 适配器
│       │   ├── langchain_tool.py    # LangChain Tool 适配器
│       │   └── api_router.py        # FastAPI Router 适配器
│       ├── storage/                 # 存储后端
│       │   ├── local.py / memory.py / s3.py
│       ├── config/packs/            # 内置 Pack（ops / work / teacher / learning）
│       └── tests/
├── examples/                        # 示例
│   ├── report_demo.yaml             # 报告生成 × AgentKit 声明式演示
│   ├── run_report_demo.py           # Python 入口（声明式 + LLM Function Call）
│   └── story_best_practice.yaml     # Story 最佳实践
├── docs/                            # 设计文档
├── pyproject.toml                   # 项目配置（uv）
├── uv.lock                          # 依赖锁定
└── AGENTS.md                        # 项目规范
```

## 测试

```bash
# 安装开发依赖
uv sync --extra dev

# 运行全部测试
uv run pytest

# 运行特定测试文件
uv run pytest src/agentkit/tests/test_workflow.py

# 查看详细输出
uv run pytest -v
```

测试配置：`asyncio_mode = "auto"`，AgentKit 测试目录 `src/agentkit/tests/`，Report Engine SDK 测试目录 `src/report_engine_sdk/tests/`。

## 设计原则

- **高度模块化**：core / steps / skill / mcp / llm / tools / yaml / runtime / server 各司其职，子模块间通过 `TYPE_CHECKING` 避免循环依赖
- **协议无关核心**：report_engine_sdk 核心层零框架依赖，适配器懒加载可选依赖
- **易于配置**：所有默认值集中在 `agentkit.config` 统一管理
- **可拓展**：新增 Step 类型、Tool、Skill、LLM 提供商均通过注册机制接入
- **不可变理念**：配置对象通过 `dataclasses.replace` 产生副本，不修改原对象
- **安全求值**：模板表达式使用 AST 解析，不调用 `eval()`
- **零侵入运行时**：runtime 层仅通过 hooks 接入，`core/` 不依赖 runtime

## 许可证

MIT
