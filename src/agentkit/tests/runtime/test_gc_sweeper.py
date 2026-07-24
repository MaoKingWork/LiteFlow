"""GCSweeper：孤儿文件对账 + .tmp 残留清理。

出口标准（对齐 §5.6 GCSweeper）：
    - .tmp 残留立即删（崩溃窗口 1-3 兜底）
    - 未被 events.jsonl 引用且超宽限期的文件视为孤儿，删除
    - 被 artifact_produced 事件引用的文件不删
    - 宽限期内未引用文件保留（防误删并发写入中的文件）
    - 损坏 events.jsonl 行跳过，不影响其他 run 对账
"""
from __future__ import annotations

import json
import os
import time

import pytest

from agentkit.runtime.artifact import ArtifactStore, GCSweeper
from agentkit.runtime.event import EventBus, EventLog, EventType, RunEvent


# ---------------------------------------------------------------------------
# 辅助：构造一个产物 + 对应事件
# ---------------------------------------------------------------------------
async def _make_artifact_with_event(
    base_dir: str,
    run_id: str,
    step_id: str,
    artifact_id: str,
    content: bytes = b"content",
) -> str:
    """创建产物 + 写入对应 artifact_produced 事件，返回产物路径。"""
    log = EventLog(run_id, base_dir=base_dir)
    bus = EventBus(run_id, log=log)
    store = ArtifactStore(run_id, event_bus=bus, base_dir=base_dir)
    ref = await store.save(step_id, content, artifact_id=artifact_id)
    return ref.uri


# ---------------------------------------------------------------------------
# .tmp 残留清理
# ---------------------------------------------------------------------------
def test_sweep_deletes_tmp_files(tmp_path):
    """.tmp 残留立即删（崩溃窗口 1-3 兜底）。"""
    # 创建几个 .tmp 残留
    run_dir = tmp_path / "run_1" / "artifacts" / "step_a"
    run_dir.mkdir(parents=True)
    (run_dir / "crashed1.tmp").write_bytes(b"partial1")
    (run_dir / "crashed2.tmp").write_bytes(b"partial2")

    # 创建一个正常的非 tmp 文件（不应被 .tmp 清理影响）
    (run_dir / "good_art").write_bytes(b"good")

    sweeper = GCSweeper(base_dir=str(tmp_path))
    stats = sweeper.sweep_once()

    assert stats["deleted_tmp"] == 2
    assert not (run_dir / "crashed1.tmp").exists()
    assert not (run_dir / "crashed2.tmp").exists()
    # good_art 因无 events.jsonl 引用且超宽限期默认 24h，可能被当孤儿删
    # 这里仅验证 .tmp 清理，good_art 的去留由孤儿规则测试覆盖


def test_sweep_tmp_in_multiple_runs(tmp_path):
    """跨多个 run 目录的 .tmp 都被清理。"""
    for run_id in ["run_a", "run_b", "run_c"]:
        d = tmp_path / run_id / "artifacts" / "step_x"
        d.mkdir(parents=True)
        (d / "residual.tmp").write_bytes(b"x")

    sweeper = GCSweeper(base_dir=str(tmp_path))
    stats = sweeper.sweep_once()
    assert stats["deleted_tmp"] == 3


def test_sweep_no_tmp_returns_zero(tmp_path):
    """无 .tmp 残留时 deleted_tmp=0。"""
    d = tmp_path / "run_x" / "artifacts" / "step_y"
    d.mkdir(parents=True)
    (d / "artifact_1").write_bytes(b"good")

    sweeper = GCSweeper(base_dir=str(tmp_path))
    stats = sweeper.sweep_once()
    assert stats["deleted_tmp"] == 0


# ---------------------------------------------------------------------------
# 孤儿文件对账
# ---------------------------------------------------------------------------
async def test_sweep_keeps_referenced_artifacts(tmp_path):
    """被 artifact_produced 事件引用的文件不删。"""
    # 正常创建一个有事件的 artifact
    uri = await _make_artifact_with_event(
        str(tmp_path), "run_1", "step_a", "art_referenced", b"good content",
    )
    assert os.path.isfile(uri)

    sweeper = GCSweeper(base_dir=str(tmp_path), orphan_grace_seconds=0)
    stats = sweeper.sweep_once()

    # 被引用的文件保留
    assert os.path.isfile(uri), "被引用的产物不应被清理"
    assert stats["deleted_orphan"] == 0


