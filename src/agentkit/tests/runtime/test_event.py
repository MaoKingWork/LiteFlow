"""RunEvent / EventBus / EventLog：协议 + 背压 + 日志先行 + 续传。

出口标准（对齐 §8 风险 3.3 验证）：
    - seq 单调递增
    - 日志先行：publish 返回前 EventLog 已落盘
    - SSE 续传：read_from(seq) 补齐缺失事件，无丢失无重复
    - 背压分级：可靠级超时降级、delta 合并不丢最终态
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from agentkit.runtime.event import (
    EVENT_PROTOCOL_VERSION,
    EventBus,
    EventLog,
    EventType,
    RunEvent,
)


# ---------------------------------------------------------------------------
# RunEvent 序列化往返
# ---------------------------------------------------------------------------
def test_run_event_to_dict_roundtrip():
    """to_dict / from_dict 互为逆运算。"""
    ev = RunEvent(
        v=1,
        seq=42,
        run_id="run_abc",
        type=EventType.STEP_FINISHED,
        ts=1721300000.123,
        step_id="analyze",
        attempt=0,
        payload={"status": "success", "duration_ms": 12.5},
    )
    d = ev.to_dict()
    assert d["seq"] == 42
    assert d["type"] == "step_finished"
    ev2 = RunEvent.from_dict(d)
    assert ev2 == ev


def test_run_event_from_dict_lenient():
    """from_dict 宽松处理未知字段（向前兼容）。"""
    d = {
        "v": 1, "seq": 1, "run_id": "r", "type": "x", "ts": 0.0,
        "unknown_future_field": "ignored",
    }
    ev = RunEvent.from_dict(d)
    assert ev.seq == 1
    assert ev.step_id is None  # 默认值


def test_run_event_jsonl_single_line():
    """to_jsonl 单行无换行；from_jsonl 可逆。"""
    ev = RunEvent(seq=1, type=EventType.RUN_STARTED, payload={"a": 1})
    line = ev.to_jsonl()
    assert "\n" not in line
    assert line.startswith("{") and line.endswith("}")
    ev2 = RunEvent.from_jsonl(line)
    assert ev2.seq == 1
    assert ev2.type == EventType.RUN_STARTED


def test_event_protocol_version_constant():
    """协议版本常量为 1。"""
    assert EVENT_PROTOCOL_VERSION == 1
    assert RunEvent().v == 1


# ---------------------------------------------------------------------------
# EventLog：JSONL 持久化 + 续传
# ---------------------------------------------------------------------------
def test_event_log_append_and_read(tmp_path):
    """append 后 read_from 可读到，seq 单调。"""
    log = EventLog("run_x", base_dir=str(tmp_path))
    ev1 = RunEvent(seq=1, run_id="run_x", type=EventType.RUN_STARTED, ts=1.0)
    ev2 = RunEvent(seq=2, run_id="run_x", type=EventType.STEP_STARTED, ts=2.0,
                   step_id="s1")
    ev3 = RunEvent(seq=3, run_id="run_x", type=EventType.STEP_FINISHED, ts=3.0,
                   step_id="s1")
    log.append(ev1)
    log.append(ev2)
    log.append(ev3)

    # 全量读
    events = list(log.read_from())
    assert [e.seq for e in events] == [1, 2, 3]

    # 续传：从 seq=2 起
    events = list(log.read_from(seq=2))
    assert [e.seq for e in events] == [2, 3]


def test_event_log_creates_parent_dir(tmp_path):
    """EventLog 构造时自动创建父目录。"""
    base = tmp_path / "nested" / "runs"
    log = EventLog("run_y", base_dir=str(base))
    assert os.path.isdir(str(base / "run_y"))


def test_event_log_read_from_empty(tmp_path):
    """文件不存在时 read_from 返回空迭代器。"""
    log = EventLog("run_z", base_dir=str(tmp_path))
    assert list(log.read_from()) == []
    assert log.latest_seq() == 0


def test_event_log_latest_seq(tmp_path):
    """latest_seq 返回当前最大 seq。"""
    log = EventLog("run_l", base_dir=str(tmp_path))
    for i in range(1, 6):
        log.append(RunEvent(seq=i, type=EventType.RUN_STARTED, ts=float(i)))
    assert log.latest_seq() == 5


def test_event_log_skips_corrupt_lines(tmp_path):
    """损坏行跳过并记 warning，不影响其他行。"""
    log = EventLog("run_c", base_dir=str(tmp_path))
    log.append(RunEvent(seq=1, type=EventType.RUN_STARTED, ts=1.0))
    # 手动追加损坏行
    with open(log.path, "a", encoding="utf-8") as f:
        f.write("not a json\n")
        f.write("{invalid\n")
    log.append(RunEvent(seq=2, type=EventType.RUN_COMPLETED, ts=2.0))

    events = list(log.read_from())
    assert [e.seq for e in events] == [1, 2]


def test_event_log_jsonl_format(tmp_path):
    """每行是合法 JSON，且以 \\n 结尾。"""
    log = EventLog("run_f", base_dir=str(tmp_path))
    log.append(RunEvent(seq=1, type=EventType.RUN_STARTED, ts=1.0))
    log.append(RunEvent(seq=2, type=EventType.RUN_COMPLETED, ts=2.0))

    with open(log.path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 2
    for line in lines:
        assert line.endswith("\n")
        json.loads(line)  # 不抛异常即合法


# ---------------------------------------------------------------------------
# EventBus：seq 分配 + 日志先行 + 订阅
# ---------------------------------------------------------------------------
async def test_event_bus_seq_monotonic(tmp_path):
    """seq 由 bus 单点分配，单调递增。"""
    log = EventLog("run_m", base_dir=str(tmp_path))
    bus = EventBus("run_m", log=log)
    seqs = []
    for _ in range(5):
        seq = await bus.next_seq()
        seqs.append(seq)
    assert seqs == [1, 2, 3, 4, 5]


async def test_event_bus_publish_fills_fields(tmp_path):
    """publish 填充 run_id / ts / seq。"""
    log = EventLog("run_p", base_dir=str(tmp_path))
    bus = EventBus("run_p", log=log)
    ev = RunEvent(type=EventType.RUN_STARTED)
    await bus.publish(ev)
    assert ev.run_id == "run_p"
    assert ev.ts > 0
    assert ev.seq == 1


async def test_event_bus_log_first(tmp_path):
    """日志先行：publish 返回前 EventLog 已落盘。"""
    log = EventLog("run_lf", base_dir=str(tmp_path))
    bus = EventBus("run_lf", log=log)
    await bus.publish(RunEvent(type=EventType.RUN_STARTED))
    await bus.publish(RunEvent(type=EventType.STEP_STARTED, step_id="s1"))

    events = list(log.read_from())
    assert len(events) == 2
    assert events[0].type == EventType.RUN_STARTED
    assert events[1].step_id == "s1"


async def test_event_bus_subscribe_receives_events(tmp_path):
    """per-subscriber queue：订阅者收到 publish 的事件。"""
    log = EventLog("run_s", base_dir=str(tmp_path))
    bus = EventBus("run_s", log=log)
    sub = await bus.subscribe()

    await bus.publish(RunEvent(type=EventType.RUN_STARTED))
    await bus.publish(RunEvent(type=EventType.STEP_STARTED, step_id="s1"))

    # 取两个事件
    ev1 = await asyncio.wait_for(sub.get(), timeout=1.0)
    ev2 = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev1 is not None
    assert ev2 is not None
    assert ev1.type == EventType.RUN_STARTED
    assert ev2.step_id == "s1"
    sub.cancel()


async def test_event_bus_multiple_subscribers(tmp_path):
    """多订阅者各自独立收到事件（广播）。"""
    log = EventLog("run_ms", base_dir=str(tmp_path))
    bus = EventBus("run_ms", log=log)
    sub1 = await bus.subscribe()
    sub2 = await bus.subscribe()

    await bus.publish(RunEvent(type=EventType.RUN_STARTED))

    ev1 = await asyncio.wait_for(sub1.get(), timeout=1.0)
    ev2 = await asyncio.wait_for(sub2.get(), timeout=1.0)
    assert ev1 is not None and ev2 is not None
    assert ev1.seq == ev2.seq  # 同一事件
    sub1.cancel()
    sub2.cancel()


async def test_event_bus_subscription_async_iteration(tmp_path):
    """订阅支持 async for 迭代。"""
    log = EventLog("run_ai", base_dir=str(tmp_path))
    bus = EventBus("run_ai", log=log)
    sub = await bus.subscribe()

    await bus.publish(RunEvent(type=EventType.RUN_STARTED))
    await bus.publish(RunEvent(type=EventType.RUN_COMPLETED))

    received = []
    # 用 wait_for 限时避免阻塞
    async def collect():
        async for ev in sub:
            received.append(ev)
            if len(received) >= 2:
                break
    await asyncio.wait_for(collect(), timeout=1.0)
    assert len(received) == 2
    sub.cancel()


# ---------------------------------------------------------------------------
# 背压分级
# ---------------------------------------------------------------------------
async def test_event_bus_reliable_drops_on_full(tmp_path):
    """可靠级事件队列满时超时降级（已落日志，分发丢弃）。"""
    log = EventLog("run_full", base_dir=str(tmp_path))
    # queue_size=1 + reliable_block_timeout 极小，触发降级
    bus = EventBus("run_full", log=log, queue_size=1,
                   reliable_block_timeout=0.05)
    sub = await bus.subscribe()

    # 第一条入队（队列满）
    await bus.publish(RunEvent(type=EventType.RUN_STARTED))
    # 第二条阻塞后超时降级（已落日志）
    await bus.publish(RunEvent(type=EventType.STEP_STARTED, step_id="s1"))

    # 日志两条都在（日志先行）
    events = list(log.read_from())
    assert len(events) == 2

    # 订阅者只收到第一条（第二条降级丢弃）
    ev1 = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev1 is not None
    assert ev1.type == EventType.RUN_STARTED
    # 短时间内不应有第二条
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.get(), timeout=0.2)
    sub.cancel()


async def test_event_bus_delta_mergeable(tmp_path):
    """llm_delta 可合并级：50ms 窗口内合并。"""
    log = EventLog("run_dm", base_dir=str(tmp_path))
    bus = EventBus("run_dm", log=log, delta_merge_window=0.05)
    sub = await bus.subscribe()

    # 连续发 3 个 delta，窗口内合并
    for i, d in enumerate(["a", "b", "c"]):
        await bus.publish(RunEvent(
            type=EventType.LLM_DELTA,
            step_id="s1",
            payload={"delta": d, "accumulated_len": i + 1},
        ))

    # 等待 flush（窗口 + 余量）
    await asyncio.sleep(0.15)

    # 日志中所有 delta 都落盘（日志先行，不合并日志）
    log_events = list(log.read_from())
    deltas = [e for e in log_events if e.type == EventType.LLM_DELTA]
    assert len(deltas) == 3

    # 订阅者收到合并后的事件（至少 1 个，delta 字段为合并文本）
    received = []
    for _ in range(3):
        try:
            ev = await asyncio.wait_for(sub.get(), timeout=0.2)
            if ev is not None and ev.type == EventType.LLM_DELTA:
                received.append(ev)
        except asyncio.TimeoutError:
            break
    # 合并后至少 1 个 delta 事件，文本包含所有片段
    assert len(received) >= 1
    merged_text = "".join(r.payload.get("delta", "") for r in received)
    assert "a" in merged_text and "b" in merged_text and "c" in merged_text
    sub.cancel()


# ---------------------------------------------------------------------------
# 便捷发布方法
# ---------------------------------------------------------------------------
async def test_event_bus_publish_run_created(tmp_path):
    """publish_run_created 便捷方法。"""
    log = EventLog("run_pc", base_dir=str(tmp_path))
    bus = EventBus("run_pc", log=log)
    await bus.publish_run_created(workflow_name="wf_x")

    events = list(log.read_from())
    assert len(events) == 1
    assert events[0].type == EventType.RUN_CREATED
    assert events[0].payload["workflow_name"] == "wf_x"


async def test_event_bus_publish_run_lifecycle(tmp_path):
    """publish_run_lifecycle 状态映射到对应事件类型。"""
    log = EventLog("run_pl", base_dir=str(tmp_path))
    bus = EventBus("run_pl", log=log)
    await bus.publish_run_lifecycle("running")
    await bus.publish_run_lifecycle("completed")
    await bus.publish_run_lifecycle("failed", error="boom")
    await bus.publish_run_lifecycle("cancelling")
    await bus.publish_run_lifecycle("cancelled")
    await bus.publish_run_lifecycle("interrupted")

    events = list(log.read_from())
    types = [e.type for e in events]
    assert types == [
        EventType.RUN_STARTED,
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLING,
        EventType.RUN_CANCELLED,
        EventType.RUN_INTERRUPTED,
    ]
    # failed 事件带 error
    failed_ev = events[2]
    assert failed_ev.payload["error"] == "boom"


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------
async def test_event_bus_close_cancels_subscriptions(tmp_path):
    """close 取消所有订阅者。"""
    log = EventLog("run_cl", base_dir=str(tmp_path))
    bus = EventBus("run_cl", log=log)
    sub = await bus.subscribe()
    await bus.close()
    assert sub._closed
