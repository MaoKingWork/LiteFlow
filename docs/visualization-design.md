# 可视化适配设计（Visualization Adaptation）

为 AgentKit 引入可视化能力：**实时编辑工作流、实时查看运行状态与产物、实时中断运行**。在不推翻现有引擎（线性 Step 序列 + 容器嵌套 + checkpoint/resume）的前提下，新增三个互相独立的适配层：运行时控制层、事件与产物层、服务与编辑层。

> 开发阶段设计。引擎核心语义（执行模型、checkpoint、hooks 契约）不变；所有可视化能力以"叠加层"形式接入，不侵入执行路径。

---

## 1. 设计目标与原则

| 目标 | 落地方式 |
|------|----------|
| 轻量 | 引擎层零新依赖；Server 为可选 extra（`pip install ".[server]"`），照抄 `report_engine_sdk/adapters/api_router.py` 懒加载模式；SSE 而非 WebSocket；不引入消息队列/事务协调器 |
| 稳定 | 事件协议显式版本化；所有状态转换落盘；崩溃可恢复、可对账；引擎测试先行补齐 |
| 模块化 | `core/` 不依赖 server 代码；事件、产物、执行卸载、运行控制各自独立成模块，经 hooks 与注册表接入 |
| 易维护 | 前后端唯一契约 = 版本化事件协议 + 文档模型 JSON Schema；历史与实时复用同一事件流渲染 |
| 易用 | `agentkit serve` 一键起本地服务 + 托管前端；不声明任何新字段时行为完全同现状 |

**核心定位**：可视化是叠加在现有「YAML 定义 + Workflow 引擎 + Hooks + Checkpoint」之上的**观测与控制层**。引擎只新增两个原语（取消令牌、执行卸载），其余全部在外层完成。

---

## 2. 现状评估（依据）

### 2.1 可直接复用的地基

| 能力 | 位置 | 复用方式 |
|------|------|----------|
| 全生命周期 hooks（含流式 `attempt` 契约） | `core/hooks.py` | 新增 `EventBusHooks` 子类翻译为序列化事件，执行引擎零改动 |
| StepTrace（状态/耗时/token/error/摘要） | `steps/base.py` | 事件 payload 直接对齐其字段 |
| CancelledError 可穿透（`asyncio.wait_for` + BaseException 不捕获） | `steps/base.py:286,301` | 硬中断的技术底座，仅需显式状态处理 |
| 每步 checkpoint + resume | `core/checkpoint.py`、`core/workflow.py` | 崩溃恢复、断点续跑已在；`status` 为自由 str，可扩展取值 |
| 结构化校验 path（`steps[2].then[1]`） | `yaml/validator.py` | 编辑器错误定位雏形，补 `severity/code` 即可 |
| 端口系统（`inputs/outputs/type/from`） | `core/ports.py` | 前端连线类型检查、自动连线的数据源 |
| FastAPI 懒加载 adapter 先例 | `report_engine_sdk/adapters/api_router.py` | Server 层复用同一模式 |
| Redis checkpoint 后端先例 | `core/checkpoint.py` | 未来多实例演进的预留接口 |
| 同步阻塞卸载先例 | `tools/db.py:257-270`（`asyncio.to_thread`） | 统一抽象为 Tool 级声明机制 |

### 2.2 缺口

1. 无服务层（agentkit 无任何 HTTP/WS 代码，仅 CLI 入口）。
2. 无中断 API；`Checkpoint.status` 仅 `running/completed/failed`；取消时 `CancelledError` 绕过 `except Exception` 的失败落盘分支（`workflow.py:254`），状态会滞留 `running`。
3. 无运行注册表，运行中状态不可查询。
4. 大对象只存 200 字符摘要（`context.py:547-571`），产物全文不可见。
5. 模型单向（YAML→SDK），无反向序列化、无 UI 元数据约定、无内省接口。
6. 事件未协议化：hooks 是进程内对象回调，无序列化/版本化/持久化。
7. **引擎零测试**（`test_*.py` 仅存在于 `report_engine_sdk`），任何改造无回归保障。

---

## 3. 三项架构风险的判定与对策

本章回应设计评审中提出的三个风险，逐一给出判定、代码证据与对策。

### 3.1 事件循环阻塞 —— 判定：成立

