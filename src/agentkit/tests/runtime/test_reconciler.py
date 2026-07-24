"""Reconciler：启动对账（僵尸 run 标记 + GC 清理 + 日志完整性扫描）。

出口标准（对齐 P1 §B1）：
    - running/cancelling → interrupted，error="process_restart"
    - completed/failed/interrupted 状态不变
    - 每个 interrupted run 的 events.jsonl 有 run_interrupted 事件
    - 不自动 resume（completed_steps 不变，未重新执行）
    - GCSweeper 清理 .tmp 残留 + 孤儿文件（超宽限期删除，宽限期内保留）
    - 损坏 events.jsonl 不中断对账，event_log_corrupt 计数正确
    - 重复 reconcile 幂等（不重复发 run_interrupted 事件）
"""
from __future__ import annotations

import json
import os
import time

import pytest

from agentkit.core.checkpoint import Checkpoint, RunStatus
from agentkit.runtime.event import EventLog, EventType
from agentkit.runtime.reconciler import ReconcileResult, Reconciler


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def reconciler(checkpoint_store, tmp_path) -> Reconciler:
    """基于 tmp_path 的 Reconciler，测试结束自动清理。"""
    return Reconciler(checkpoint_store, base_dir=str(tmp_path / "runs"))


async def _make_checkpoint(
    store,
    run_id: str,
    *,
    workflow_name: str = "test_wf",
    status: str = RunStatus.RUNNING,
    completed_steps: list[str] | None = None,
    error: str | None = None,
) -> Checkpoint:
    """创建并保存一个 checkpoint，返回实例。"""
    cp = Checkpoint.new(workflow_name, run_id=run_id)
    cp.status = status
    if completed_steps is not None:
        cp.completed_steps = list(completed_steps)
    cp.error = error
    await store.save(cp)
    return cp


# ---------------------------------------------------------------------------
# 僵尸 run 对账：running/cancelling → interrupted
# ---------------------------------------------------------------------------
async def test_reconcile_running_to_interrupted(reconciler, checkpoint_store):
    """status=running → reconcile 后=interrupted，error="process_restart"。"""
    await _make_checkpoint(checkpoint_store, "run_1", status=RunStatus.RUNNING)

    result = await reconciler.reconcile()

    assert result.interrupted_count == 1
    cp = await checkpoint_store.load("run_1")
    assert cp.status == RunStatus.INTERRUPTED
    assert cp.error == "process_restart"


async def test_reconcile_cancelling_to_interrupted(reconciler, checkpoint_store):
    """status=cancelling → reconcile 后=interrupted。"""
    await _make_checkpoint(checkpoint_store, "run_2", status=RunStatus.CANCELLING)

    result = await reconciler.reconcile()

    assert result.interrupted_count == 1
    cp = await checkpoint_store.load("run_2")
    assert cp.status == RunStatus.INTERRUPTED
    assert cp.error == "process_restart"


# ---------------------------------------------------------------------------
# 非僵尸 run 不变
# ---------------------------------------------------------------------------
async def test_reconcile_completed_unchanged(reconciler, checkpoint_store):
    """status=completed → reconcile 后不变。"""
    await _make_checkpoint(checkpoint_store, "run_3", status=RunStatus.COMPLETED)

    result = await reconciler.reconcile()

    assert result.interrupted_count == 0
    cp = await checkpoint_store.load("run_3")
    assert cp.status == RunStatus.COMPLETED
    assert cp.error is None


async def test_reconcile_failed_unchanged(reconciler, checkpoint_store):
    """status=failed → reconcile 后不变（含 error 与 completed_steps）。"""
    await _make_checkpoint(
        checkpoint_store, "run_4",
        status=RunStatus.FAILED,
        completed_steps=["s1"],
        error="original error",
    )

    result = await reconciler.reconcile()

    assert result.interrupted_count == 0
    cp = await checkpoint_store.load("run_4")
    assert cp.status == RunStatus.FAILED
    assert cp.error == "original error"
    assert cp.completed_steps == ["s1"]


async def test_reconcile_interrupted_unchanged(reconciler, checkpoint_store):
    """status=interrupted → reconcile 后不变（不重复对账）。"""
    await _make_checkpoint(
        checkpoint_store, "run_int",
        status=RunStatus.INTERRUPTED,
        error="previous_restart",
    )

    result = await reconciler.reconcile()

    assert result.interrupted_count == 0
    cp = await checkpoint_store.load("run_int")
    assert cp.status == RunStatus.INTERRUPTED
    assert cp.error == "previous_restart"


