# AgentKit

轻量化智能体框架，采用双层架构（YAML 配置层 + Python SDK 层），围绕 8 个核心概念构建：Agent / Tool / Skill / MCP / Step / Context / Workflow / Hooks。

## 核心特性

- **双层架构**：YAML 声明式配置 + Python SDK，按需选择抽象层级
- **6 种 Step 类型**：tool / llm / condition / loop / parallel / skill，覆盖常见编排场景
- **模板引擎**：`{{var}}` 变量替换、`${ENV}` 环境变量、`{{#if}}`/`{{#each}}` 条件与循环
- **可观测性开箱即用**：自动装配日志与 Token 计量 Hooks，Step 级耗时与失败链可视化
- **LLM 客户端生命周期托管**：`async with` 自动关闭连接，避免泄漏
- **Skill 能力包**：系统提示词 + 输出契约 + 专属工具的封装单元，支持多 Skill 合并
- **MCP 自动发现**：连接 MCP Server 后自动注册其工具为框架一等公民
- **多 LLM 提供商**：内置 DeepSeek 预设，兼容所有 OpenAI Chat Completions API

## 安装

```bash
pip install -e .
```

可选依赖按需安装：

```bash
pip install -e ".[openai]"    # OpenAI SDK（DeepSeek 等兼容客户端）
pip install -e ".[redis]"     # Redis 检查点存储
pip install -e ".[dev]"       # pytest + pytest-asyncio
```

环境要求：Python >= 3.11

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

## 核心概念

| 概念 | 职责 | 核心类 |
|------|------|--------|
| **Agent** | 提示词 + 模型 + 输出契约 + 工具集 + 重试策略的配置载体 | `AgentConfig` |
| **Tool** | 统一工具接口，分 Source / Action / Sink 三种语义角色 | `Tool` |
| **Skill** | 能力包：系统提示词 + 输出模型 + 专属工具的封装 | `SkillManifest` |
| **MCP** | 外部 MCP Server 连接管理与工具自动发现注册 | `MCPManager` |
| **Step** | 执行单元，6 种类型，含钩子/超时/重试/trace 编排 | `BaseStep` |
| **Context** | 工作流共享状态，支持快照/恢复与 trace 记录 | `Context` |
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

调用 LLM 生成文本，支持 Function Call 多轮对话。`prompt` 为用户提示词模板（支持变量替换与块语法）。

```yaml
- id: analyze
  type: llm
  agent: analyzer
  prompt: |
    日期: {{date}}
    数据: {{orders_raw}}
    请生成摘要。
  output: analysis
```

> **字段说明**：`prompt` 为规范字段名。旧版使用的 `input` 字段仍向后兼容，但会触发 `DeprecationWarning`，建议迁移至 `prompt`。

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
  until: "'{{validation_pass}}' == 'true'"
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
- `on_tool_call(tool, params, result)` — 工具调用后
- `on_mcp_call(server, tool, params, result)` — MCP 工具调用后

内置实现：

- `LoggingHooks` — 输出 Step 执行日志
- `TokenAccountingHooks` — 累计 Token 用量，通过 `.total_tokens` 属性获取
- `CompositeHooks` — 组合多个 hooks，按顺序触发

## LLM 配置

### 内置预设提供商

| 名称 | 模型 | 特性 |
|------|------|------|
| `deepseek` | deepseek-v4-pro | 思考模式开启，reasoning_effort=high |
| `deepseek-flash` | deepseek-v4-flash | 思考模式关闭，更快更省 |

API Key 从环境变量 `DEEPSEEK_API_KEY` 读取。

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

所有提供商须兼容 OpenAI Chat Completions API（`POST {base_url}/chat/completions`）。`provider_type="openai"` 创建通用 `OpenAIClient`，`provider_type="deepseek"` 创建带深度优化的 `DeepSeekClient`。

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
```

`run` 命令运行结束后自动输出 TraceSummary（Step 级耗时、Token 用量、失败链）。`--input` 支持 JSON 值：`--input count=5`（解析为 int）、`--input tags='["a","b"]'`（解析为 list）。

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
| `llm_request_timeout_seconds` | 120.0 | LLM 单次请求超时 |
| `default_llm_provider` | `"deepseek"` | 默认 LLM 提供商名 |
| `default_hooks_enabled` | `True` | 是否默认装配可观测性 Hooks |

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

## 项目结构

```
agentkit/
├── __init__.py              # 包入口，版本号
├── cli.py                   # CLI 命令行入口
├── config.py                # 全局配置中枢
├── core/
│   ├── agent.py             # AgentConfig 配置对象
│   ├── context.py           # Context 共享状态
│   ├── checkpoint.py        # 检查点存储
│   ├── hooks.py             # 生命周期钩子
│   ├── template.py          # 模板引擎
│   ├── trace_summary.py     # 执行轨迹汇总可视化
│   └── workflow.py          # Workflow 编排器
├── steps/
│   ├── base.py              # BaseStep 抽象基类
│   ├── tool_step.py         # 工具调用 Step
│   ├── llm_step.py          # LLM 调用 Step
│   ├── condition_step.py    # 条件分支 Step
│   ├── loop_step.py         # 循环 Step
│   ├── parallel_step.py     # 并行 Step
│   └── skill_step.py        # Skill 调用 Step
├── skill/
│   ├── registry.py          # SkillManifest + SkillRegistry
│   ├── loader.py            # Skill 包加载器
│   └── merger.py            # 多 Skill 合并
├── tools/
│   ├── base.py              # Tool 抽象基类 + 注册机制
│   └── report_engine.py     # ReportEngine 工具适配器
├── mcp/
│   └── manager.py           # MCP 连接管理 + 自动发现
├── llm/
│   ├── base.py              # LLMClient 抽象基类
│   ├── provider.py          # 提供商配置与注册
│   ├── openai.py            # OpenAI 兼容客户端
│   ├── deepseek.py          # DeepSeek 深度优化客户端
│   └── mock.py              # Mock 客户端（测试用）
├── yaml/
│   ├── loader.py            # YAML → SDK 对象编译器
│   └── validator.py         # YAML 静态校验器
└── parsers/                  # Function Call 解析器
```

## 测试

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行全部测试
pytest

# 运行特定测试文件
pytest tests/test_template.py

# 查看详细输出
pytest -v
```

测试配置：`asyncio_mode = "auto"`，测试目录 `tests/`。

## 设计原则

- **高度模块化**：core / steps / skill / mcp / llm / tools / yaml 各司其职，子模块间通过 `TYPE_CHECKING` 避免循环依赖
- **易于配置**：所有默认值集中在 `agentkit.config` 统一管理
- **可拓展**：新增 Step 类型、Tool、Skill、LLM 提供商均通过注册机制接入
- **不可变理念**：配置对象通过 `dataclasses.replace` 产生副本，不修改原对象
- **安全求值**：模板表达式使用 AST 解析，不调用 `eval()`

## 许可证

MIT