**证据**：
- `tools/db.py` 已经用 `asyncio.to_thread` 包装同步 DB 调用（第 257 行注释原文："避免阻塞事件循环"）——项目自己已确认该问题真实，但仅 db 工具有此处理。
- `tools/report_engine.py` 的 `call` 直接同步驱动 `ReportEngine.evaluate → render`，渲染链（reportlab / python-docx / markdown2）均为同步阻塞库，无任何卸载。这是**现存的、确定的阻塞点**。
- 可视化场景下该问题被放大：Server 单事件循环承载所有 run 与 SSE 推送，一次阻塞 = 所有客户端"假死"。

**对策（详见 5.5）**：
- 将 `db.py` 的局部做法抽象为 Tool 级声明：`Tool.execution = "inline" | "thread" | "process"`，默认 `inline`（行为同现状，轻量）。
- `ReportEngineTool` 标记 `execution = "thread"`。
- 事件分发（SSE）永远在主事件循环，绝不进入执行器。
- 线程模式受 GIL 限制，对纯 Python CPU 密集任务仅缓解；`process` 模式提供彻底隔离，以"params/result 仅 JSON 可序列化、Context 不进子进程"为契约。

### 3.2 运行态易失 —— 判定：部分成立

**证据与修正**：
- "任务丢失无法恢复"**不成立**：checkpoint 每步落盘（`completed_steps` + Context 快照），`resume()` 可跳过已完成 Step 续跑。执行进度本身是持久的。
- **成立**的三个子问题：
  1. **僵尸状态**：进程重启后，checkpoint 中 `running` 状态的 run 实际已死，无人认领，前端会显示一个永远"运行中"的幽灵。
  2. **控制权随进程消失**：Task 句柄、取消令牌在内存，重启后无法对旧 run 施加任何操作（语义上旧 run 已随进程终止，无需 cancel，但需要"标记 + 允许恢复"）。
  3. **SSE 断流无续传**：连接断开后事件流无法续接。
- "无法水平扩展"：对轻量单机定位，这是**定位声明而非缺陷**，写入非目标（第 9 章），并以 Redis checkpoint 后端作为演进预留。

**对策（详见 5.3 / 5.4）**：
- 明确划分：**控制面易失**（Task、令牌、订阅者——内存），**数据面持久**（状态机、completed_steps、快照、事件日志、产物引用——磁盘）。
- 启动 **Reconciler** 对账：`running/cancelling` → `interrupted`；**不自动 resume**（避免 sink 类工具如 wecom 通知被意外重发），由用户显式恢复。
- 事件携带 per-run 单调 `seq`，SSE 支持 `Last-Event-ID` 从事件日志补齐。

### 3.3 事件与产物的非事务一致性 —— 判定：成立，但以顺序协议替代事务

**分析**：ArtifactStore（文件）与 EventBus（分发）之间确实无原子性。但两个资源同为本地文件系统，无需事务协调，用**严格写序 + 原子 rename + 事件日志先行 + 对账 GC** 即可达到最终一致。

**对策（详见 5.6）**：生产者五步写序 `写 .tmp → fsync/close → 原子 rename → append 事件日志 → 内存分发`，配合崩溃窗口分析与 GC 对账，保证：
- 前端**永远不可能**收到指向不存在/不完整文件的 URI（rename 先于分发）；
- 孤儿文件有界且可回收（GC 对账，宽限 24h）；
- 实时推送缺失可由 `Last-Event-ID` 从事件日志补齐。

---

## 4. 总体架构

```
┌────────────────────────────────────────────────────────────┐
│ 前端（React Flow 受限画布 + 运行面板 + 产物面板）            │
│   文档模型 JSON ⇄ YAML        事件流渲染器（实时/历史复用）   │
└──────────────▲───────────────────────────▲─────────────────┘
               │ HTTP / SSE                 │
┌──────────────┴───────────────────────────┴─────────────────┐
│ Server 层（agentkit.server，可选 extra，FastAPI 懒加载）     │
│   定义 CRUD/校验 │ 内省 │ 运行控制 │ SSE 适配 │ 产物下载     │
│   Reconciler（启动对账） │ GCSweeper（产物对账）             │
└──────────────▲───────────────────────────▲─────────────────┘
               │ 进程内调用                 │ 订阅
┌──────────────┴───────────────────────────┴─────────────────┐
│ 运行时层（agentkit.runtime）                                │
│   RunManager（状态机/取消两阶段） │ EventBus（per-run 队列） │
│   EventLog（JSONL 持久化）        │ ArtifactStore（写序协议）│
│   BlockingExecutor（thread/process 卸载）                   │
└──────────────▲─────────────────────────────────────────────┘
               │ 引擎 API（run/resume + cancel_token）│ hooks
┌──────────────┴─────────────────────────────────────────────┐
│ 引擎层（agentkit.core / steps —— 语义不变）                  │
│   Workflow + cancel_token │ EventBusHooks（hook→事件翻译）   │
│   CheckpointStore（Local/Redis）                            │
└─────────────────────────────────────────────────────────────┘
```