# ---------------------------------------------------------------------------
# run_interrupted 事件
# ---------------------------------------------------------------------------
async def test_reconcile_interrupted_emits_event(reconciler, checkpoint_store):
    """reconcile 后 events.jsonl 有 run_interrupted 事件，payload 含 reason。"""
    await _make_checkpoint(checkpoint_store, "run_5", status=RunStatus.RUNNING)

    await reconciler.reconcile()

    log = EventLog("run_5", base_dir=reconciler._base_dir)
    events = list(log.read_from())
    interrupted_events = [e for e in events if e.type == EventType.RUN_INTERRUPTED]
    assert len(interrupted_events) == 1
    ev = interrupted_events[0]
    assert ev.run_id == "run_5"
    assert ev.payload["status"] == "interrupted"
    assert ev.payload["reason"] == "process_restart"


async def test_reconcile_event_seq_continues(reconciler, checkpoint_store):
    """已有事件的 run，run_interrupted 的 seq 接续递增。"""
    await _make_checkpoint(checkpoint_store, "run_seq", status=RunStatus.RUNNING)

    # 预先写两条事件
    log = EventLog("run_seq", base_dir=reconciler._base_dir)
    from agentkit.runtime.event import RunEvent
    log.append(RunEvent(
        run_id="run_seq", type=EventType.RUN_CREATED, seq=1, ts=time.time(),
        payload={"workflow_name": "test_wf"},
    ))
    log.append(RunEvent(
        run_id="run_seq", type=EventType.RUN_STARTED, seq=2, ts=time.time(),
        payload={"status": "running"},
    ))

    await reconciler.reconcile()

    events = list(log.read_from())
    interrupted = [e for e in events if e.type == EventType.RUN_INTERRUPTED]
    assert len(interrupted) == 1
    assert interrupted[0].seq == 3  # 接续 seq=2


# ---------------------------------------------------------------------------
# 不自动 resume
# ---------------------------------------------------------------------------
async def test_reconcile_no_auto_resume(reconciler, checkpoint_store):
    """reconcile 后 run 仍为 interrupted，completed_steps 不变（未自动执行）。"""
    await _make_checkpoint(
        checkpoint_store, "run_6",
        status=RunStatus.RUNNING,
        completed_steps=["s1", "s2"],
    )

    result = await reconciler.reconcile()

    assert result.interrupted_count == 1
    cp = await checkpoint_store.load("run_6")
    # 仍为 interrupted，未自动 resume
    assert cp.status == RunStatus.INTERRUPTED
    # completed_steps 不变（未重新执行任何 step）
    assert cp.completed_steps == ["s1", "s2"]


# ---------------------------------------------------------------------------
# GC 清理：.tmp 残留
# ---------------------------------------------------------------------------
async def test_reconcile_gc_sweeps_tmp(reconciler, checkpoint_store, tmp_path):
    """预置 .tmp 文件 → reconcile 后删除。"""
    tmp_dir = tmp_path / "runs" / "run_7" / "artifacts" / "step_a"
    tmp_dir.mkdir(parents=True)
    tmp_file = tmp_dir / "crashed.tmp"
    tmp_file.write_bytes(b"partial")

    result = await reconciler.reconcile()

    assert result.gc_stats["deleted_tmp"] >= 1
    assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# GC 清理：孤儿文件
# ---------------------------------------------------------------------------
async def test_reconcile_gc_sweeps_orphan_after_grace(
    reconciler, checkpoint_store, tmp_path,
):
    """超宽限期的孤儿文件 → reconcile 后删除。"""
    run_dir = tmp_path / "runs" / "run_orphan" / "artifacts" / "step_a"
    run_dir.mkdir(parents=True)
    orphan = run_dir / "orphan_art"
    orphan.write_bytes(b"orphan")
    # mtime 设为 25 小时前（超默认 24h 宽限期）
    old_time = time.time() - 25 * 3600
    os.utime(orphan, (old_time, old_time))

    result = await reconciler.reconcile()

    assert result.gc_stats["deleted_orphan"] >= 1
    assert not orphan.exists()


async def test_reconcile_gc_keeps_orphan_within_grace(
    reconciler, checkpoint_store, tmp_path,
):
    """宽限期内的孤儿文件 → reconcile 后保留。"""
    run_dir = tmp_path / "runs" / "run_grace" / "artifacts" / "step_a"
    run_dir.mkdir(parents=True)
    recent = run_dir / "recent_art"
    recent.write_bytes(b"recent")
    # mtime 设为 1 小时前（默认宽限 24h，未超）
    recent_time = time.time() - 3600
    os.utime(recent, (recent_time, recent_time))

    result = await reconciler.reconcile()

    assert result.gc_stats["deleted_orphan"] == 0
    assert recent.exists()


