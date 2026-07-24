"""ParallelStep:并行执行 + fail_fast / collect_all + output 汇总。"""
from __future__ import annotations

import pytest

from agentkit.core.context import Context
from agentkit.steps.parallel_step import ParallelError, ParallelStep
from agentkit.steps.tool_step import ToolStep


# ---------------------------------------------------------------------------
# 基础并行:两分支各自写入 output
# ---------------------------------------------------------------------------
async def test_parallel_basic(echo_tool):
    b1 = ToolStep(id="b1", tool="test.echo", params={"x": 1}, output="r1")
    b2 = ToolStep(id="b2", tool="test.echo", params={"y": 2}, output="r2")
    step = ParallelStep(id="par", branches=[b1, b2])

    ctx = Context()
    await step.execute(ctx)

    assert ctx.get("r1")["echo"] == {"x": 1}
    assert ctx.get("r2")["echo"] == {"y": 2}


# ---------------------------------------------------------------------------
# output 汇总:各分支 output 收为 dict
# ---------------------------------------------------------------------------
async def test_parallel_output_merge(echo_tool):
    b1 = ToolStep(id="b1", tool="test.echo", params={"x": 1}, output="r1")
    b2 = ToolStep(id="b2", tool="test.echo", params={"y": 2}, output="r2")
    step = ParallelStep(id="par", branches=[b1, b2], output="merged")

    ctx = Context()
    await step.execute(ctx)

    merged = ctx.get("merged")
    assert "r1" in merged
    assert "r2" in merged
    assert merged["r1"]["echo"] == {"x": 1}


# ---------------------------------------------------------------------------
# fail_fast:首个错误即取消所有分支
# ---------------------------------------------------------------------------
async def test_parallel_fail_fast(echo_tool, fail_tool):
    b1 = ToolStep(id="b1", tool="test.fail", params={}, output="r1")
    b2 = ToolStep(id="b2", tool="test.echo", params={"y": 2}, output="r2")
    step = ParallelStep(id="par", branches=[b1, b2], on_error="fail_fast")

    ctx = Context()
    with pytest.raises(RuntimeError, match="故意失败"):
        await step.execute(ctx)


# ---------------------------------------------------------------------------
# collect_all:等待所有分支,收集错误后统一 raise
# ---------------------------------------------------------------------------
async def test_parallel_collect_all(echo_tool, fail_tool):
    b1 = ToolStep(id="b1", tool="test.echo", params={"x": 1}, output="r1")
    b2 = ToolStep(id="b2", tool="test.fail", params={}, output="r2")
    step = ParallelStep(id="par", branches=[b1, b2], on_error="collect_all")

    ctx = Context()
    with pytest.raises(ParallelError) as exc_info:
        await step.execute(ctx)

    assert len(exc_info.value.errors) == 1
    # b1 成功(不被 b2 失败影响)
    assert ctx.get("r1")["echo"] == {"x": 1}


# ---------------------------------------------------------------------------
# output key 重复校验
# ---------------------------------------------------------------------------
async def test_parallel_duplicate_output_rejected(echo_tool):
    """branches 的 output key 重复时构造抛 ValueError。"""
    b1 = ToolStep(id="b1", tool="test.echo", params={"x": 1}, output="dup")
    b2 = ToolStep(id="b2", tool="test.echo", params={"y": 2}, output="dup")
    with pytest.raises(ValueError, match="重复"):
        ParallelStep(id="par", branches=[b1, b2])