**依赖方向**：只允许向下依赖。`core/` 不 import `runtime/`、`server/`；`runtime/` 经 hooks 与引擎公开 API 接入；`server/` 仅依赖 `runtime/` 与引擎公开 API。

---

## 5. 子系统设计

### 5.1 事件协议 RunEvent（v1）

前后端唯一运行时契约，显式版本化：

```json
{
  "v": 1,
  "seq": 42,
  "run_id": "run_abc123",
  "type": "llm_delta",
  "ts": 1721300000.123,
  "step_id": "analyze",
  "attempt": 0,
  "payload": { "delta": "……", "accumulated_len": 128 }
}
```

| 字段 | 说明 |
|------|------|
| `v` | 协议版本。破坏性变更升 v2，旧字段只增不改 |
| `seq` | per-run 单调递增整数，SSE `Last-Event-ID` 与历史回放游标 |
| `type` | 事件类型（见下表） |
| `step_id` / `attempt` | 可选；`attempt` 透传现有流式契约（重试/降级时前端重置缓冲） |
| `payload` | 类型相关数据；StepTrace 字段直接对齐 |

事件类型全集：

| 类别 | 类型 | 来源 hook |
|------|------|-----------|
| 运行 | `run_created` / `run_started` / `run_completed` / `run_failed` / `run_cancelling` / `run_cancelled` / `run_interrupted` | RunManager / Workflow hooks |
| Step | `step_started` / `step_finished`（含 status/duration_ms/token/error/retry_count/summary） | `before_step` / `after_step` |
| LLM 流 | `llm_stream_start` / `llm_delta` / `llm_stream_end` | 现有 `on_llm_stream_*` |
| 工具 | `tool_call`（name/params 摘要/result 摘要/duration） | `on_tool_call` / `on_mcp_call` |
| 产物 | `artifact_produced`（`{id, uri, content_type, size, md5, summary}`） | ArtifactStore 写序完成后 |

**背压分级**（per-run `asyncio.Queue`，容量可配，默认 1000）：

| 级别 | 事件 | 队列满策略 |
|------|------|------------|
| 可靠 | run_*、step_*、artifact_produced、tool_call | 不丢；阻塞生产者至多 1s，超时记 warning 并落日志（日志先行保证不丢历史） |
| 可合并 | llm_delta | 50ms 窗口合并；队列满时丢弃旧 delta、保留最新（前端只损失流畅度，不损失正确性） |

### 5.2 EventBus 与事件日志（EventLog）

- **EventBusHooks**（`runtime/event_hooks.py`）：`LifecycleHooks` 子类，把现有 hook 回调一对一翻译为 RunEvent：先 **append 事件日志**，再入内存队列。执行引擎零改动，经 `Workflow(hooks=CompositeHooks([..., EventBusHooks(bus, run_id)]))` 接入。
- **EventLog**：`output/runs/{run_id}/events.jsonl`，单行一事件，`O_APPEND` 写入。它是历史回放的唯一数据源，也是崩溃后 SSE 续传的数据源。
- **历史 = 实时**：前端渲染器只消费事件流；看历史 run 即顺序读 JSONL，看实时即 SSE。二者同一渲染路径。

### 5.3 RunManager 与运行状态机

**状态机**（`Checkpoint.status` 扩展取值；dataclass 字段本为自由 str，无迁移成本）：

```
pending ──→ running ──→ completed
               │   └──→ failed
               │   └──→ cancelling ──→ cancelled
               └──（进程重启，Reconciler）──→ interrupted ──(用户 resume)──→ running
```

| 状态 | 语义 | 持久化时机 |
|------|------|-----------|
| `pending` | 已创建未调度 | 创建时 |
| `running` | 执行中 | 每步成功后（现状保留） |
| `cancelling` | 已受理取消，等待当前 step 收尾 | 受理时 |
| `cancelled` | 已在 step 边界停止或硬中断落盘 | 停止时 |
| `interrupted` | 进程重启后发现的孤儿 running/cancelling | Reconciler 对账时 |
| `completed` / `failed` | 终态（现状） | 现状 |

