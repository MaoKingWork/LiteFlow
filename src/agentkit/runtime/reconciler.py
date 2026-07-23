"""runtime.reconciler —— 启动对账：僵尸 run 标记 + GC 清理 + 日志完整性扫描。

本模块实现 :class:`Reconciler`，在 Server 启动时执行一次性对账，保证崩溃后
状态一致（对齐 ``docs/visualization-design.md`` §5.4）。

对账流程：
    1. 扫描 checkpoint_store 全量 run_id
    2. 对 status ∈ {running, cancelling} 的 run：
       - 置 ``interrupted`` + ``error="process_restart"``
       - save 回 checkpoint_store
       - 构造临时 EventLog，append ``run_interrupted`` 事件
    3. **不自动 resume**（§5.4：sink 类工具有副作用，自动重放可能重复通知 / 写入）
    4. ``GCSweeper.sweep_once()`` 清理 ``.tmp`` 残留与孤儿文件
    5. 扫描 ``{base_dir}/*/events.jsonl``，统计损坏行数

设计要点：
    - **不自动 resume**：恢复决策留给用户（前端 ``POST /resume``）
    - **run_interrupted 事件**：对每个 interrupted run，构造临时 EventLog
      （不建 EventBus，因为 run 已死无订阅者），append ``run_interrupted``
      事件。前端 SSE 续传时能看到状态变更
    - **interrupted_reason 存储**：因 ``core/`` 不改（P0 已完成），
      ``Checkpoint`` 无 ``interrupted_reason`` 字段，复用 ``error`` 字段
      存储原因（``"process_restart"``）；事件 payload 携带 ``reason``
    - **损坏日志不中断**：损坏行跳过并记 warning，仅统计计数

模块化原则：
    - 仅依赖标准库 + ``core/`` 公开 API + ``runtime/`` 兄弟模块
    - 不依赖 ``server/``（Reconciler 在 server lifespan 启动前可独立运行）

公开 API：
    - ReconcileResult: 对账结果统计
    - Reconciler:      启动对账器
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentkit.core.checkpoint import CheckpointStore, RunStatus
from agentkit.runtime.artifact import GCSweeper
from agentkit.runtime.event import EventLog, EventType, RunEvent

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

__all__ = ["ReconcileResult", "Reconciler"]


# ---------------------------------------------------------------------------
# ReconcileResult —— 对账结果统计
# ---------------------------------------------------------------------------
@dataclass
class ReconcileResult:
    """对账结果统计。

    Attributes:
        interrupted_count:   标记为 interrupted 的 run 数。
        gc_stats:            ``GCSweeper.sweep_once()`` 返回值。
        event_log_corrupt:   损坏事件日志文件数（含无法解析行的文件）。
    """

    interrupted_count: int = 0
    gc_stats: dict[str, int] = field(default_factory=dict)
    event_log_corrupt: int = 0


# ---------------------------------------------------------------------------
# Reconciler —— 启动对账器
# ---------------------------------------------------------------------------
class Reconciler:
    """启动对账（对齐 §5.4）。

    在 Server lifespan 启动时调 :meth:`reconcile`，把僵尸 ``running`` /
    ``cancelling`` 标记为 ``interrupted``，并清理孤儿文件。

    Args:
        checkpoint_store:  检查点存储。
        base_dir:          产物与事件日志根目录，默认 ``output/runs``。
    """

    def __init__(
        self,
        checkpoint_store: CheckpointStore,
        *,
        base_dir: str = "output/runs",
    ) -> None:
        self._checkpoint_store: CheckpointStore = checkpoint_store
        self._base_dir: str = base_dir

    async def reconcile(self) -> ReconcileResult:
        """执行一次对账。

        流程：
            1. ``checkpoint_store.list_runs()`` 取全量 run_id
            2. load 每个 checkpoint，status ∈ {running, cancelling}
               → 置 ``interrupted`` + ``error="process_restart"``
               → save 回 checkpoint_store
               → 构造 EventLog，append ``run_interrupted`` 事件
            3. 不自动 resume（§5.4：sink 副作用）
            4. ``GCSweeper.sweep_once()`` 清理孤儿
            5. 统计损坏事件日志（无法解析的 events.jsonl）

        Returns:
            ReconcileResult: 统计结果。
        """
        # 1. 对账僵尸 run
        interrupted_count = await self._reconcile_zombie_runs()

        # 2. GC 清理
        gc_sweeper = GCSweeper(base_dir=self._base_dir)
        gc_stats = gc_sweeper.sweep_once()

        # 3. 事件日志完整性扫描
        event_log_corrupt = self._scan_corrupt_logs()

        result = ReconcileResult(
            interrupted_count=interrupted_count,
            gc_stats=gc_stats,
            event_log_corrupt=event_log_corrupt,
        )
        logger.info(
            "对账完成：interrupted=%d, gc=%s, corrupt_logs=%d",
            result.interrupted_count,
            result.gc_stats,
            result.event_log_corrupt,
        )
        return result

    # ------------------------------------------------------------------
    # 内部：对账僵尸 run
    # ------------------------------------------------------------------
    async def _reconcile_zombie_runs(self) -> int:
        """把 running/cancelling 的 run 标记为 interrupted。

        对每个僵尸 run：
            - checkpoint.status = interrupted
            - checkpoint.error = "process_restart"
            - save 回 checkpoint_store
            - EventLog append run_interrupted 事件

        Returns:
            int: 标记为 interrupted 的 run 数。
        """
        run_ids = await self._checkpoint_store.list_runs()
        count = 0
        for run_id in run_ids:
            cp = await self._checkpoint_store.load(run_id)
            if cp is None:
                continue
            if cp.status not in (RunStatus.RUNNING, RunStatus.CANCELLING):
                continue

            # 置 interrupted + 原因
            cp.status = RunStatus.INTERRUPTED
            cp.error = "process_restart"
            cp.updated_at = time.time()
            await self._checkpoint_store.save(cp)

            # append run_interrupted 事件到 EventLog
            # （不建 EventBus：run 已死无订阅者，仅写日志供 SSE 续传）
            self._append_interrupted_event(run_id)

            count += 1
            logger.info("对账：run %s %s → interrupted", run_id, cp.status)
        return count

    def _append_interrupted_event(self, run_id: str) -> None:
        """向 run 的事件日志追加 ``run_interrupted`` 事件。

        手动分配 seq（取 latest_seq + 1），不建 EventBus。
        """
        event_log = EventLog(run_id, base_dir=self._base_dir)
        latest = event_log.latest_seq()
        event = RunEvent(
            run_id=run_id,
            type=EventType.RUN_INTERRUPTED,
            seq=latest + 1,
            ts=time.time(),
            payload={
                "status": "interrupted",
                "reason": "process_restart",
            },
        )
        try:
            event_log.append(event)
        except Exception as exc:
            logger.warning(
                "写入 run_interrupted 事件失败 run=%s: %r", run_id, exc
            )

    # ------------------------------------------------------------------
    # 内部：扫描损坏事件日志
    # ------------------------------------------------------------------
    def _scan_corrupt_logs(self) -> int:
        """扫描所有 ``events.jsonl``，统计含损坏行的文件数。

        遍历 ``{base_dir}/{run_id}/events.jsonl``，逐行解析；
        任何行无法解析则该文件计为损坏。不中断对账流程。

        Returns:
            int: 含损坏行的事件日志文件数。
        """
        if not os.path.isdir(self._base_dir):
            return 0

        corrupt_count = 0
        for run_id in os.listdir(self._base_dir):
            log_path = os.path.join(self._base_dir, run_id, "events.jsonl")
            if not os.path.isfile(log_path):
                continue
            if self._log_has_corrupt_lines(log_path):
                corrupt_count += 1
                logger.warning("事件日志损坏: %s", log_path)
        return corrupt_count

    @staticmethod
    def _log_has_corrupt_lines(log_path: str) -> bool:
        """检查单个 events.jsonl 是否含无法解析的行。

        空行跳过；任何非空行无法 JSON 解析则返回 ``True``。
        """
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        return True
        except OSError:
            # 文件不可读：不计为损坏（可能是权限问题，不是日志损坏）
            return False
        return False
