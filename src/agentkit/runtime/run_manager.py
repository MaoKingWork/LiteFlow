"""runtime.run_manager —— 运行控制层：状态机编排 + 取消编排 + 内存注册表。

本模块实现 :class:`RunManager`，作为 P1 可视化服务层与引擎之间的控制面，
负责：

    - **start()**：分配 run_id → 构造 EventBus / EventLog / ArtifactStore /
      EventBusHooks → 注入 hooks → 发 ``run_created`` → ``asyncio.create_task``
      → 注册 :class:`RunHandle`。
    - **cancel()**：``graceful`` 触发 :class:`CancelToken` / ``immediate`` 调
      ``task.cancel()`` → 发 ``run_cancelling`` → await task → 发 ``run_cancelled``。
    - **resume()**：从 ``interrupted`` / ``failed`` 恢复，复用既有 checkpoint
      跳过 ``completed_steps``（sink 类工具不重复执行）。
    - **内存注册表**：``dict[run_id, RunHandle]``，重启丢弃（语义正确——
      崩溃后的 run 由 :class:`~agentkit.runtime.reconciler.Reconciler` 标记
      ``interrupted``，用户经 ``POST /resume`` 恢复）。

设计要点（对齐 ``docs/visualization-design.md`` §5.4）：
    - **不侵入 _execute**：经 hooks + Workflow 公开 API（``run`` / ``resume``）接入，
      ``core/`` 零改动。
    - **hooks 注入**：``start()`` 时用 :class:`CompositeHooks` 把原 workflow hooks
      + :class:`EventBusHooks` 合并；保留原始 hooks 引用以支持同 workflow 多次运行。
    - **cancel 幂等**：重复 ``cancel()`` 不重复发 ``run_cancelled``。
    - **run_cancelled 只发一次**：``mark_cancelling()`` 让 ``after_workflow`` 跳过
      ``run_completed`` / ``run_failed``，由 ``cancel()`` 显式补发 ``run_cancelled``
      作为唯一终态事件。
    - **Task done 回调**：正常完成的 run 自动从内存注册表移除，``list_runs`` 的
      ``is_active`` 准确反映可取消状态。

模块化原则：
    - 仅依赖标准库 + ``core/`` 公开 API + ``runtime/`` 兄弟模块
    - ``config`` 统一读取 ``server_*`` 配置（见 :mod:`agentkit.config`）

公开 API：
    - RunHandle:    单个 run 的内存句柄
    - RunSummary:   run 列表项（合并 checkpoint + 内存状态）
    - RunManager:   运行控制层
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentkit.config import get_default
from agentkit.core.cancel import CancelToken
from agentkit.core.checkpoint import CheckpointStore, LocalCheckpointStore, RunStatus
from agentkit.core.hooks import CompositeHooks, LifecycleHooks
from agentkit.runtime.artifact import ArtifactStore
from agentkit.runtime.event import EventBus, EventLog, EventType, RunEvent
from agentkit.runtime.event_hooks import EventBusHooks

if TYPE_CHECKING:
    from agentkit.core.workflow import Workflow, WorkflowResult

logger = logging.getLogger(__name__)

__all__ = ["RunHandle", "RunSummary", "RunManager"]


# ---------------------------------------------------------------------------
# RunHandle —— 单个 run 的内存句柄
# ---------------------------------------------------------------------------
@dataclass
class RunHandle:
    """单个 run 的内存句柄（控制面，重启丢弃）。

    由 :meth:`RunManager.start` / :meth:`RunManager.resume` 创建并注册到
    :attr:`RunManager._handles`。Task 正常完成后经 done 回调自动移除；
    ``cancel()`` / ``shutdown()`` 时显式移除。

    Attributes:
        run_id:          run id。
        workflow_name:   所属 Workflow 名称。
        task:            ``asyncio.Task[WorkflowResult]``。
        cancel_token:    协作式取消令牌。
        event_bus:       per-run 事件总线。
        event_log:       per-run 事件日志。
        artifact_store:  per-run 产物存储。
        hooks:           注入的 EventBusHooks（用于 mark_cancelling）。
        started_at:      启动时间戳。
        cancelling:      是否已进入 cancelling 流程（幂等保护）。
    """

    run_id: str
    workflow_name: str
    task: "asyncio.Task"
    cancel_token: CancelToken
    event_bus: EventBus
    event_log: EventLog
    artifact_store: ArtifactStore
    hooks: EventBusHooks
    started_at: float
    cancelling: bool = False


# ---------------------------------------------------------------------------
# RunSummary —— run 列表项
# ---------------------------------------------------------------------------
@dataclass
class RunSummary:
    """run 列表项（合并 checkpoint 状态 + 内存活跃标记）。

    Attributes:
        run_id:          run id。
        workflow_name:   所属 Workflow 名称。
        status:          运行状态（来自 checkpoint）。
        started_at:      启动时间戳。
        updated_at:      最近更新时间戳。
        is_active:       是否在内存注册表中（可 cancel）。
        error:           失败 / 中断时的错误信息。
    """

    run_id: str
    workflow_name: str
    status: str
    started_at: float
    updated_at: float
    is_active: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# _WorkflowEntry —— workflow 注册条目（内部）
# ---------------------------------------------------------------------------
@dataclass
class _WorkflowEntry:
    """workflow 注册条目，保存原始 hooks 以支持同 workflow 多次运行。

    Attributes:
        workflow:       Workflow 实例引用。
        original_hooks: 首次注册时的原始 hooks（供每次 start/resume 重新包装）。
    """

    workflow: "Workflow"
    original_hooks: LifecycleHooks | None


# ---------------------------------------------------------------------------
# RunManager —— 运行控制层
# ---------------------------------------------------------------------------
class RunManager:
    """运行控制层：状态机编排 + 取消编排 + 内存注册表。

    职责：
        - :meth:`start`：创建 EventBus / EventLog / ArtifactStore + checkpoint(running)
          → 发 ``run_created`` → ``asyncio.create_task(workflow.run)`` → 注册 RunHandle
        - :meth:`cancel`：``graceful`` 触发令牌 / ``immediate`` ``task.cancel()``
          → 发 ``run_cancelling`` → await task → 发 ``run_cancelled``
        - :meth:`resume`：从 ``interrupted`` / ``failed`` 恢复，调 ``workflow.resume(run_id)``
        - 内存注册表 ``dict[run_id, RunHandle]``，重启丢弃（语义正确）

    不侵入 ``_execute``：经 hooks + Workflow 公开 API 接入。

    Args:
        checkpoint_store:  检查点存储；``None`` 时用 :class:`LocalCheckpointStore`。
        base_dir:          产物与事件日志根目录，默认 ``output/runs``。
    """

    def __init__(
        self,
        checkpoint_store: CheckpointStore | None = None,
        *,
        base_dir: str = "output/runs",
    ) -> None:
        self._checkpoint_store: CheckpointStore = (
            checkpoint_store or LocalCheckpointStore()
        )
        self._base_dir: str = base_dir
        # 内存注册表：run_id → RunHandle（重启丢弃）
        self._handles: dict[str, RunHandle] = {}
        # workflow 注册表：workflow_name → _WorkflowEntry
        # 保存原始 hooks，每次 start/resume 用原始 hooks + 新 EventBusHooks 重新包装
        self._workflows: dict[str, _WorkflowEntry] = {}

    # ------------------------------------------------------------------
    # start —— 启动一个 run
    # ------------------------------------------------------------------
    async def start(
        self,
        workflow: "Workflow",
        inputs: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> str:
        """启动一个 run。

        流程：
            1. 分配 run_id（``None`` 时自动生成 ``run_<uuid hex 前 12 位>``）
            2. 注册 workflow（首次注册时保存原始 hooks）
            3. 构造 EventBus + EventLog + ArtifactStore + EventBusHooks
            4. 把 EventBusHooks 与原 hooks 经 CompositeHooks 合并后注入 workflow
            5. 发 ``run_created`` 事件
            6. ``asyncio.create_task(workflow.run(inputs, run_id, cancel_token))``
            7. 注册 RunHandle 到内存

        Args:
            workflow: 要执行的 Workflow（hooks 会被替换为 CompositeHooks）。
            inputs:   输入变量 dict。
            run_id:   自定义 run id；``None`` 时自动生成。

        Returns:
            str: run_id。
        """
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        entry = self._register_workflow(workflow)

        # per-run 基础设施
        event_log = EventLog(run_id, base_dir=self._base_dir)
        event_bus = EventBus(
            run_id,
            log=event_log,
            queue_size=get_default("server_event_queue_size"),
        )
        artifact_store = ArtifactStore(
            run_id,
            event_bus=event_bus,
            base_dir=self._base_dir,
            max_size=get_default("server_artifact_max_size"),
            max_total=get_default("server_artifact_max_total"),
        )
        hooks = EventBusHooks(event_bus, run_id=run_id)

        # 注入 hooks：原 hooks + EventBusHooks
        workflow.hooks = self._merge_hooks(entry.original_hooks, hooks)
        # 确保 workflow 用 RunManager 的 checkpoint_store（单一数据源）
        workflow.checkpoint_store = self._checkpoint_store

        # 发 run_created（seq=1，在 task 启动前）
        await event_bus.publish(RunEvent(
            run_id=run_id,
            type=EventType.RUN_CREATED,
            payload={"workflow_name": workflow.name},
        ))

        # 启动 task
        cancel_token = CancelToken()
        task = asyncio.create_task(
            workflow.run(inputs, run_id, cancel_token),
            name=f"run:{run_id}",
        )

        # 注册 handle + done 回调
        handle = RunHandle(
            run_id=run_id,
            workflow_name=workflow.name,
            task=task,
            cancel_token=cancel_token,
            event_bus=event_bus,
            event_log=event_log,
            artifact_store=artifact_store,
            hooks=hooks,
            started_at=time.time(),
        )
        self._handles[run_id] = handle
        task.add_done_callback(
            functools.partial(self._on_task_done, run_id)
        )
        return run_id

    # ------------------------------------------------------------------
    # cancel —— 取消一个 run
    # ------------------------------------------------------------------
    async def cancel(self, run_id: str, *, mode: str = "graceful") -> None:
        """取消一个 run。

        流程（两种模式共用）：
            1. ``mark_cancelling()`` → ``after_workflow`` 跳过 ``run_completed``
               / ``run_failed``（防止 cancelled 时误发 ``run_completed``）
            2. 发 ``run_cancelling``
            3. 触发取消：``graceful`` → ``cancel_token.trigger()``；
               ``immediate`` → ``task.cancel()``
            4. ``await handle.task``（吞 ``CancelledError``）
            5. 发 ``run_cancelled``（唯一终态事件）
            6. 关闭 EventBus + 移除 handle

        Args:
            run_id: 目标 run。
            mode:   ``graceful``（默认）触发令牌，当前 step 完成后停止；
                    ``immediate`` 调 ``task.cancel()`` 注入 ``CancelledError``。

        Raises:
            KeyError: run_id 不在内存注册表（已结束或不存在）。
            ValueError: mode 非法。
        """
        if mode not in ("graceful", "immediate"):
            raise ValueError(
                f"非法 mode: {mode!r}（允许: graceful / immediate）"
            )

        handle = self._handles.get(run_id)
        if handle is None or handle.task.done():
            # 已结束（done 回调可能尚未触发）或不存在
            if handle is not None:
                self._handles.pop(run_id, None)
            raise KeyError(
                f"run {run_id!r} 已结束或不在内存注册表"
            )

        # 幂等：重复 cancel 不重复发事件
        if handle.cancelling:
            return
        handle.cancelling = True

        # 标记 cancelling：after_workflow 跳过 run_completed/run_failed
        handle.hooks.mark_cancelling()

        # 发 run_cancelling
        await handle.event_bus.publish(RunEvent(
            run_id=run_id,
            type=EventType.RUN_CANCELLING,
            payload={"mode": mode},
        ))

        # 触发取消
        if mode == "graceful":
            handle.cancel_token.trigger()
        else:
            handle.task.cancel()

        # 等待 task 结束（吞 CancelledError）
        try:
            await handle.task
        except asyncio.CancelledError:
            pass

        # 发 run_cancelled（mark_cancelling 保证了 after_workflow 未发终态事件，
        # 此处补发 run_cancelled 作为唯一终态事件）
        await handle.event_bus.publish(RunEvent(
            run_id=run_id,
            type=EventType.RUN_CANCELLED,
            payload={"mode": mode},
        ))

        # 清理
        await handle.event_bus.close()
        self._handles.pop(run_id, None)

    # ------------------------------------------------------------------
    # resume —— 从 interrupted/failed 恢复
    # ------------------------------------------------------------------
    async def resume(self, run_id: str) -> str:
        """从 ``interrupted`` / ``failed`` 恢复执行。

        调用 ``workflow.resume(run_id)``，复用既有 checkpoint 跳过
        ``completed_steps``。sink 类工具不会重复执行（resume 跳过已完成的 step）。

        流程与 :meth:`start` 类似，但调 ``workflow.resume`` 而非 ``workflow.run``。

        Args:
            run_id: 要恢复的 run id。

        Returns:
            str: 同一 run_id（resume 不分配新 id）。

        Raises:
            KeyError: run_id 不在 checkpoint store，或对应 workflow 未注册。
            ValueError: run 状态非 ``interrupted`` / ``failed``。
        """
        checkpoint = await self._checkpoint_store.load(run_id)
        if checkpoint is None:
            raise KeyError(f"检查点 {run_id!r} 不存在，无法 resume")

        if checkpoint.status not in (RunStatus.INTERRUPTED, RunStatus.FAILED):
            raise ValueError(
                f"run {run_id!r} 状态为 {checkpoint.status!r}，"
                f"仅 interrupted/failed 可 resume"
            )

        entry = self._workflows.get(checkpoint.workflow_name)
        if entry is None:
            raise KeyError(
                f"workflow {checkpoint.workflow_name!r} 未注册，无法 resume"
                f"（需先 start 或经 server 层加载）"
            )
        workflow = entry.workflow

        # per-run 基础设施
        event_log = EventLog(run_id, base_dir=self._base_dir)
        event_bus = EventBus(
            run_id,
            log=event_log,
            queue_size=get_default("server_event_queue_size"),
        )
        artifact_store = ArtifactStore(
            run_id,
            event_bus=event_bus,
            base_dir=self._base_dir,
            max_size=get_default("server_artifact_max_size"),
            max_total=get_default("server_artifact_max_total"),
        )
        hooks = EventBusHooks(event_bus, run_id=run_id)

        # 注入 hooks
        workflow.hooks = self._merge_hooks(entry.original_hooks, hooks)
        workflow.checkpoint_store = self._checkpoint_store

        # 启动 task
        cancel_token = CancelToken()
        task = asyncio.create_task(
            workflow.resume(run_id, cancel_token),
            name=f"resume:{run_id}",
        )

        # 注册 handle + done 回调
        handle = RunHandle(
            run_id=run_id,
            workflow_name=workflow.name,
            task=task,
            cancel_token=cancel_token,
            event_bus=event_bus,
            event_log=event_log,
            artifact_store=artifact_store,
            hooks=hooks,
            started_at=time.time(),
        )
        self._handles[run_id] = handle
        task.add_done_callback(
            functools.partial(self._on_task_done, run_id)
        )
        return run_id

    # ------------------------------------------------------------------
    # get —— 查内存注册表
    # ------------------------------------------------------------------
    def get(self, run_id: str) -> RunHandle | None:
        """查内存注册表（仅活跃 run）。

        Returns:
            RunHandle | None: 活跃 run 的句柄；不在内存时返回 ``None``。
        """
        return self._handles.get(run_id)

    # ------------------------------------------------------------------
    # list_runs —— 合并 checkpoint + 内存状态
    # ------------------------------------------------------------------
    async def list_runs(
        self, workflow_name: str | None = None
    ) -> list[RunSummary]:
        """列出所有 run（合并 checkpoint 状态 + 内存活跃标记）。

        Args:
            workflow_name: 可选，按 Workflow 名称过滤。

        Returns:
            list[RunSummary]: run 摘要列表。
        """
        run_ids = await self._checkpoint_store.list_runs(workflow_name)
        summaries: list[RunSummary] = []
        for rid in run_ids:
            cp = await self._checkpoint_store.load(rid)
            if cp is None:
                continue
            summaries.append(RunSummary(
                run_id=rid,
                workflow_name=cp.workflow_name,
                status=cp.status,
                started_at=cp.started_at,
                updated_at=cp.updated_at,
                is_active=rid in self._handles,
                error=cp.error,
            ))
        return summaries

    # ------------------------------------------------------------------
    # shutdown —— 关闭所有活跃 Task 与 EventBus
    # ------------------------------------------------------------------
    async def shutdown(self) -> None:
        """关闭所有活跃 Task 与 EventBus（进程退出时调用）。

        对每个活跃 handle：``task.cancel()`` → ``await`` → 关闭 EventBus。
        已完成的 Task 直接跳过 await。
        """
        handles = list(self._handles.values())
        self._handles.clear()

        tasks_to_await: list[asyncio.Task] = []
        for handle in handles:
            if not handle.task.done():
                handle.task.cancel()
                tasks_to_await.append(handle.task)

        if tasks_to_await:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)

        for handle in handles:
            try:
                await handle.event_bus.close()
            except Exception as exc:
                logger.warning("关闭 EventBus 失败 run=%s: %r", handle.run_id, exc)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _register_workflow(self, workflow: "Workflow") -> _WorkflowEntry:
        """注册 workflow，保存原始 hooks。

        首次注册时记录原始 hooks；已注册时不覆盖（保留首次的原始 hooks，
        避免把含 EventBusHooks 的 CompositeHooks 当作 "原始" hooks）。
        """
        name = workflow.name
        if name not in self._workflows:
            self._workflows[name] = _WorkflowEntry(
                workflow=workflow,
                original_hooks=workflow.hooks,
            )
        return self._workflows[name]

    @staticmethod
    def _merge_hooks(
        original: LifecycleHooks | None,
        event_hooks: EventBusHooks,
    ) -> LifecycleHooks:
        """把原 hooks 与 EventBusHooks 合并为 CompositeHooks。

        ``original`` 为 ``None`` 时直接返回 ``event_hooks``。
        """
        if original is None:
            return event_hooks
        return CompositeHooks([original, event_hooks])

    def _on_task_done(self, run_id: str, task: "asyncio.Task") -> None:
        """Task 完成回调：从内存注册表移除（幂等）。

        正常完成的 run 经此回调自动移除，``list_runs`` 的 ``is_active``
        准确反映可取消状态。``cancel()`` / ``shutdown()`` 也会移除，
        ``dict.pop(key, None)`` 保证幂等。
        """
        handle = self._handles.pop(run_id, None)
        if handle is None:
            return
        # 记录未捕获异常（workflow 内部已落盘，此处仅日志）
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("run %s 异常结束: %r", run_id, exc)