**取消两阶段**：

| 模式 | 机制 | 适用 |
|------|------|------|
| `graceful`（默认） | 置 per-run 取消令牌；`Workflow._execute` 的 step 循环边界、LoopStep 迭代间、ParallelStep gather 处检查令牌，当前 step 完成后停止 → `cancelled` 落盘 | 大多数中断；状态绝对一致 |
| `immediate` | `asyncio.Task.cancel()`，`CancelledError` 沿既有穿透性传播；`_execute` **显式捕获 CancelledError** → `cancelled` 落盘 → 重抛 | LLM 长时间流式、工具卡死 |

> 修复点：现状下 cancel 会绕过 `except Exception` 的失败落盘分支（`workflow.py:254`），状态滞留 `running`。本设计在 `_execute` 增加 `except CancelledError` 显式分支，这是引擎层仅有的两处改动之一（另一处为令牌检查）。

**内存/持久划分**：

| 数据 | 位置 | 重启后 |
|------|------|--------|
| Task 句柄、取消令牌、SSE 订阅者 | RunManager 内存 | 丢弃（进程已死，语义正确） |
| 状态机、completed_steps、Context 快照 | CheckpointStore | 完整保留 |
| 事件流 | EventLog JSONL | 完整保留 |
| 产物与引用 | ArtifactStore + 事件日志 | 完整保留 |

### 5.4 崩溃恢复 Reconciler

Server 启动时执行（`server/lifecycle.py`）：

1. 扫描 CheckpointStore，所有 `running | cancelling` → 置 `interrupted`，附 `interrupted_reason: "process_restart"`、原 `updated_at`。
2. **不自动 resume**。依据：sink 类工具（wecom 通知、写库）有副作用，自动重放可能重复通知/重复写入；恢复决策必须留给用户。
3. 前端将 `interrupted` run 标记为"可恢复"，提供 `resume` / `discard` 操作；`resume` 走既有 checkpoint 机制，跳过 `completed_steps`。
4. 同时启动 **GCSweeper**（5.6）与事件日志完整性检查。

### 5.5 阻塞执行卸载（BlockingExecutor）

将 `tools/db.py` 的局部 `to_thread` 先例抽象为 Tool 级声明：

```python
class Tool(ABC):
    execution: str = "inline"   # "inline" | "thread" | "process"
```

| 模式 | 调度 | 契约 | 适用 |
|------|------|------|------|
| `inline`（默认） | 直接 `await` | 实现必须真异步（现状语义） | HTTP/DB(IO 异步化)/轻计算 |
| `thread` | `asyncio.to_thread` 经共享 `ThreadPoolExecutor`（大小可配 `executor_max_workers`，默认 4） | 无（同进程，Context 可传） | 同步 IO 库、reportlab/docx 渲染 |
| `process` | `ProcessPoolExecutor`（默认 2 worker） | params/result 仅 JSON 可序列化；**Context 不进子进程**，工具只收声明输入 | 纯 Python CPU 密集（大文本解析、图片处理） |

- `ToolStep` 按 `tool.execution` 分派；`ReportEngineTool` 标记 `execution = "thread"`。
- **事件分发不进执行器**：EventBusHooks 的队列操作全在主循环。
- 限度说明：GIL 下 `thread` 对纯 Python CPU 密集仅缓解（reportlab 的 C 段会释放 GIL，收益真实）；彻底隔离用 `process`，代价是序列化与进程启动开销。二者按工具标注共存，默认不改任何现有工具行为。

### 5.6 ArtifactStore 与一致性协议

**动机**：Context 大对象仅留 200 字符摘要，产物全文不可见；且事件流不能携带大产物（背压）。产物独立落盘、事件只带引用。

**目录布局**：`output/runs/{run_id}/artifacts/{step_id}/{artifact_id}`

**生产者写序（五步，严格顺序）**：

```
1. 写 {artifact_id}.tmp
2. flush + fsync + close
3. os.replace(.tmp → {artifact_id})   # 同文件系统内原子
4. append artifact_produced 事件 → events.jsonl
5. 入内存队列 → SSE 分发
```

**崩溃窗口分析**：

