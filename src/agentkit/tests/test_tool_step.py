"""ToolStep:工具调用 + 参数模板 + Hook + trace。"""
from __future__ import annotations

import pytest

from agentkit.core.context import Context
from agentkit.steps.tool_step import ToolStep
from agentkit.tests.conftest import EchoTool, FailTool, RecordingHooks


async def test_tool_step_basic(echo_tool):
    """基础调用:参数解析 → 工具执行 → 结果写入 output。"""
    step = ToolStep(
        id="t1",
        tool="test.echo",
        params={"x": 1, "y": "hello"},
        output="result",
    )
    ctx = Context()
    ctx.set("name", "test")
    trace = await step.execute(ctx)

    assert trace.status == "success"
    result = ctx.get("result")
    assert result["echo"] == {"x": 1, "y": "hello"}


async def test_tool_step_param_template(echo_tool):
    """参数模板 {{var}} 从 Context 解析。"""
    step = ToolStep(
        id="t1",
        tool="test.echo",
        params={"user": "{{name}}", "count": 3},
        output="result",
    )
    ctx = Context()
    ctx.set("name", "alice")
    await step.execute(ctx)

    result = ctx.get("result")
    assert result["echo"]["user"] == "alice"
    assert result["echo"]["count"] == 3


async def test_tool_step_on_tool_call_hook(echo_tool):
    """on_tool_call Hook 被触发。"""
    hooks = RecordingHooks()
    step = ToolStep(
        id="t1",
        tool="test.echo",
        params={"x": 1},
        output="result",
    )
    ctx = Context()
    await step.execute(ctx, hooks)

    assert "on_tool_call:test.echo" in hooks.events


async def test_tool_step_trace_tool_calls(echo_tool):
    """trace.tool_calls 记录了工具调用信息。"""
    step = ToolStep(
        id="t1",
        tool="test.echo",
        params={"x": 1},
        output="result",
    )
    ctx = Context()
    trace = await step.execute(ctx)

    assert len(trace.tool_calls) == 1
    assert trace.tool_calls[0]["tool"] == "test.echo"
    assert trace.tool_calls[0]["status"] == "ok"


async def test_tool_step_failure(fail_tool):
    """工具抛异常 → execute 捕获 → trace.status=failed → 传播异常。"""
    step = ToolStep(
        id="t1",
        tool="test.fail",
        params={"x": 1},
        output="result",
    )
    ctx = Context()
    with pytest.raises(RuntimeError, match="故意失败"):
        await step.execute(ctx)
