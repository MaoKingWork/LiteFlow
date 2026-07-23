"""EventBusHooks：hook → RunEvent 翻译（执行引擎零改动）。

出口标准（对齐 §5.2）：
    - before_workflow → run_started
    - after_workflow (success) → run_completed
    - after_workflow (failure) → run_failed
    - before_step → step_started
    - after_step → step_finished (payload 对齐 StepTrace)
    - on_step_error → 标记 _workflow_failed，返回 RAISE
    - on_llm_stream_start → llm_stream_start
    - on_llm_stream_delta → llm_delta (可合并)
    - on_llm_stream_end → llm_stream_end
    - on_tool_call → tool_call
    - on_mcp_call → tool_call (payload 加 mcp_server)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from agentkit.core.agent import AgentConfig
from agentkit.core.hooks import ErrorAction
from agentkit.runtime.event import EventBus, EventLog, EventType, RunEvent
from agentkit.runtime.event_hooks import EventBusHooks


# ---------------------------------------------------------------------------
# 辅助：构造 mock Workflow / Step / StepTrace
# ---------------------------------------------------------------------------
@dataclass
class _MockWorkflow:
    name: str = "test_workflow"


@dataclass
class _MockStep:
    id: str = "step_1"
    type: str = "tool"


@dataclass
class _MockTrace:
    step_id: str = "step_1"
    status: str = "success"
    duration_ms: float = 12.5
    input_summary: str = "input preview"
    output_summary: str = "output preview"
    token_usage: int | None = 42
    error: str | None = None
    retry_count: int = 0
    tool_calls: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = [{"tool": "test.echo", "status": "ok"}]


@dataclass
class _MockTool:
    name: str = "test.echo"


# ---------------------------------------------------------------------------
# before_workflow → run_started
# ---------------------------------------------------------------------------
async def test_before_workflow_publishes_run_started(tmp_path):
    """before_workflow → run_started 事件，payload 含 workflow_name。"""
    log = EventLog("run_1", base_dir=str(tmp_path))
    bus = EventBus("run_1", log=log)
    hooks = EventBusHooks(bus, run_id="run_1")

    wf = _MockWorkflow(name="my_workflow")
    from agentkit.core.context import Context
    ctx = Context()
    await hooks.before_workflow(wf, ctx)

    events = list(log.read_from())
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.RUN_STARTED
    assert ev.run_id == "run_1"
    assert ev.payload["workflow_name"] == "my_workflow"


# ---------------------------------------------------------------------------
# after_workflow → run_completed / run_failed
# ---------------------------------------------------------------------------
async def test_after_workflow_success_publishes_run_completed(tmp_path):
    """after_workflow 在成功时 → run_completed 事件。"""
    log = EventLog("run_2", base_dir=str(tmp_path))
    bus = EventBus("run_2", log=log)
    hooks = EventBusHooks(bus, run_id="run_2")

    wf = _MockWorkflow()
    from agentkit.core.context import Context
    ctx = Context()
    await hooks.after_workflow(wf, ctx, ctx)

    events = list(log.read_from())
    assert len(events) == 1
    assert events[0].type == EventType.RUN_COMPLETED
    assert events[0].payload["status"] == "completed"


async def test_after_workflow_failure_publishes_run_failed(tmp_path):
    """after_workflow 在失败时 → run_failed 事件，payload 含 error。"""
    log = EventLog("run_3", base_dir=str(tmp_path))
    bus = EventBus("run_3", log=log)
    hooks = EventBusHooks(bus, run_id="run_3")

    # 模拟失败：先触发 on_step_error 设置失败标志
    from agentkit.core.context import Context
    step = _MockStep()
    ctx = Context()
    await hooks.on_step_error(step, ctx, RuntimeError("boom"))

    await hooks.after_workflow(_MockWorkflow(), ctx, ctx)

    events = list(log.read_from())
    # on_step_error 不直接发事件，只设置标志；after_workflow 发 run_failed
    assert len(events) == 1
    assert events[0].type == EventType.RUN_FAILED
    assert events[0].payload["status"] == "failed"
    assert "RuntimeError" in events[0].payload["error"]
    assert "boom" in events[0].payload["error"]


# ---------------------------------------------------------------------------
# on_step_error 行为
# ---------------------------------------------------------------------------
async def test_on_step_error_returns_raise(tmp_path):
    """on_step_error 返回 ErrorAction.RAISE（不干预重试决策）。"""
    log = EventLog("run_4", base_dir=str(tmp_path))
    bus = EventBus("run_4", log=log)
    hooks = EventBusHooks(bus, run_id="run_4")

    from agentkit.core.context import Context
    action = await hooks.on_step_error(
        _MockStep(), Context(), ValueError("err"),
    )
    assert action is ErrorAction.RAISE


async def test_on_step_error_sets_workflow_failed_flag(tmp_path):
    """on_step_error 设置 _workflow_failed 标志，影响 after_workflow 分支。"""
    log = EventLog("run_5", base_dir=str(tmp_path))
    bus = EventBus("run_5", log=log)
    hooks = EventBusHooks(bus, run_id="run_5")

    assert hooks._workflow_failed is False
    from agentkit.core.context import Context
    await hooks.on_step_error(_MockStep(), Context(), RuntimeError("err"))
    assert hooks._workflow_failed is True
    assert hooks._workflow_error is not None


# ---------------------------------------------------------------------------
# before_step → step_started
# ---------------------------------------------------------------------------
async def test_before_step_publishes_step_started(tmp_path):
    """before_step → step_started 事件，payload 含 step_type。"""
    log = EventLog("run_6", base_dir=str(tmp_path))
    bus = EventBus("run_6", log=log)
    hooks = EventBusHooks(bus, run_id="run_6")

    from agentkit.core.context import Context
    step = _MockStep(id="analyze", type="llm")
    await hooks.before_step(step, Context())

    events = list(log.read_from())
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.STEP_STARTED
    assert ev.step_id == "analyze"
    assert ev.payload["step_type"] == "llm"


async def test_before_step_falls_back_to_type_when_no_id(tmp_path):
    """step 无 id 时 step_id 回落到 type。"""
    log = EventLog("run_7", base_dir=str(tmp_path))
    bus = EventBus("run_7", log=log)
    hooks = EventBusHooks(bus, run_id="run_7")

    from agentkit.core.context import Context
    step = _MockStep(id="", type="tool")
    await hooks.before_step(step, Context())

    events = list(log.read_from())
    assert events[0].step_id == "tool"


# ---------------------------------------------------------------------------
# after_step → step_finished
# ---------------------------------------------------------------------------
async def test_after_step_publishes_step_finished_with_trace_fields(tmp_path):
    """after_step → step_finished 事件，payload 对齐 StepTrace 全字段。"""
    log = EventLog("run_8", base_dir=str(tmp_path))
    bus = EventBus("run_8", log=log)
    hooks = EventBusHooks(bus, run_id="run_8")

    from agentkit.core.context import Context
    step = _MockStep(id="s1", type="tool")
    trace = _MockTrace(
        step_id="s1",
        status="success",
        duration_ms=42.0,
        input_summary="in",
        output_summary="out",
        token_usage=100,
        error=None,
        retry_count=1,
        tool_calls=[{"tool": "test.echo", "status": "ok"}],
    )
    await hooks.after_step(step, Context(), trace)  # type: ignore[arg-type]

    events = list(log.read_from())
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.STEP_FINISHED
    assert ev.step_id == "s1"
    # payload 对齐 StepTrace 字段
    payload = ev.payload
    assert payload["step_type"] == "tool"
    assert payload["status"] == "success"
    assert payload["duration_ms"] == 42.0
    assert payload["token_usage"] == 100
    assert payload["error"] is None
    assert payload["retry_count"] == 1
    assert payload["input_summary"] == "in"
    assert payload["output_summary"] == "out"
    assert payload["tool_calls"] == [{"tool": "test.echo", "status": "ok"}]


# ---------------------------------------------------------------------------
# LLM 流式 hook
# ---------------------------------------------------------------------------
async def test_on_llm_stream_start_publishes_event(tmp_path):
    """on_llm_stream_start → llm_stream_start 事件。"""
    log = EventLog("run_9", base_dir=str(tmp_path))
    bus = EventBus("run_9", log=log)
    hooks = EventBusHooks(bus, run_id="run_9")

    step = _MockStep(id="llm_1", type="llm")
    agent = AgentConfig(name="writer", model="gpt-4", system="")
    await hooks.on_llm_stream_start(step, agent, attempt=0)  # type: ignore[arg-type]

    events = list(log.read_from())
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.LLM_STREAM_START
    assert ev.step_id == "llm_1"
    assert ev.attempt == 0
    assert ev.payload["agent_name"] == "writer"
    assert ev.payload["model"] == "gpt-4"


async def test_on_llm_stream_delta_publishes_event(tmp_path):
    """on_llm_stream_delta → llm_delta 事件，payload 带 accumulated_len。"""
    log = EventLog("run_10", base_dir=str(tmp_path))
    bus = EventBus("run_10", log=log, delta_merge_window=0.0)  # 立即 flush
    hooks = EventBusHooks(bus, run_id="run_10")

    step = _MockStep(id="llm_2", type="llm")
    agent = AgentConfig(name="writer", model="gpt-4", system="")
    await hooks.on_llm_stream_delta(
        step, agent, "hello", "hello world", attempt=0,  # type: ignore[arg-type]
    )

    # delta_merge_window=0 时立即 flush
    await asyncio.sleep(0.05)

    events = list(log.read_from())
    deltas = [e for e in events if e.type == EventType.LLM_DELTA]
    assert len(deltas) >= 1
    ev = deltas[0]
    assert ev.step_id == "llm_2"
    assert ev.payload["delta"] == "hello"
    assert ev.payload["accumulated_len"] == len("hello world")
    assert ev.payload["delta_reasoning"] is None


async def test_on_llm_stream_delta_with_reasoning(tmp_path):
    """on_llm_stream_delta 携带 delta_reasoning 字段。"""
    log = EventLog("run_11", base_dir=str(tmp_path))
    bus = EventBus("run_11", log=log, delta_merge_window=0.0)
    hooks = EventBusHooks(bus, run_id="run_11")

    step = _MockStep(id="llm_3", type="llm")
    agent = AgentConfig(name="thinker", model="gpt-4", system="")
    await hooks.on_llm_stream_delta(
        step, agent, "text", "text acc", attempt=0,  # type: ignore[arg-type]
        delta_reasoning="thought",
    )
    await asyncio.sleep(0.05)

    events = list(log.read_from())
    deltas = [e for e in events if e.type == EventType.LLM_DELTA]
    assert len(deltas) >= 1
    assert deltas[0].payload["delta_reasoning"] == "thought"


async def test_on_llm_stream_end_publishes_event(tmp_path):
    """on_llm_stream_end → llm_stream_end 事件，payload 带 full_len。"""
    log = EventLog("run_12", base_dir=str(tmp_path))
    bus = EventBus("run_12", log=log)
    hooks = EventBusHooks(bus, run_id="run_12")

    step = _MockStep(id="llm_4", type="llm")
    agent = AgentConfig(name="writer", model="gpt-4", system="")
    full_content = "final answer"
    await hooks.on_llm_stream_end(step, agent, full_content, attempt=0)  # type: ignore[arg-type]

    events = list(log.read_from())
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.LLM_STREAM_END
    assert ev.step_id == "llm_4"
    assert ev.payload["full_len"] == len(full_content)


# ---------------------------------------------------------------------------
# 工具调用 hook
# ---------------------------------------------------------------------------
async def test_on_tool_call_publishes_tool_call_event(tmp_path):
    """on_tool_call → tool_call 事件，payload 含 name/summary/status。"""
    log = EventLog("run_13", base_dir=str(tmp_path))
    bus = EventBus("run_13", log=log)
    hooks = EventBusHooks(bus, run_id="run_13")

    tool = _MockTool(name="db.query")
    params = {"sql": "SELECT 1"}
    result = {"rows": [1]}
    await hooks.on_tool_call(tool, params, result)  # type: ignore[arg-type]

    events = list(log.read_from())
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.TOOL_CALL
    assert ev.payload["name"] == "db.query"
    assert ev.payload["status"] == "ok"
    assert "SELECT 1" in ev.payload["params_summary"]
    assert "rows" in ev.payload["result_summary"]


async def test_on_tool_call_error_status(tmp_path):
    """result 含 error 字段时 status='error'。"""
    log = EventLog("run_14", base_dir=str(tmp_path))
    bus = EventBus("run_14", log=log)
    hooks = EventBusHooks(bus, run_id="run_14")

    tool = _MockTool(name="db.query")
    result = {"error": "connection refused"}
    await hooks.on_tool_call(tool, {}, result)  # type: ignore[arg-type]

    events = list(log.read_from())
    assert events[0].payload["status"] == "error"


async def test_on_mcp_call_publishes_tool_call_event(tmp_path):
    """on_mcp_call → tool_call 事件，payload 加 mcp_server。"""
    log = EventLog("run_15", base_dir=str(tmp_path))
    bus = EventBus("run_15", log=log)
    hooks = EventBusHooks(bus, run_id="run_15")

    params = {"q": "search"}
    result = {"hits": [1, 2]}
    await hooks.on_mcp_call("brave_search", "search_api", params, result)

    events = list(log.read_from())
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.TOOL_CALL
    assert ev.payload["name"] == "search_api"
    assert ev.payload["mcp_server"] == "brave_search"
    assert ev.payload["status"] == "ok"


# ---------------------------------------------------------------------------
# run_id 默认值
# ---------------------------------------------------------------------------
async def test_run_id_defaults_to_bus_run_id(tmp_path):
    """run_id 未传时默认用 bus.run_id。"""
    log = EventLog("run_default", base_dir=str(tmp_path))
    bus = EventBus("run_default", log=log)
    # 不传 run_id
    hooks = EventBusHooks(bus)
    assert hooks._run_id == "run_default"

    from agentkit.core.context import Context
    await hooks.before_workflow(_MockWorkflow(), Context())

    events = list(log.read_from())
    assert events[0].run_id == "run_default"


# ---------------------------------------------------------------------------
# 集成：与真实 Workflow + Step 联动
# ---------------------------------------------------------------------------
async def test_event_bus_hooks_with_real_workflow(tmp_path):
    """与真实 Workflow + ToolStep 联动：事件序列完整。"""
    from agentkit.core.context import Context
    from agentkit.core.workflow import Workflow
    from agentkit.steps.tool_step import ToolStep
    from agentkit.tests.conftest import EchoTool
    from agentkit.tools.base import register

    log = EventLog("run_integration", base_dir=str(tmp_path))
    bus = EventBus("run_integration", log=log)
    hooks = EventBusHooks(bus, run_id="run_integration")

    # 注册 echo 工具
    register(EchoTool())

    wf = Workflow(
        name="integration_test",
        steps=[
            ToolStep(id="step_a", tool="test.echo", params={"x": 1}, output="r1"),
        ],
        hooks=hooks,
        auto_hooks=False,
    )
    result = await wf.run(inputs={"init": 1})

    events = list(log.read_from())
    types = [e.type for e in events]
    # 完整序列：run_started → step_started → step_finished → run_completed
    # （tool_call hook 由 ToolStep 在 run 内触发）
    assert EventType.RUN_STARTED in types
    assert EventType.STEP_STARTED in types
    assert EventType.STEP_FINISHED in types
    assert EventType.TOOL_CALL in types
    assert EventType.RUN_COMPLETED in types

    # 事件 seq 单调递增
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert all(s > 0 for s in seqs)


async def test_event_bus_hooks_with_failing_workflow(tmp_path):
    """失败 Step 触发 run_failed 事件。"""
    from agentkit.core.workflow import Workflow
    from agentkit.steps.tool_step import ToolStep
    from agentkit.tests.conftest import FailTool
    from agentkit.tools.base import register

    log = EventLog("run_fail", base_dir=str(tmp_path))
    bus = EventBus("run_fail", log=log)
    hooks = EventBusHooks(bus, run_id="run_fail")

    register(FailTool())

    wf = Workflow(
        name="fail_test",
        steps=[
            ToolStep(id="bad_step", tool="test.fail", params={"x": 1}, output="r"),
        ],
        hooks=hooks,
        auto_hooks=False,
    )
    result = await wf.run()

    events = list(log.read_from())
    types = [e.type for e in events]
    assert EventType.RUN_STARTED in types
    assert EventType.STEP_STARTED in types
    assert EventType.STEP_FINISHED in types  # 失败也触发 after_step
    assert EventType.RUN_FAILED in types

    # run_failed 事件带 error
    failed_ev = next(e for e in events if e.type == EventType.RUN_FAILED)
    assert "故意失败" in failed_ev.payload["error"] or "RuntimeError" in failed_ev.payload["error"]