| 崩溃点 | 残留 | 后果 | 兜底 |
|--------|------|------|------|
| 1–3 之间 | `.tmp` 文件 | 无引用、无事件 | GC 直接删 |
| 3–4 之间 | 完整文件，无事件 | 孤儿（前端不可能拿到它的 URI） | GC 对账，宽限 24h 后删 |
| 4–5 之间 | 文件 + 日志，实时推送缺失 | 无不良状态 | SSE 重连 `Last-Event-ID` 从日志补齐 |

**保证**：客户端收到 `artifact_produced` 时，文件必然完整存在（rename 先于分发）——"URI 指向不存在文件"在协议上被消除。

**消费侧**：事件携带 `size + md5`，前端下载后可选校验；`GET /api/artifacts/...` 返回 404 时客户端重试一次（容忍极端挂载/时钟问题），二次 404 展示"产物不可用"。

**GCSweeper**：启动时 + 每 6 小时扫描；删除：所有 `.tmp` 残留；超过宽限期（默认 24h，可配）且未被任何事件日志/checkpoint 引用的孤儿文件。对账数据源 = 事件日志（`artifact_produced` 记录全集）。

### 5.7 编辑模型与双向序列化

**文档模型**：YAML 的 JSON 同构结构 = 前端唯一事实来源。编辑器直接操作 JSON，保存时渲染 YAML；不做"图模型 ⇄ YAML 双向实时同步"。

**UI 元数据**（引擎忽略但保留，round-trip 不丢）：

```yaml
ui:
  viewport: {x: 0, y: 0, zoom: 1}
steps:
  - id: analyze
    type: llm
    ui: {position: {x: 240, y: 120}, collapsed: false, note: ""}
```

依据：validator 为白名单逐项校验、不拒绝未知字段（已核实），`ui` 段天然兼容。

**反向序列化**：新增 `yaml/dumper.py`（独立于执行路径），将 SDK 对象树/文档模型导出为 YAML；与 `loader.py` 构成 round-trip 测试对。

**内省接口**（前端表单/面板的唯一数据源）：

| 接口 | 内容 |
|------|------|
| `GET /api/meta/step-types` | StepRegistry 全类型 + 各字段 schema + 容器嵌套规则（then/else/branches/body） |
| `GET /api/meta/tools` | ToolRegistry 全工具 + `param_model` JSON Schema + `role` + `execution` |
| `GET /api/meta/agents` | 当前定义内 agent 清单 + 字段 |

**诊断**：`ValidationError` 补 `severity: error|warning` 与 `code`（机器可读错误码）；`path`（`steps[2].then[1]`）已满足节点定位，前端据此在画布标注。

**画布形态**：受限画布 = 顶层线性流 + condition/loop/parallel 容器块嵌套，与执行模型一一对应；**不做自由 DAG**（依据：checkpoint 的 `completed_steps` 顺序语义、端口静态连线校验均建立在序模型上，DAG 化将推翻两者，违背"轻量、稳定"）。

### 5.8 Server API 与 SSE

FastAPI 可选 extra（`agentkit[server]`），懒加载模式照抄 `report_engine_sdk/adapters/api_router.py`：

| 端点 | 说明 |
|------|------|
| `PUT /api/workflows/{name}` | 保存定义（先经 validator，失败返回 diagnostics） |
| `POST /api/workflows/validate` | 返回结构化 diagnostics[] |
| `GET /api/meta/step-types` `/tools` `/agents` | 内省（5.7） |
| `POST /api/workflows/{name}/runs` | 启动 run（body: inputs, run_id?） |
| `GET /api/runs?workflow=` | run 列表（含 `interrupted`） |
| `GET /api/runs/{run_id}` | 状态 + traces 摘要 + 产物清单 |
| `POST /api/runs/{run_id}/cancel?mode=graceful\|immediate` | 中断 |
| `POST /api/runs/{run_id}/resume` | 从 interrupted/failed 恢复 |
| `GET /api/runs/{run_id}/events` | SSE；`Last-Event-ID` 从事件日志补齐后接 live |
| `GET /api/runs/{run_id}/artifacts` | 产物清单 |
| `GET /api/artifacts/{run_id}/{artifact_id}` | 产物内容/下载（支持 Range） |

**SSE 而非 WebSocket 的依据**：运行状态为单向推送，中断指令走普通 POST；SSE 更轻、浏览器原生自动重连（`Last-Event-ID` 即 `seq`）、对代理友好。