async def test_sweep_deletes_orphan_after_grace(tmp_path):
    """未被引用且超宽限期的文件视为孤儿，删除。"""
    # 创建一个无事件的孤儿文件
    run_dir = tmp_path / "run_orphan" / "artifacts" / "step_a"
    run_dir.mkdir(parents=True)
    orphan_file = run_dir / "orphan_art"
    orphan_file.write_bytes(b"orphan")

    # 修改 mtime 为 25 小时前（超出默认 24h 宽限期）
    old_time = time.time() - 25 * 3600
    os.utime(orphan_file, (old_time, old_time))

    sweeper = GCSweeper(base_dir=str(tmp_path), orphan_grace_seconds=24 * 3600)
    stats = sweeper.sweep_once()

    assert stats["deleted_orphan"] == 1
    assert not orphan_file.exists()


async def test_sweep_keeps_orphan_within_grace(tmp_path):
    """未引用但仍在宽限期内的文件保留（防误删并发写入中的文件）。"""
    run_dir = tmp_path / "run_grace" / "artifacts" / "step_a"
    run_dir.mkdir(parents=True)
    recent_file = run_dir / "recent_art"
    recent_file.write_bytes(b"recent")

    # mtime 为 1 小时前（默认宽限 24h，未超）
    recent_time = time.time() - 3600
    os.utime(recent_file, (recent_time, recent_time))

    sweeper = GCSweeper(base_dir=str(tmp_path), orphan_grace_seconds=24 * 3600)
    stats = sweeper.sweep_once()

    assert stats["deleted_orphan"] == 0
    assert recent_file.exists(), "宽限期内文件不应被删"


def test_sweep_zero_grace_deletes_immediately(tmp_path):
    """orphan_grace_seconds=0 时未引用文件立即删。"""
    run_dir = tmp_path / "run_zero" / "artifacts" / "step_a"
    run_dir.mkdir(parents=True)
    orphan = run_dir / "unreferenced"
    orphan.write_bytes(b"trash")

    sweeper = GCSweeper(base_dir=str(tmp_path), orphan_grace_seconds=0)
    stats = sweeper.sweep_once()
    assert stats["deleted_orphan"] == 1
    assert not orphan.exists()


# ---------------------------------------------------------------------------
# 边界情况
# ---------------------------------------------------------------------------
def test_sweep_nonexistent_base_dir(tmp_path):
    """base_dir 不存在时返回空统计，不抛异常。"""
    sweeper = GCSweeper(base_dir=str(tmp_path / "nonexistent"))
    stats = sweeper.sweep_once()
    assert stats == {"deleted_tmp": 0, "deleted_orphan": 0, "skipped": 0}


def test_sweep_empty_base_dir(tmp_path):
    """base_dir 为空目录时返回空统计。"""
    sweeper = GCSweeper(base_dir=str(tmp_path))
    stats = sweeper.sweep_once()
    assert stats["deleted_tmp"] == 0
    assert stats["deleted_orphan"] == 0


async def test_sweep_skips_events_jsonl(tmp_path):
    """sweep 不删 events.jsonl（即使无 artifact_produced 事件）。"""
    run_dir = tmp_path / "run_log"
    run_dir.mkdir()
    log_file = run_dir / "events.jsonl"
    log_file.write_text(
        json.dumps({"type": "run_started", "seq": 1}) + "\n"
    )

    sweeper = GCSweeper(base_dir=str(tmp_path), orphan_grace_seconds=0)
    stats = sweeper.sweep_once()

    assert log_file.exists(), "events.jsonl 不应被删"
    # events.jsonl 不是 .tmp，也不是 artifact，应跳过
    assert stats["deleted_orphan"] == 0


async def test_sweep_handles_corrupt_events_jsonl(tmp_path):
    """损坏的 events.jsonl 行跳过，不影响其他 run 对账。"""
    # run_1: 正常的 artifact + 事件
    uri1 = await _make_artifact_with_event(
        str(tmp_path), "run_1", "step_a", "art_1", b"good",
    )
    # run_2: 损坏的 events.jsonl + 一个孤儿文件
    run2_dir = tmp_path / "run_2"
    run2_dir.mkdir()
    log2 = run2_dir / "events.jsonl"
    log2.write_text("not a json line\n{invalid\n")
    art2_dir = run2_dir / "artifacts" / "step_b"
    art2_dir.mkdir(parents=True)
    orphan = art2_dir / "orphan"
    orphan.write_bytes(b"orphan")
    old_time = time.time() - 48 * 3600
    os.utime(orphan, (old_time, old_time))

    sweeper = GCSweeper(base_dir=str(tmp_path), orphan_grace_seconds=24 * 3600)
    stats = sweeper.sweep_once()

    # run_1 的 artifact 被引用，保留
    assert os.path.isfile(uri1)
    # run_2 的损坏 events.jsonl 不影响孤儿识别，orphan 被删
    assert not orphan.exists()
    assert stats["deleted_orphan"] >= 1


