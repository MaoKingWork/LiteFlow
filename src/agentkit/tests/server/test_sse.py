"""server.sse —— SSE 事件流推送 + Last-Event-ID 续传测试（E3）。

出口标准（对齐 P1 §E3）:
    - run 已结束 → 回放全部历史后关闭
    - Last-Event-ID=N → 只返回 seq>N 的事件
    - run 进行中 → 先补历史 → 接 live → 收到终态后断开
    - 断开重连后 seq 连续无丢失无重复
    - run 已 completed → 只回放历史,不接 live
    - 不存在的 run_id → 返回空
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from agentkit.runtime.event import EventLog, EventType, RunEvent
from agentkit.server.sse import (
    TERMINAL_TYPES,
    create_sse_response,
    event_stream,
)


# ---------------------------------------------------------------------------
# 辅助：mock RunManager
# ---------------------------------------------------------------------------
class _MockRM:
    """最小 RunManager mock（get 返回固定 handle 或 None）。"""

    def __init__(self, base_dir: str, handle=None):
        self._base_dir = base_dir
        self._handle = handle

    def get(self, run_id: str):
        return self._handle


def _write_events(tmp_path, run_id="run_1", count=4, terminal=True):
    """写 count 个事件到 EventLog,最后一个可选为终态。"""
    log = EventLog(run_id, base_dir=str(tmp_path))
    for i in range(1, count + 1):
        is_last = (i == count)
        if is_last and terminal:
            ev_type = EventType.RUN_COMPLETED
        elif is_last:
            ev_type = EventType.STEP_FINISHED
        elif i == 1:
            ev_type = EventType.RUN_STARTED
        else:
            ev_type = EventType.STEP_FINISHED
        log.append(RunEvent(
            run_id=run_id,
            seq=i,
            type=ev_type,
            step_id=f"s{i}" if "step" in ev_type else None,
            payload={"seq": i},
        ))


async def _collect(stream) -> list:
    """收集 async generator 的所有 yield。"""
    result = []
    async for item in stream:
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# 历史回放
# ---------------------------------------------------------------------------
async def test_sse_replay_history(tmp_path):
    """run 已结束,Last-Event-ID=0 → 返回全部历史事件后关闭。"""
    _write_events(tmp_path, count=4, terminal=True)
    rm = _MockRM(str(tmp_path), handle=None)
    events = await _collect(event_stream("run_1", 0, rm))
    assert len(events) == 4
    assert events[-1]["event"] == EventType.RUN_COMPLETED
    assert events[-1]["id"] == "4"


async def test_sse_resume_from_id(tmp_path):
    """Last-Event-ID=2 → 只返回 seq>2 的事件。"""
    _write_events(tmp_path, count=4, terminal=True)
    rm = _MockRM(str(tmp_path), handle=None)
    events = await _collect(event_stream("run_1", 2, rm))
    assert len(events) == 2  # seq=3, 4
    assert events[0]["id"] == "3"
    assert events[1]["id"] == "4"


async def test_sse_unknown_run(tmp_path):
    """不存在的 run_id → 返回空。"""
    rm = _MockRM(str(tmp_path), handle=None)
    events = await _collect(event_stream("nonexistent", 0, rm))
    assert len(events) == 0


async def test_sse_finished_run_no_live(tmp_path):
    """run 已 completed → 只回放历史,不接 live。"""
    _write_events(tmp_path, count=2, terminal=True)
    # handle=None 模拟 run 不在内存（已结束）
    rm = _MockRM(str(tmp_path), handle=None)
    events = await _collect(event_stream("run_1", 0, rm))
    assert len(events) == 2
    assert events[-1]["event"] == EventType.RUN_COMPLETED


# ---------------------------------------------------------------------------
# seq 连续性（断开重连）
# ---------------------------------------------------------------------------
async def test_sse_seq_continuous(tmp_path):
    """断开重连后事件 seq 连续无丢失无重复。"""
    _write_events(tmp_path, count=10, terminal=True)
    rm = _MockRM(str(tmp_path), handle=None)

    # 第一次消费到 seq=5 后断开
    seen = []
    async for sse in event_stream("run_1", 0, rm):
        seen.append(sse)
        if int(sse["id"]) >= 5:
            break

    # 第二次从 seq=5 续传
    async for sse in event_stream("run_1", 5, rm):
        seen.append(sse)

    seqs = [int(s["id"]) for s in seen]
    assert seqs == list(range(1, 11))  # 1-10 无丢失无重复


# ---------------------------------------------------------------------------
# live 订阅（真实 RunManager）
# ---------------------------------------------------------------------------
async def test_sse_live_then_terminal(tmp_path):
    """run 进行中 → 先补历史 → 接 live → 收到终态后断开。"""
    from agentkit.core.checkpoint import LocalCheckpointStore
    from agentkit.runtime.run_manager import RunManager
    from agentkit.tools.base import Tool, register
    from agentkit.yaml.loader import load_workflow_from_dict

    class _QuickTool(Tool):
        name = "test.sse_quick"
        description = "quick tool for sse test"

        async def call(self, params, ctx):
            await asyncio.sleep(0.05)
            return {"done": True}

    register(_QuickTool())
    try:
        cp_store = LocalCheckpointStore(base_dir=str(tmp_path / "cp"))
        rm = RunManager(
            checkpoint_store=cp_store,
            base_dir=str(tmp_path / "runs"),
        )
        config = {
            "name": "sse_live_wf",
            "steps": [
                {"type": "tool", "tool": "test.sse_quick", "id": "s1", "output": "r"}
            ],
        }
        workflow = load_workflow_from_dict(config, base_dir=str(tmp_path))
        run_id = await rm.start(workflow)

        events = []
        async for sse in event_stream(run_id, 0, rm):
            events.append(sse)
            if sse["event"] in TERMINAL_TYPES:
                break

        types = [e["event"] for e in events]
        # 应该收到终态事件（completed 或 failed）
        assert any(t in TERMINAL_TYPES for t in types)
        # seq 单调递增
        seqs = [int(e["id"]) for e in events]
        assert seqs == sorted(seqs)
        assert seqs[0] == 1  # run_created 是 seq=1

        await rm.shutdown()
    finally:
        pass


# ---------------------------------------------------------------------------
# create_sse_response
# ---------------------------------------------------------------------------
def test_create_sse_response():
    """create_sse_response 返回 EventSourceResponse 实例。"""
    pytest.importorskip("sse_starlette")
    from sse_starlette import EventSourceResponse

    rm = _MockRM(".", handle=None)
    resp = create_sse_response("run_x", rm, 0)
    assert isinstance(resp, EventSourceResponse)