**CLI**：`agentkit serve --dir ./workflows --port 8000` 一键起服务并托管前端静态页。

### 5.9 安全与配额

| 项 | 策略 |
|----|------|
| 绑定 | 默认 `127.0.0.1`；显式 `--host 0.0.0.0` 才对外 |
| 鉴权 | 静态 bearer token（env `AGENTKIT_SERVER_TOKEN`）；未配置时仅本地可访问 |
| CORS | 默认关闭，按 origin 白名单开启 |
| 机密防护 | `${ENV}` 占位符在 API 下发给编辑器时**保留原样、永不回传解析值**（防止 API key 经编辑器泄漏；loader 解析仍发生在服务端运行时） |
| 表达式 | 模板与 `when` 表达式维持既有 AST 安全求值（不 eval） |
| 配额 | 单 run 产物总量、单 artifact 大小、事件日志长度上限可配；超限拒绝并记事件 |

---

## 6. 存储布局

```
output/
└── runs/
    └── {run_id}/
        ├── checkpoint.json        # CheckpointStore（Local 实现，现状）
        ├── events.jsonl           # EventLog（seq 单调，回放/续传数据源）
        └── artifacts/
            └── {step_id}/
                └── {artifact_id}  # 原子 rename 后的最终产物
```

工作流定义仍以 YAML 文件为唯一事实来源（`--dir` 指定目录），Server 不做定义的数据库化。

---

## 7. 分期实施路线

| 期 | 内容 | 出口标准 |
|----|------|----------|
| **P0 引擎原语 + 测试** | ① 引擎测试补齐（6 种 Step、重试/超时、checkpoint/resume、hook 触发序列）；② 状态机扩展 + 取消两阶段（含 `_execute` 的 CancelledError 显式落盘）；③ RunEvent v1 + EventBusHooks + EventLog；④ ArtifactStore 写序协议；⑤ Tool.execution + ReportEngineTool 标 thread | 测试绿；`cancel(graceful/immediate)` 后状态正确落盘并可 resume；事件与 hook 序列一一对应 |
| **P1 服务层** | FastAPI 壳、SSE（Last-Event-ID 续传）、Reconciler、GCSweeper、内省接口、安全基线 | kill -9 后重启：running→interrupted→resume 成功且 sink 不重复执行；阻塞工具执行期间 SSE 延迟 < 200ms |
| **P2 前端** | 受限画布（线性+容器嵌套）、属性表单（内省驱动）、运行面板（节点着色 + LLM 流式 + attempt 重置）、产物面板、历史回放 | 编辑→校验→运行→观察→中断→恢复全链路可用；历史 run 回放与实时渲染一致 |
| **P3 增强（可后置）** | pause/resume、断点/单步、定义 Git 版本化、多用户 | —— |

---

## 8. 验证方案（三风险对应）

| 风险 | 验证测试 |
|------|----------|
| 3.1 阻塞 | run A 含 `execution=thread` 的 5s 阻塞工具；并发 run B 开启流式 LLM；断言 B 的 SSE delta 端到端延迟 p95 < 200ms |
| 3.2 易失 | run 执行中 `kill -9` Server → 重启 → 断言 run 为 `interrupted` → `resume` → 断言从断点续跑、已完成 sink 工具未重复执行（MockToolHooks 计数） |
| 3.3 一致性 | 故障注入：rename 后 kill → 重启 GC → 断言孤儿被回收；事件日志 append 后、分发前 kill → SSE 重连 → 断言事件由日志补齐、无丢失无重复（seq 校验） |

---

## 9. 非目标（明确不做）

| 不做 | 理由 |
|------|------|
| 多实例 active-active / 水平扩展 | 与"轻量单机"定位冲突；Redis CheckpointStore 已留演进接口，未来需 lease/leader 选举时单独立项 |
| 分布式事件总线（Kafka/Redis Stream） | 单机 JSONL 事件日志已满足回放与续传；引入外部 MQ 违背轻量 |
| 事务协调器（2PC/ saga） | 5.6 的顺序协议 + 原子 rename + 对账 GC 已达最终一致，工程成本低一个量级 |
| 自由 DAG 编辑与执行 | 推翻 checkpoint 顺序语义与端口静态校验，收益不明、改动面巨大 |
| 运行中热修改定义 | 运行绑定启动时的定义快照；编辑仅影响下一次 run——语义简单、无竞态 |
| 多租户/权限模型 | P3 之后按真实需求评估 |