# ---------------------------------------------------------------------------
# 集成：与 ArtifactStore 协作
# ---------------------------------------------------------------------------
async def test_sweep_after_normal_run_keeps_everything(tmp_path):
    """正常 run 后 sweep 不删任何产物（所有产物都被事件引用）。"""
    log = EventLog("run_normal", base_dir=str(tmp_path))
    bus = EventBus("run_normal", log=log)
    store = ArtifactStore("run_normal", event_bus=bus, base_dir=str(tmp_path))

    # 创建 3 个正常产物
    await store.save("step_1", "a", artifact_id="art_1")
    await store.save("step_1", "b", artifact_id="art_2")
    await store.save("step_2", "c", artifact_id="art_3")

    sweeper = GCSweeper(base_dir=str(tmp_path), orphan_grace_seconds=0)
    stats = sweeper.sweep_once()

    # 所有产物保留
    artifacts = store.list_artifacts()
    assert len(artifacts) == 3
    for ref in artifacts:
        assert os.path.isfile(ref.uri)
    assert stats["deleted_orphan"] == 0
    assert stats["deleted_tmp"] == 0


async def test_sweep_cleans_residual_after_simulated_crash(tmp_path):
    """模拟崩溃后 sweep 清理 .tmp 残留，保留已 publish 的产物。"""
    log = EventLog("run_crash", base_dir=str(tmp_path))
    bus = EventBus("run_crash", log=log)
    store = ArtifactStore("run_crash", event_bus=bus, base_dir=str(tmp_path))

    # 一个正常的产物
    ref = await store.save("step_1", "good", artifact_id="art_good")

    # 模拟崩溃：手动创建一个 .tmp 残留
    artifact_dir = os.path.dirname(ref.uri)
    tmp_residual = os.path.join(artifact_dir, "crashed.tmp")
    with open(tmp_residual, "wb") as f:
        f.write(b"partial")

    sweeper = GCSweeper(base_dir=str(tmp_path), orphan_grace_seconds=0)
    stats = sweeper.sweep_once()

    assert stats["deleted_tmp"] == 1
    assert not os.path.exists(tmp_residual)
    # 正常产物保留
    assert os.path.isfile(ref.uri)


# ---------------------------------------------------------------------------
# 多 run 混合场景
# ---------------------------------------------------------------------------
async def test_sweep_multi_run_mixed(tmp_path):
    """多 run 混合场景：referenced / orphan / tmp 残留并存。"""
    # run_A: 正常产物（被引用）
    uri_a = await _make_artifact_with_event(
        str(tmp_path), "run_A", "step_a", "art_a", b"a",
    )
    # run_B: 孤儿文件（无事件，超宽限期）
    run_b_dir = tmp_path / "run_B" / "artifacts" / "step_b"
    run_b_dir.mkdir(parents=True)
    orphan_b = run_b_dir / "orphan_b"
    orphan_b.write_bytes(b"orphan")
    old_time = time.time() - 48 * 3600
    os.utime(orphan_b, (old_time, old_time))

    # run_C: .tmp 残留
    run_c_dir = tmp_path / "run_C" / "artifacts" / "step_c"
    run_c_dir.mkdir(parents=True)
    tmp_c = run_c_dir / "residual.tmp"
    tmp_c.write_bytes(b"partial")

    sweeper = GCSweeper(base_dir=str(tmp_path), orphan_grace_seconds=24 * 3600)
    stats = sweeper.sweep_once()

    # run_A 产物保留
    assert os.path.isfile(uri_a)
    # run_B 孤儿删除
    assert not orphan_b.exists()
    # run_C tmp 残留删除
    assert not tmp_c.exists()
    assert stats["deleted_tmp"] == 1
    assert stats["deleted_orphan"] == 1


def test_sweep_stats_keys(tmp_path):
    """sweep_once 返回的 stats 包含三个固定 key。"""
    sweeper = GCSweeper(base_dir=str(tmp_path))
    stats = sweeper.sweep_once()
    assert set(stats.keys()) == {"deleted_tmp", "deleted_orphan", "skipped"}
