# P1 服务层执行方案与验收标准

> 对齐 `docs/visualization-design.md` §7。P1 出口标准：**kill -9 后重启 → running→interrupted→resume 成功且 sink 不重复执行；阻塞工具执行期间 SSE 延迟 p95 < 200ms**。

## 目录

- [依赖关系图](#依赖关系图)
- [阶段 A：运行时控制层](#阶段-a运行时控制层)
  - [A1：EventBusHooks cancelled 补丁](#a1eventbushooks-cancelled-补丁)
  - [A2：RunManager](#a2runmanager)
- [阶段 B：崩溃恢复](#阶段-b崩溃恢复)
  - [B1：Reconciler](#b1reconciler)
- [阶段 C：配置与基础设施](#阶段-c配置与基础设施)
  - [C1：config 配置项](#c1config-配置项)
  - [C2：server/settings.py](#c2serversettingspy)
  - [C3：server/security.py](#c3serversecuritypy)
- [阶段 D：诊断增强](#阶段-d诊断增强)
  - [D1：validator severity/code](#d1validator-severitycode)
- [阶段 E：Server 路由层](#阶段-eserver-路由层)
  - [E1：workflows routes](#e1workflows-routes)
  - [E2：runs routes](#e2runs-routes)
  - [E3：SSE 适配](#e3sse-适配)
  - [E4：artifacts routes + GCSweeper 调度](#e4artifacts-routes--gcsweeper-调度)
- [阶段 F：集成](#阶段-f集成)
  - [F1：server/app.py + lifespan](#f1serverapppy--lifespan)
  - [F2：cli serve + pyproject](#f2cli-serve--pyproject)
- [阶段 G：测试验证](#阶段-g测试验证)
- [附录：模块依赖方向](#附录模块依赖方向)

---

## 依赖关系图

```
A1 (EventBusHooks 补丁) ──┐
                          ▼
A2 (RunManager) ──────────┼──► B1 (Reconciler)
                          │
C1 (config) ──► C2 (settings) ──► C3 (security)
                                          │
D1 (validator) ───────────────────────────┤
                                          ▼
                    E1 (workflows routes) ◄── D1
                    E2 (runs routes)     ◄── A2, C3
                    E3 (SSE)             ◄── A2
                    E4 (artifacts)       ◄── B1 (GCSweeper 调度)
                                          │
                                          ▼
                              F1 (app + lifespan) ◄── 全部
                              F2 (cli + pyproject) ◄── F1
                                          │
                                          ▼
                              G (测试验证)
```

建议按 A → B → C → D → E → F → G 顺序实施。A/B 可并行于 C/D（无依赖）。

---

## 阶段 A：运行时控制层

补全 P0 EventBusHooks 注释明示的缺口：`run_created / run_cancelling / run_cancelled` 无人发；无内存注册表。

### A1：EventBusHooks cancelled 补丁

**目标**：消除 cancelled 状态下 `after_workflow` 误发 `run_completed` 的问题。

**问题根因**：[runtime/event_hooks.py:131-145](file:///d:/Dev/LiteFlow/src/agentkit/runtime/event_hooks.py#L131-145) 的 `after_workflow` 基于 `_workflow_failed` 标志只发 `run_completed` 或 `run_failed`。但 [core/workflow.py:340-346](file:///d:/Dev/LiteFlow/src/agentkit/core/workflow.py#L340-346) 的 `finally` 在 cancelled 路径（graceful return 与 immediate raise）都会调用 `after_workflow`，导致 cancelled 时误发 `run_completed`。

**改动文件**：`src/agentkit/runtime/event_hooks.py`

**接口契约**：

```python
class EventBusHooks(LifecycleHooks):
    def __init__(self, bus: EventBus, run_id: str = "") -> None:
        self._bus: EventBus = bus
        self._run_id: str = run_id or bus.run_id
        self._workflow_failed: bool = False
        self._workflow_error: str | None = None
        self._workflow_cancelling: bool = False  # 【新增】

    # 【新增方法】
    def mark_cancelling(self) -> None:
        """标记当前 run 进入 cancelling 状态。

        由 RunManager.cancel() 在受理取消时调用。
        调用后 after_workflow 将跳过 run_completed/run_failed 的发送
        （由 RunManager 在 Task 收尾时显式发 run_cancelled）。
        """
        self._workflow_cancelling = True
```

**改动点**：`after_workflow` 开头增加判断：

```python
async def after_workflow(self, wf, ctx, result) -> None:
    # cancelled 时跳过：由 RunManager 发 run_cancelled
    if self._workflow_cancelling:
        return
    # 原有逻辑不变
    if self._workflow_failed:
        ...  # run_failed
    else:
        ...  # run_completed
```

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_cancelled_skips_after_workflow` | `mark_cancelling()` 后 `after_workflow` 不发任何事件；未标记时行为不变 |
| `test_normal_completed_unchanged` | 无 `mark_cancelling` 时 `after_workflow` 照发 `run_completed` |
| `test_failed_unchanged` | `on_step_error` 设置 `_workflow_failed` 后 `after_workflow` 照发 `run_failed` |

---

### A2：RunManager

**目标**：运行时控制层核心——状态机编排、取消编排、内存注册表。

**新增文件**：`src/agentkit/runtime/run_manager.py`

**依赖**：`core/workflow.py`（`Workflow.run/resume`）、`core/cancel.py`（`CancelToken`）、`core/checkpoint.py`（`CheckpointStore/Checkpoint/RunStatus`）、`runtime/event.py`（`EventBus/EventLog/EventType`）、`runtime/event_hooks.py`（`EventBusHooks`）、`runtime/artifact.py`（`ArtifactStore`）、`config.py`（`get_default`）。

**接口契约**：

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class RunHandle:
    """单个 run 的内存句柄（控制面，重启丢弃）。"""
    run_id: str
    workflow_name: str
    task: "asyncio.Task"              # asyncio.Task[WorkflowResult]
    cancel_token: CancelToken
    event_bus: EventBus
    event_log: EventLog
    artifact_store: ArtifactStore
    hooks: EventBusHooks
    started_at: float


@dataclass
class RunSummary:
    """run 列表项（合并内存 + checkpoint 状态）。"""
    run_id: str
    workflow_name: str
    status: str                       # 来自 checkpoint
    started_at: float
    updated_at: float
    is_active: bool                  # 是否在内存注册表中（可 cancel）
    error: str | None = None


class RunManager:
    """运行控制层：状态机编排 + 取消编排 + 内存注册表。

    职责：
        - start(): 创建 EventBus/EventLog/ArtifactStore + checkpoint(running)
          → 发 run_created → asyncio.create_task(workflow.run) → 注册 RunHandle
        - cancel(mode): graceful 触发令牌 / immediate task.cancel()
          → 发 run_cancelling → await task → 发 run_cancelled
        - resume(): 从 interrupted/failed 恢复，调 workflow.resume(run_id)
        - 内存注册表 dict[run_id, RunHandle]，重启丢弃（语义正确）

    不侵入 _execute：经 hooks + Workflow 公开 API 接入。

    Args:
        checkpoint_store:  检查点存储；None 时用 LocalCheckpointStore。
        base_dir:           产物与事件日志根目录，默认 "output/runs"。
    """

    def __init__(
        self,
        checkpoint_store: "CheckpointStore | None" = None,
        *,
        base_dir: str = "output/runs",
    ) -> None: ...

    async def start(
        self,
        workflow: "Workflow",
        inputs: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> str:
        """启动一个 run。

        流程：
            1. 分配 run_id（None 时 Checkpoint.new 自动生成）
            2. 构造 EventBus + EventLog + ArtifactStore + EventBusHooks
            3. 把 EventBusHooks 注入 workflow（CompositeHooks 合并原有 hooks）
            4. 发 run_created 事件
            5. asyncio.create_task(workflow.run(inputs, run_id, cancel_token))
            6. 注册 RunHandle 到内存

        Returns:
            str: run_id
        """
        ...

    async def cancel(self, run_id: str, *, mode: str = "graceful") -> None:
        """取消一个 run。

        Args:
            run_id: 目标 run。
            mode:   "graceful"（默认）触发令牌，当前 step 完成后停止；
                    "immediate" 调 task.cancel() 注入 CancelledError。

        Raises:
            KeyError: run_id 不在内存注册表（已结束或不存在）。
            ValueError: mode 非法。
        """
        ...

    async def resume(self, run_id: str) -> str:
        """从 interrupted/failed 恢复执行。

        调用 workflow.resume(run_id)，复用既有 checkpoint 跳过 completed_steps。
        sink 类工具不会重复执行（resume 跳过已完成的 step）。

        Raises:
            KeyError: run_id 不在 checkpoint store。
            ValueError: run 状态非 interrupted/failed。
        """
        ...

    def get(self, run_id: str) -> RunHandle | None:
        """查内存注册表（仅活跃 run）。"""
        ...

    async def list_runs(
        self, workflow_name: str | None = None
    ) -> list[RunSummary]:
        """列出所有 run（合并 checkpoint 状态 + 内存活跃标记）。"""
        ...

    async def shutdown(self) -> None:
        """关闭所有活跃 Task 与 EventBus（进程退出时调用）。"""
        ...
```

**实现要点**：

1. **hooks 注入**：`start()` 时用 `CompositeHooks` 把原 workflow hooks + `EventBusHooks` 合并。需要给 `Workflow` 加 `hooks` 可变属性或构造时传入。若 `Workflow.__init__` 不支持后改 hooks，则 `start()` 内克隆 workflow 或在构造时注入——优先检查 Workflow 是否支持 hooks 替换，不支持则在 `start()` 接收已构造好的 Workflow（调用方负责注入 hooks）。

2. **cancel 收尾**：两种模式都 `await handle.task`（吞 `asyncio.CancelledError`），然后检查 `WorkflowResult.status`，若为 `cancelled` 则发 `run_cancelled`。**保证 `run_cancelled` 只发一次**。

3. **ArtifactStore 配置**：从 `config` 读取 `server_artifact_max_size/max_total`（C1 定义）。

4. **list_runs 合并**：`checkpoint_store.list_runs()` 取全量 run_id，每个 `load` 取状态；内存注册表标记 `is_active`。

5. **shutdown**：遍历内存注册表，`task.cancel()` 后 `await asyncio.gather(*tasks, return_exceptions=True)`，关闭所有 EventBus。

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_start_emits_run_created` | `start()` 后 EventLog 有 `run_created` 事件，seq=1 |
| `test_start_registers_handle` | `get(run_id)` 返回非 None，含 task/cancel_token/event_bus |
| `test_graceful_cancel` | `cancel(mode="graceful")` 后 EventLog 有 `run_cancelling` + `run_cancelled`；checkpoint.status == cancelled；无 `run_completed` 误发 |
| `test_immediate_cancel` | `cancel(mode="immediate")` 同上，且 Task 收到 CancelledError |
| `test_cancel_idempotent` | 重复 `cancel()` 不重复发 `run_cancelled` |
| `test_cancel_unknown_run` | `cancel("不存在")` 抛 KeyError |
| `test_resume_from_interrupted` | checkpoint 置 interrupted → `resume()` → 跳过 completed_steps，MockToolHooks 计数 sink 未增加 |
| `test_resume_from_failed` | checkpoint 置 failed → `resume()` → 从失败 step 续跑 |
| `test_resume_wrong_status` | checkpoint 为 completed → `resume()` 抛 ValueError |
| `test_list_runs` | 多个 run 后 `list_runs()` 返回全量，is_active 标记正确 |
| `test_shutdown_cleans_tasks` | `shutdown()` 后所有 task done，EventBus closed |
| `test_events_seq_monotonic` | 同一 run 所有事件 seq 严格递增 |

---

## 阶段 B：崩溃恢复

### B1：Reconciler

**目标**：Server 启动时对账，把僵尸 running/cancelling 标记为 interrupted。

**新增文件**：`src/agentkit/runtime/reconciler.py`

**依赖**：`core/checkpoint.py`、`runtime/run_manager.py`（`RunManager`）、`runtime/event.py`（`EventBus/EventLog/EventType`）、`runtime/artifact.py`（`GCSweeper`）。

**接口契约**：

```python
@dataclass
class ReconcileResult:
    """对账结果统计。"""
    interrupted_count: int          # 标记为 interrupted 的 run 数
    gc_stats: dict[str, int]       # GCSweeper.sweep_once() 返回值
    event_log_corrupt: int         # 损坏事件日志文件数


class Reconciler:
    """启动对账（对齐 §5.4）。

    Args:
        checkpoint_store:  检查点存储。
        base_dir:          产物与事件日志根目录。
    """

    def __init__(
        self,
        checkpoint_store: "CheckpointStore",
        *,
        base_dir: str = "output/runs",
    ) -> None: ...

    async def reconcile(self) -> ReconcileResult:
        """执行一次对账。

        流程：
            1. checkpoint_store.list_runs() 取全量 run_id
            2. load 每个 checkpoint，status in {running, cancelling}
               → 置 interrupted + interrupted_reason="process_restart"
               → save 回 checkpoint_store
               → 构造 EventLog，append run_interrupted 事件
            3. 不自动 resume（§5.4：sink 副作用）
            4. GCSweeper.sweep_once() 清理孤儿
            5. 统计损坏事件日志（无法解析的 events.jsonl）

        Returns:
            ReconcileResult: 统计结果。
        """
        ...
```

**实现要点**：

1. **不自动 resume**：依据 §5.4，sink 类工具（wecom 通知、写库）有副作用，自动重放可能重复通知/写入。恢复决策留给用户（前端 `POST /resume`）。

2. **run_interrupted 事件**：对每个 interrupted run，构造临时 `EventLog`（不建 EventBus，因为 run 已死无订阅者），`append` 一个 `run_interrupted` 事件。这样前端 SSE 续传时能看到状态变更。

3. **GCSweeper 调度**：`reconcile()` 内调 `GCSweeper.sweep_once()`（P0 已实现 [runtime/artifact.py:358-408](file:///d:/Dev/LiteFlow/src/agentkit/runtime/artifact.py#L358-408)），清理 .tmp 残留与孤儿文件。

4. **事件日志完整性**：扫描 `{base_dir}/*/events.jsonl`，统计无法解析的行数（记 warning 不中断）。

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_reconcile_running_to_interrupted` | checkpoint status=running → reconcile 后=interrupted；interrupted_reason 字段存在 |
| `test_reconcile_cancelling_to_interrupted` | checkpoint status=cancelling → reconcile 后=interrupted |
| `test_reconcile_completed_unchanged` | checkpoint status=completed → reconcile 后不变 |
| `test_reconcile_interrupted_emits_event` | reconcile 后 events.jsonl 有 `run_interrupted` 事件 |
| `test_reconcile_no_auto_resume` | reconcile 后 run 仍为 interrupted，未自动执行（MockToolHooks 计数为 0） |
| `test_reconcile_gc_sweeps_tmp` | 预置 .tmp 文件 → reconcile 后删除 |
| `test_reconcile_gc_sweeps_orphan` | 预置孤儿文件（超宽限期）→ reconcile 后删除；宽限期内不删 |
| `test_reconcile_corrupt_log_counted` | 预置损坏 events.jsonl → reconcile 不中断，event_log_corrupt 计数正确 |

---

## 阶段 C：配置与基础设施

### C1：config 配置项

**目标**：在 `config.py` 的 `_DEFAULTS` dict 追加 server 相关配置。

**改动文件**：`src/agentkit/config.py`（[第 39-80 行 `_DEFAULTS`](file:///d:/Dev/LiteFlow/src/agentkit/config.py#L39-80)）

**追加内容**：

```python
_DEFAULTS: dict[str, Any] = {
    # ... 现有项不变 ...

    # ==================== Server（P1 新增）====================
    # Server 绑定地址。默认 127.0.0.1 仅本地；--host 0.0.0.0 才对外。
    # 被 server.app 使用。
    "server_host": "127.0.0.1",
    # Server 端口。被 server.app 使用。
    "server_port": 8000,
    # Server 鉴权 bearer token；空字符串=仅本地可访问。
    # 也可通过 env AGENTKIT_SERVER_TOKEN 设置。被 server.security 使用。
    "server_token": "",
    # CORS 允许的 origin 列表；空列表=关闭 CORS。被 server.security 使用。
    "server_cors_origins": [],
    # EventBus per-subscriber 队列容量。被 runtime.event 使用。
    "server_event_queue_size": 1000,
    # 单 run 事件日志最大事件数；超限拒绝并记事件。被 server.routes.runs 使用。
    "server_event_log_max_events": 100000,
    # 单 artifact 最大字节（100MB）。被 runtime.artifact 使用。
    "server_artifact_max_size": 100 * 1024 * 1024,
    # 单 run 产物总量最大字节（1GB）。被 runtime.artifact 使用。
    "server_artifact_max_total": 1024 * 1024 * 1024,
    # GCSweeper 定时扫描间隔（秒，6h）。被 server.app lifespan 使用。
    "server_gc_interval_seconds": 6 * 3600,
    # GCSweeper 孤儿文件宽限期（秒，24h）。被 runtime.artifact 使用。
    "server_gc_orphan_grace_seconds": 24 * 3600,
}
```

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_get_default_server_keys` | `get_default("server_host")` 等全部可读，返回默认值 |
| `test_set_default_server_keys` | `set_default("server_port", 9000)` 后 `get_default` 返回 9000 |
| `test_reset_default_server_keys` | `reset_default("server_port")` 后回退到 8000 |
| `test_unknown_key_raises` | `get_default("server_unknown")` 抛 KeyError |

---

### C2：server/settings.py

**目标**：从 config 读取 server 配置，封装为 pydantic Settings 供 FastAPI 依赖注入。

**新增文件**：`src/agentkit/server/settings.py`

**注意**：本模块 **不用** `from __future__ import annotations`（Pydantic + FastAPI 局部类解析陷阱，见 [api_router.py:12-18](file:///d:/Dev/LiteFlow/src/report_engine_sdk/adapters/api_router.py#L12-18) 注释）。

**接口契约**：

```python
# 不用 from __future__ import annotations

from dataclasses import dataclass
from agentkit.config import get_default


@dataclass
class ServerSettings:
    """Server 配置快照（启动时从 config 读取一次）。"""
    host: str
    port: int
    token: str
    cors_origins: list[str]
    event_queue_size: int
    event_log_max_events: int
    artifact_max_size: int
    artifact_max_total: int
    gc_interval_seconds: float
    gc_orphan_grace_seconds: float

    @classmethod
    def from_config(cls) -> "ServerSettings":
        """从 config.get_default 读取所有 server_* 配置。"""
        return cls(
            host=get_default("server_host"),
            port=get_default("server_port"),
            token=get_default("server_token") or _read_env_token(),
            cors_origins=list(get_default("server_cors_origins")),
            event_queue_size=int(get_default("server_event_queue_size")),
            event_log_max_events=int(get_default("server_event_log_max_events")),
            artifact_max_size=int(get_default("server_artifact_max_size")),
            artifact_max_total=int(get_default("server_artifact_max_total")),
            gc_interval_seconds=float(get_default("server_gc_interval_seconds")),
            gc_orphan_grace_seconds=float(get_default("server_gc_orphan_grace_seconds")),
        )


def _read_env_token() -> str:
    """从 env AGENTKIT_SERVER_TOKEN 读取 token。"""
    import os
    return os.environ.get("AGENTKIT_SERVER_TOKEN", "")
```

**实现要点**：用 `dataclass` 而非 pydantic `BaseSettings`，避免引入额外依赖。`token` 优先读 config，其次读环境变量。

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_from_config_defaults` | `ServerSettings.from_config()` 返回所有默认值 |
| `test_from_config_overrides` | `set_default` 覆盖后 `from_config()` 反映新值 |
| `test_token_from_env` | 设 `AGENTKIT_SERVER_TOKEN=xxx` 后 token 读取正确 |

---

### C3：server/security.py

**目标**：bearer token 鉴权 + CORS + 绑定校验。

**新增文件**：`src/agentkit/server/security.py`

**注意**：不用 `from __future__ import annotations`。

**接口契约**：

```python
# 不用 from __future__ import annotations


def create_security_middleware(settings):
    """创建安全中间件链。

    返回一个 list，可被 FastAPI app.middleware 注册：
        - bearer token 校验（settings.token 非空时启用）
        - CORS（settings.cors_origins 非空时启用）
        - 绑定校验（非 127.0.0.1 请求 + 无 token → 401）

    懒加载：本函数内部 import fastapi / starlette。
    """
    try:
        from fastapi import Request, HTTPException
        from starlette.middleware.base import BaseHTTPMiddleware
    except ImportError as e:
        raise ImportError(
            "Server 功能需要 fastapi: pip install agentkit[server]"
        ) from e
    ...


def verify_token(settings) -> "callable":
    """返回 FastAPI 依赖函数，校验 Authorization: Bearer <token>。

    settings.token 为空时：
        - 请求来自 127.0.0.1/localhost → 放行
        - 请求来自其他地址 → 401
    settings.token 非空时：
        - Bearer 匹配 → 放行
        - 不匹配或缺失 → 401
    """
    ...
```

**实现要点**：

1. **懒加载**：`import fastapi` 在函数内部，模块顶层不依赖 fastapi。
2. **本地放行**：token 为空时仅允许 `127.0.0.1` / `::1` / `localhost`。
3. **CORS**：用 `starlette.middleware.cors.CORSMiddleware`，origins 来自 settings。

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_no_token_localhost_ok` | token 空 + 请求来自 127.0.0.1 → 放行 |
| `test_no_token_remote_401` | token 空 + 请求来自 10.0.0.1 → 401 |
| `test_token_match_ok` | token=abc + Bearer abc → 放行 |
| `test_token_mismatch_401` | token=abc + Bearer xyz → 401 |
| `test_token_missing_401` | token=abc + 无 Authorization 头 → 401 |
| `test_cors_enabled` | cors_origins=["http://localhost:3000"] → 预检请求返回允许头 |
| `test_cors_disabled` | cors_origins=[] → 无 CORS 头 |

---

## 阶段 D：诊断增强

### D1：validator severity/code

**目标**：`ValidationError` 补 `severity` 与 `code` 字段，前端可机器可读。

**改动文件**：`src/agentkit/yaml/validator.py`（[第 44-57 行 ValidationError](file:///d:/Dev/LiteFlow/src/agentkit/yaml/validator.py#L44-57)）

**改动内容**：

```python
@dataclass
class ValidationError:
    """单个校验错误。

    Attributes:
        path:      错误所在路径（如 steps[2] / steps[0].then[1]）。
        message:   错误描述。
        severity:  严重级别，"error"（默认）或 "warning"。
        code:      机器可读错误码（如 "step.type_unknown"），空字符串=无码。
    """

    path: str
    message: str
    severity: str = "error"       # 【新增】默认 error，向后兼容
    code: str = ""                # 【新增】默认空，向后兼容

    def __str__(self) -> str:
        prefix = "错误" if self.severity == "error" else "警告"
        code_str = f" [{self.code}]" if self.code else ""
        return f"{prefix} [{self.path}]{code_str} {self.message}"
```

**ValidationReport 增强**（向后兼容，仅新增方法）：

```python
@dataclass
class ValidationReport:
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)

    # 【新增】统一诊断列表（合并 errors + warnings，每条带 severity）
    @property
    def diagnostics(self) -> list[ValidationError]:
        """所有诊断项（errors 在前，warnings 在后）。"""
        return list(self.errors) + list(self.warnings)

    def to_api_response(self) -> dict:
        """序列化为 API 响应格式。"""
        return {
            "is_valid": self.is_valid,
            "diagnostics": [
                {
                    "path": d.path,
                    "message": d.message,
                    "severity": d.severity,
                    "code": d.code,
                }
                for d in self.diagnostics
            ],
        }
```

**校验函数填充 code**：在 `validate_workflow` 内各 `report.errors.append(...)` 处补 `code=`。建议的 code 命名规范（`<类别>.<具体>`）：

| code | 触发条件 |
|------|---------|
| `root.name_missing` | 顶层无 name |
| `root.steps_missing` | 顶层无 steps |
| `agent.provider_unknown` | provider 引用不存在 |
| `step.type_unknown` | Step type 未注册 |
| `step.id_duplicate` | 同级 id 重复 |
| `step.output_duplicate` | 同级 output key 重复 |
| `llm.agent_unknown` | LLMStep agent 引用不存在 |
| `llm.output_format_invalid` | output_format 非法 |
| `tool.unknown` | ToolStep tool 引用不存在（warning） |
| `skill.unknown` | SkillStep skill 引用不存在（warning） |
| `condition.when_missing` | ConditionStep 无 when |
| `loop.iter_until_conflict` | iter 与 until 同时存在或缺 |
| `loop.output_mode_invalid` | output_mode 非法 |
| `parallel.branches_empty` | branches 为空 |
| `port.name_duplicate` | 端口名重复 |
| `port.type_schema_conflict` | type 与 schema 同时声明 |
| `port.output_outputs_conflict` | output 与 outputs 同时声明 |
| `port.from_unknown` | from 来源不存在（warning） |
| `port.type_incompatible` | 上下游类型不兼容（warning） |
| `template.ghost_dependency` | 模板引用未声明变量（warning） |

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_validation_error_defaults` | `ValidationError("p", "m")` 默认 severity=error, code="" |
| `test_severity_warning` | `ValidationError("p", "m", severity="warning")` 正确 |
| `test_diagnostics_property` | report.errors=[e1], warnings=[w1] → diagnostics=[e1, w1] |
| `test_to_api_response` | 含 is_valid + diagnostics 列表，每条有 path/message/severity/code |
| `test_validate_fills_codes` | 各类错误触发后 code 字段非空且符合命名规范 |
| `test_backward_compat` | 旧代码 `ValidationError("p", "m")` 不传 severity/code 仍正常工作 |

---

## 阶段 E：Server 路由层

### E1：workflows routes

**目标**：工作流 CRUD + 校验 + 内省接口。

**新增文件**：`src/agentkit/server/routes/workflows.py`

**依赖**：`yaml/loader.py`、`yaml/validator.py`（D1 增强后）、`steps/base.py`（StepRegistry）、`tools/base.py`（ToolRegistry）、`core/agent.py`（AgentRegistry）。

**接口契约**：

```python
# 不用 from __future__ import annotations

def create_workflow_routes(workflow_dir: str, prefix: str = "/api"):
    """创建工作流路由。

    懒加载 fastapi。返回 APIRouter。

    端点：
        PUT  /api/workflows/{name}        保存定义（YAML 文本 → 文件）
        POST /api/workflows/validate      校验，返回 diagnostics[]
        GET  /api/meta/step-types         StepRegistry 全类型 + schema
        GET  /api/meta/tools              ToolRegistry 全工具 + schema
        GET  /api/meta/agents             YAML agents 段原文（ENV 占位符保留）
    """
    try:
        from fastapi import APIRouter, HTTPException
        from pydantic import BaseModel
    except ImportError as e:
        raise ImportError("Server 需要 fastapi: pip install agentkit[server]") from e
    ...
```

**各端点实现要点**：

1. **`PUT /api/workflows/{name}`**：
   - 请求体：YAML 文本（`text/yaml` 或 `application/json` 含 `{yaml: "..."}`）
   - 先 `yaml.safe_load` 解析 → `validate_workflow` → 若有 errors 返回 400 + diagnostics
   - 校验通过 → 写文件 `{workflow_dir}/{name}.yaml`
   - **保留 `${ENV}` 占位符原文**，不在此解析

2. **`POST /api/workflows/validate`**：
   - 请求体：YAML 文本或 dict
   - 返回 `ValidationReport.to_api_response()`（D1 增强）

3. **`GET /api/meta/step-types`**：
   - `_GLOBAL_STEP_REGISTRY.list()` 遍历
   - 每个类型用 `inspect.signature(cls.__init__)` 提取参数 schema
   - 容器嵌套规则：ConditionStep→`then/else`、LoopStep→`body`(YAML key `step`)、ParallelStep→`branches`
   - 返回 `{types: [{name, fields: [{name, type, required, default}], container_fields: [...]}]}`

4. **`GET /api/meta/tools`**：
   - `list_tools()` 遍历，`get_tool(name)` 取实例
   - 每个返回 `{name, role, execution, description, param_model_schema}`
   - `param_model_schema = tool.param_model.model_json_schema() if tool.param_model else None`

5. **`GET /api/meta/agents`**：
   - 读 `{workflow_dir}/*.yaml`，解析 `agents:` 段
   - **ENV 占位符保留原文**，不回传解析值（§5.9 机密防护）
   - 返回 `{agents: [{name, model, provider, system, ...}]}`（system 等字段含 `${ENV}` 原样）

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_put_workflow_valid` | 有效 YAML → 200，文件创建 |
| `test_put_workflow_invalid` | 无效 YAML → 400 + diagnostics 含 path/severity/code |
| `test_put_workflow_env_preserved` | 含 `${API_KEY}` 的 YAML → 保存后占位符原样保留 |
| `test_validate_returns_diagnostics` | 校验结果含 is_valid + diagnostics 列表 |
| `test_meta_step_types` | 返回所有已注册 Step 类型，含字段 schema |
| `test_meta_step_types_container` | ConditionStep 含 then/else 容器字段 |
| `test_meta_tools` | 返回所有工具，含 role/execution/param_model_schema |
| `test_meta_agents_env_preserved` | agents 段含 `${ENV}` → 返回值保留原文 |

---

### E2：runs routes

**目标**：run CRUD + cancel + resume。

**新增文件**：`src/agentkit/server/routes/runs.py`

**依赖**：`runtime/run_manager.py`（A2）、`yaml/loader.py`。

**接口契约**：

```python
def create_run_routes(run_manager, workflow_dir: str, prefix: str = "/api"):
    """创建 run 路由。

    懒加载 fastapi。返回 APIRouter。

    端点：
        POST /api/workflows/{name}/runs       启动 run
        GET  /api/runs?workflow=              run 列表
        GET  /api/runs/{run_id}               状态 + traces + 产物清单
        POST /api/runs/{run_id}/cancel?mode=  中断
        POST /api/runs/{run_id}/resume        恢复
    """
    ...
```

**各端点实现要点**：

1. **`POST /api/workflows/{name}/runs`**：
   - 读 `{workflow_dir}/{name}.yaml` → `load_workflow_from_dict` 构造 Workflow
   - body：`{inputs: {...}, run_id: "..."}`（run_id 可选）
   - 调 `run_manager.start(workflow, inputs, run_id)` → 返回 `{run_id}`

2. **`GET /api/runs?workflow=`**：
   - `run_manager.list_runs(workflow_name)` → 返回 `[{run_id, workflow_name, status, started_at, updated_at, is_active, error}]`

3. **`GET /api/runs/{run_id}`**：
   - `checkpoint_store.load(run_id)` 取状态 + completed_steps
   - `EventLog(run_id).read_from()` 取 traces 摘要（step_finished 事件）
   - `ArtifactStore` 从事件日志重建 refs → 产物清单
   - 返回 `{run_id, workflow_name, status, completed_steps, traces: [...], artifacts: [...]}`

4. **`POST /api/runs/{run_id}/cancel?mode=`**：
   - `run_manager.cancel(run_id, mode=mode)`
   - mode 默认 graceful

5. **`POST /api/runs/{run_id}/resume`**：
   - `run_manager.resume(run_id)` → 返回新 run_id（或同 run_id）

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_start_run` | POST 后返回 run_id，checkpoint status=running |
| `test_start_run_with_inputs` | inputs 正确传入 Context |
| `test_start_run_custom_id` | 指定 run_id → 返回同一 run_id |
| `test_list_runs` | 多个 run → 返回全量，含 is_active 标记 |
| `test_list_runs_filter_workflow` | `?workflow=xxx` 过滤正确 |
| `test_get_run_detail` | 返回 status + completed_steps + traces + artifacts |
| `test_cancel_graceful` | `?mode=graceful` → checkpoint status=cancelled |
| `test_cancel_immediate` | `?mode=immediate` → checkpoint status=cancelled |
| `test_cancel_unknown` | 不存在的 run_id → 404 |
| `test_resume` | interrupted → resume → 从断点续跑 |
| `test_resume_wrong_status` | completed → resume → 400 |

---

### E3：SSE 适配

**目标**：事件流推送 + Last-Event-ID 续传。

**新增文件**：`src/agentkit/server/sse.py`

**依赖**：`runtime/event.py`（`EventBus/EventLog/RunEvent`）、`runtime/run_manager.py`（`RunManager`）。

**接口契约**：

```python
# 终态事件类型（收到后断开 SSE）
TERMINAL_TYPES = frozenset({
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
    EventType.RUN_INTERRUPTED,
})


async def event_stream(run_id: str, last_event_id: int, run_manager):
    """SSE 事件流生成器。

    流程：
        1. 历史补齐：EventLog(run_id).read_from(last_event_id + 1) 逐条 yield
        2. 判断是否已终态：扫描历史事件，若有终态事件则到此结束
        3. 若未终态：订阅 EventBus，live 事件逐条 yield，直到终态

    每条事件格式化为 SSE：
        id: {seq}
        event: {type}
        data: {json payload}

    Yields:
        dict: sse-starlette EventSourceResponse 所需格式 {event, data, id}
    """
    ...


def create_sse_response(run_id: str, run_manager, last_event_id: int = 0):
    """创建 SSE 响应。

    懒加载 sse_starlette.EventSourceResponse。
    """
    try:
        from sse_starlette import EventSourceResponse
    except ImportError as e:
        raise ImportError("SSE 需要 sse-starlette: pip install agentkit[server]") from e
    return EventSourceResponse(event_stream(run_id, last_event_id, run_manager))
```

**实现要点**：

1. **历史补齐**：`EventLog.read_from(seq)` 返回迭代器，逐条格式化为 SSE。
2. **终态判断**：扫描历史事件，若已有 `RUN_COMPLETED/FAILED/CANCELLED/INTERRUPTED` 则不接 live（run 已结束）。
3. **live 订阅**：`run_manager.get(run_id).event_bus.subscribe()`，若 run 不在内存（已结束）则跳过。
4. **断开检测**：客户端断开时 `EventSourceResponse` 会取消生成器，`_Subscription.cancel()` 自动清理。

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_sse_replay_history` | run 已结束，Last-Event-ID=0 → 返回全部历史事件后关闭 |
| `test_sse_resume_from_id` | Last-Event-ID=5 → 只返回 seq>5 的事件 |
| `test_sse_live_then_terminal` | run 进行中 → 先补历史 → 接 live → 收到终态后断开 |
| `test_sse_seq_continuous` | 断开重连后事件 seq 连续无丢失无重复 |
| `test_sse_finished_run_no_live` | run 已 completed → 只回放历史，不尝试 subscribe |
| `test_sse_unknown_run` | 不存在的 run_id → 返回空或 404 |

---

### E4：artifacts routes + GCSweeper 调度

**目标**：产物清单 + 下载 + GCSweeper 定时调度。

**新增文件**：`src/agentkit/server/routes/artifacts.py`

**依赖**：`runtime/artifact.py`（`ArtifactStore/GCSweeper`）、`runtime/event.py`（`EventLog` 重建 refs）。

**接口契约**：

```python
def create_artifact_routes(base_dir: str, prefix: str = "/api"):
    """创建产物路由。

    端点：
        GET /api/runs/{run_id}/artifacts          产物清单
        GET /api/artifacts/{run_id}/{artifact_id}  下载（支持 Range）
    """
    ...
```

**各端点实现要点**：

1. **`GET /api/runs/{run_id}/artifacts`**：
   - 从 `events.jsonl` 扫描 `ARTIFACT_PRODUCED` 事件，重建 `ArtifactRef` 列表
   - 返回 `[{id, step_id, uri, content_type, size, md5, summary, ts}]`

2. **`GET /api/artifacts/{run_id}/{artifact_id}`**：
   - 从事件日志找到对应 `ArtifactRef`（含 uri）
   - 用 `FileResponse` 返回文件内容，支持 `Range` 头分块
   - 设置 `Content-Type`、`Content-Disposition: attachment`
   - 文件不存在 → 404

**GCSweeper 定时调度**（在 F1 lifespan 集成，此处仅定义函数）：

```python
async def gc_sweeper_loop(base_dir: str, interval: float, grace: float):
    """GCSweeper 定时循环（在 lifespan 启动）。

    每 interval 秒执行一次 GCSweeper.sweep_once()。
    """
    import asyncio
    sweeper = GCSweeper(base_dir=base_dir, orphan_grace_seconds=grace)
    while True:
        await asyncio.sleep(interval)
        stats = sweeper.sweep_once()
        # 记日志
```

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_list_artifacts` | run 有产物 → 返回清单，含 size/md5/summary |
| `test_list_artifacts_empty` | run 无产物 → 返回空列表 |
| `test_download_artifact` | GET 下载 → 200 + 文件内容 + 正确 Content-Type |
| `test_download_artifact_range` | Range 请求 → 206 Partial Content |
| `test_download_artifact_not_found` | 不存在 → 404 |
| `test_gc_sweeper_loop` | 定时触发后 .tmp 与孤儿被清理 |

---

## 阶段 F：集成

### F1：server/app.py + lifespan

**目标**：FastAPI 应用工厂 + 生命周期管理。

**新增文件**：`src/agentkit/server/app.py`

**依赖**：全部前置阶段。

**接口契约**：

```python
# 不用 from __future__ import annotations

def create_app(
    workflow_dir: str = ".",
    *,
    settings: "ServerSettings | None" = None,
    checkpoint_store: "CheckpointStore | None" = None,
) -> "FastAPI":
    """创建 FastAPI 应用。

    懒加载 fastapi。

    组装：
        - lifespan: Reconciler.reconcile() → 启动 GCSweeper 定时循环
        - 安全中间件（C3）
        - 路由：workflows + runs + artifacts + SSE
        - RunManager / Reconciler 注入

    Args:
        workflow_dir:    工作流 YAML 目录。
        settings:        Server 配置；None 时 from_config()。
        checkpoint_store: 检查点存储；None 时 LocalCheckpointStore。
    """
    try:
        from fastapi import FastAPI
    except ImportError as e:
        raise ImportError("Server 需要 fastapi: pip install agentkit[server]") from e

    settings = settings or ServerSettings.from_config()
    # ... 组装 ...
    return app
```

**lifespan 实现**：

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    """应用生命周期。

    启动时：
        1. Reconciler.reconcile() —— 对账僵尸 run + GC 清理
        2. 启动 GCSweeper 定时循环（后台 asyncio.Task）

    关闭时：
        1. 取消 GCSweeper 循环
        2. RunManager.shutdown() —— 关闭所有活跃 Task
    """
    # 启动
    reconciler = Reconciler(checkpoint_store, base_dir=base_dir)
    result = await reconciler.reconcile()
    # 记日志

    gc_task = asyncio.create_task(
        gc_sweeper_loop(base_dir, settings.gc_interval_seconds, settings.gc_orphan_grace_seconds)
    )

    yield  # 应用运行

    # 关闭
    gc_task.cancel()
    try:
        await gc_task
    except asyncio.CancelledError:
        pass
    await run_manager.shutdown()
```

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_create_app` | create_app() 返回 FastAPI 实例，含所有路由 |
| `test_lifespan_reconciles` | app 启动后僵尸 run 被标记 interrupted |
| `test_lifespan_starts_gc` | app 启动后 GCSweeper 循环运行 |
| `test_lifespan_shutdown_cleans` | app 关闭后所有 task done，EventBus closed |
| `test_import_without_fastapi` | 未安装 fastapi 时 import 不报错（懒加载），create_app 抛 ImportError 带安装提示 |

---

### F2：cli serve + pyproject

**目标**：`agentkit serve` 命令 + 依赖声明。

**改动文件**：
1. `src/agentkit/cli.py`（[第 209 行后加 subparser](file:///d:/Dev/LiteFlow/src/agentkit/cli.py#L209)，[第 263 行后加分发](file:///d:/Dev/LiteFlow/src/agentkit/cli.py#L263)）
2. `pyproject.toml`（加 `[project.scripts]` + `server` extra）

**cli.py 改动**：

```python
# 在 main() 的 subparsers 部分（第 250 行后）加：
p_serve = subparsers.add_parser("serve", help="启动可视化服务")
p_serve.add_argument("--dir", default=".", help="工作流 YAML 目录")
p_serve.add_argument("--host", default=None, help="绑定地址（默认 127.0.0.1）")
p_serve.add_argument("--port", type=int, default=None, help="端口（默认 8000）")
p_serve.add_argument("--token", default=None, help="鉴权 bearer token")

# 在 dispatch 部分（第 263 行后）加：
elif args.command == "serve":
    return _cmd_serve(args)


def _cmd_serve(args) -> int:
    """启动可视化服务。"""
    # 覆盖 config
    if args.host is not None:
        set_default("server_host", args.host)
    if args.port is not None:
        set_default("server_port", args.port)
    if args.token is not None:
        set_default("server_token", args.token)

    # 懒加载 server
    try:
        from agentkit.server.app import create_app
    except ImportError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    # 懒加载 uvicorn
    try:
        import uvicorn
    except ImportError:
        print("错误: 需要 uvicorn: pip install agentkit[server]", file=sys.stderr)
        return 1

    app = create_app(args.dir)
    settings = ServerSettings.from_config()
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0
```

**pyproject.toml 改动**：

```toml
[project.scripts]
agentkit = "agentkit.cli:main"

[project.optional-dependencies]
doc-convert = [
    "markdown2>=2.5.5",
    "reportlab>=4.0",
    "python-docx>=1.2",
]
server = [
    "fastapi>=0.100.0",
    "uvicorn>=0.20.0",
    "sse-starlette>=1.6",
]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "fastapi>=0.100.0",
    "uvicorn>=0.20.0",
    "sse-starlette>=1.6",
    "httpx>=0.24",      # 测试 FastAPI 的 AsyncClient
]
```

**验收标准**：

| 测试用例 | 断言 |
|---------|------|
| `test_serve_command_exists` | `agentkit serve --help` 显示子命令 |
| `test_serve_starts_server` | `agentkit serve --dir ./workflows --port 8000` 启动后 GET /api/meta/step-types 返回 200 |
| `test_serve_without_fastapi` | 未安装 fastapi 时退出码 1 + 安装提示 |
| `test_cli_scripts_entry` | `agentkit --help` 可用（pyproject scripts 生效） |
| `test_pyproject_server_extra` | `pip install ".[server]"` 安装 fastapi/uvicorn/sse-starlette |

---

## 阶段 G：测试验证

对齐 `docs/visualization-design.md` §8 三风险验证。

### G1：阻塞验证（§3.1）

**测试文件**：`src/agentkit/tests/server/test_blocking_e2e.py`

**场景**：run A 含 `execution=thread` 的 5s 阻塞工具；并发 run B 开启流式 LLM。

```python
async def test_blocking_does_not_block_sse():
    """阻塞工具执行期间 SSE 延迟 p95 < 200ms。

    步骤：
        1. 构造 run A：ToolStep 用 5s 阻塞工具（execution=thread）
        2. 构造 run B：LLMStep 流式，每 50ms 产 delta
        3. 并发启动 A + B
        4. 订阅 B 的 SSE，记录每个 delta 的到达时间
        5. 断言 delta 间隔 p95 < 200ms
    """
    ...
```

**验收标准**：
- run A 执行期间 run B 的 SSE delta 端到端延迟 p95 < 200ms
- run A 最终成功完成（thread 模式正确卸载）

---

### G2：易失验证（§3.2）

**测试文件**：`src/agentkit/tests/server/test_crash_recovery_e2e.py`

**场景**：run 执行中 kill Server → 重启 → 断言 interrupted → resume → 断言续跑且 sink 不重复。

```python
async def test_crash_recovery():
    """kill -9 后重启：running→interrupted→resume 成功且 sink 不重复。

    步骤：
        1. 启动 Server，启动含 sink 工具的 run
        2. run 执行到一半时 kill Server 进程
        3. 重启 Server（触发 Reconciler）
        4. 断言 run status == interrupted
        5. 调 POST /resume
        6. 断言从断点续跑（completed_steps 跳过）
        7. 断言 MockToolHooks 的 sink 调用计数未增加
    """
    ...
```

**验收标准**：
- 重启后 run 标记为 interrupted
- resume 后从断点续跑（completed_steps 跳过）
- sink 类工具未重复执行（MockToolHooks 计数不变）

---

### G3：一致性验证（§3.3）

**测试文件**：`src/agentkit/tests/server/test_consistency_e2e.py`

**场景 1**：rename 后 kill → 重启 GC → 断言孤儿回收。
**场景 2**：事件日志 append 后、分发前 kill → SSE 重连 → 断言事件补齐无丢失。

```python
async def test_gc_reclaims_orphan():
    """故障注入：rename 后 kill → 重启 GC → 断言孤儿被回收。

    步骤：
        1. 构造 ArtifactStore.save 执行到 step 3（rename）后、step 4（publish）前崩溃
        2. 文件已存在但无事件引用（孤儿）
        3. 重启 → GCSweeper.sweep_once()
        4. 断言孤儿文件在宽限期后被删除
    """
    ...


async def test_sse_resume_no_loss():
    """事件日志 append 后、分发前 kill → SSE 重连 → 事件补齐无丢失无重复。

    步骤：
        1. run 执行中，EventLog.append 后、EventBus 入队前模拟崩溃
        2. SSE 客户端重连，带 Last-Event-ID
        3. 断言从 EventLog 补齐的事件 seq 连续
        4. 断言无丢失（所有 seq 都收到）无重复（无 seq 重复）
    """
    ...
```

**验收标准**：
- 孤儿文件超宽限期后被 GC 回收
- SSE 重连后事件 seq 连续，无丢失无重复

---

## 附录：模块依赖方向

```
core/          （不改，P0 已完成）
  ▲
  │ 只读 hooks 契约 + 公开 API
  │
runtime/       （A1 改、A2/B1 新增，其余 P0 不动）
  ▲
  │ RunManager / Reconciler / ArtifactStore / EventBus
  │
server/        （C2/C3/D1/E1-E4/F1 全新）
  │ 懒加载 fastapi/uvicorn/sse-starlette
  │
cli.py         （F2 改：+ serve 子命令）
```

**铁律**：
- `core/` 不 import `runtime/` 或 `server/`
- `runtime/` 不 import `server/`
- `server/` 模块顶层不 import fastapi（懒加载在函数内）
- `server/` 模块不用 `from __future__ import annotations`（Pydantic 局部类解析陷阱）

---

## 实施顺序建议

1. **A1**（EventBusHooks 补丁）—— 最小改动，解除 cancelled 误发
2. **A2**（RunManager）—— 核心编排层
3. **B1**（Reconciler）—— 依赖 A2
4. **C1 + C2 + C3**（配置/安全）—— 可与 A/B 并行
5. **D1**（validator 增强）—— 可与 A/B/C 并行
6. **E1-E4**（路由层）—— 依赖 A2/C3/D1
7. **F1**（app 集成）—— 依赖全部
8. **F2**（cli + pyproject）—— 依赖 F1
9. **G1-G3**（端到端测试）—— 依赖全部