# ---------------------------------------------------------------------------
# 损坏事件日志
# ---------------------------------------------------------------------------
async def test_reconcile_corrupt_log_counted(reconciler, checkpoint_store, tmp_path):
    """损坏 events.jsonl → reconcile 不中断，event_log_corrupt 计数正确。"""
    # completed run（不会被对账为 zombie）+ 损坏的 events.jsonl
    await _make_checkpoint(checkpoint_store, "run_corrupt", status=RunStatus.COMPLETED)

    log_dir = tmp_path / "runs" / "run_corrupt"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "events.jsonl"
    log_file.write_text("not a json line\n{invalid json\n")

    result = await reconciler.reconcile()

    assert result.event_log_corrupt >= 1


async def test_reconcile_valid_log_not_counted(reconciler, checkpoint_store, tmp_path):
    """正常的 events.jsonl 不被计为损坏。"""
    await _make_checkpoint(checkpoint_store, "run_valid", status=RunStatus.COMPLETED)

    log_dir = tmp_path / "runs" / "run_valid"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "events.jsonl"
    valid_event = {
        "v": 1, "seq": 1, "run_id": "run_valid",
        "type": EventType.RUN_COMPLETED, "ts": time.time(),
        "step_id": None, "attempt": None,
        "payload": {"status": "completed"},
    }
    log_file.write_text(json.dumps(valid_event) + "\n")

    result = await reconciler.reconcile()

    assert result.event_log_corrupt == 0


# ---------------------------------------------------------------------------
# 综合场景
# ---------------------------------------------------------------------------
async def test_reconcile_multiple_runs(reconciler, checkpoint_store):
    """多 run 混合：running + cancelling + completed + failed + interrupted。"""
    await _make_checkpoint(checkpoint_store, "r1", status=RunStatus.RUNNING)
    await _make_checkpoint(checkpoint_store, "r2", status=RunStatus.CANCELLING)
    await _make_checkpoint(checkpoint_store, "r3", status=RunStatus.COMPLETED)
    await _make_checkpoint(checkpoint_store, "r4", status=RunStatus.FAILED)
    await _make_checkpoint(checkpoint_store, "r5", status=RunStatus.INTERRUPTED)

    result = await reconciler.reconcile()

    assert result.interrupted_count == 2  # r1 + r2
    # 僵尸 run → interrupted
    for rid in ["r1", "r2"]:
        cp = await checkpoint_store.load(rid)
        assert cp.status == RunStatus.INTERRUPTED
        assert cp.error == "process_restart"
    # 非僵尸 run → 状态不变
    assert (await checkpoint_store.load("r3")).status == RunStatus.COMPLETED
    assert (await checkpoint_store.load("r4")).status == RunStatus.FAILED
    assert (await checkpoint_store.load("r5")).status == RunStatus.INTERRUPTED


async def test_reconcile_empty_store(reconciler):
    """空 checkpoint store → reconcile 返回零计数，不抛异常。"""
    result = await reconciler.reconcile()

    assert result.interrupted_count == 0
    assert result.gc_stats == {"deleted_tmp": 0, "deleted_orphan": 0, "skipped": 0}
    assert result.event_log_corrupt == 0


# ---------------------------------------------------------------------------
# 幂等性：重复 reconcile 不重复发事件
# ---------------------------------------------------------------------------
async def test_reconcile_idempotent(reconciler, checkpoint_store):
    """重复 reconcile 不重复发 run_interrupted 事件。"""
    await _make_checkpoint(checkpoint_store, "run_idem", status=RunStatus.RUNNING)

    # 第一次 reconcile：running → interrupted
    result1 = await reconciler.reconcile()
    assert result1.interrupted_count == 1

    log = EventLog("run_idem", base_dir=reconciler._base_dir)
    count1 = sum(
        1 for e in log.read_from() if e.type == EventType.RUN_INTERRUPTED
    )
    assert count1 == 1

    # 第二次 reconcile：已是 interrupted，不再是 zombie
    result2 = await reconciler.reconcile()
    assert result2.interrupted_count == 0

    # 仍只有 1 个 run_interrupted 事件
    log2 = EventLog("run_idem", base_dir=reconciler._base_dir)
    count2 = sum(
        1 for e in log2.read_from() if e.type == EventType.RUN_INTERRUPTED
    )
    assert count2 == 1


# ---------------------------------------------------------------------------
# ReconcileResult 默认值
# ---------------------------------------------------------------------------
def test_reconcile_result_defaults():
    """ReconcileResult 默认值正确。"""
    result = ReconcileResult()
    assert result.interrupted_count == 0
    assert result.gc_stats == {}
    assert result.event_log_corrupt == 0
