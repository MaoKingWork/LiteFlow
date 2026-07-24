"""LoopStep:iter(collect/append/last) + until(success/max) + cancel 传播。"""
from __future__ import annotations

import pytest

from agentkit.core.cancel import CancelToken
from agentkit.core.context import Context
from agentkit.steps.loop_step import LoopMaxReachedError, LoopStep
from agentkit.steps.tool_step import ToolStep
from agentkit.tests.conftest import CallbackStep, SetterStep


# ---------------------------------------------------------------------------
# 辅助:构造带 echo body 的 LoopStep
# ---------------------------------------------------------------------------
def _echo_body(body_id: str = "body") -> ToolStep:
    """body:调用 test.echo 把当前 item 写到 body_out。"""
    return ToolStep(
        id=body_id,
        tool="test.echo",
        params={"val": "{{item}}"},
        output="body_out",
    )


# ---------------------------------------------------------------------------
# iter 模式:collect
# ---------------------------------------------------------------------------
async def test_loop_iter_collect(echo_tool):
    """collect:每次 body 产出收集为列表。"""
    ctx = Context()
    ctx.set("items", ["a", "b", "c"])
    loop = LoopStep(
        id="loop",
        iter="{{items}}",
        step=_echo_body(),
        output="collected",
        output_mode="collect",
    )
    await loop.execute(ctx)

    collected = ctx.get("collected")
    assert len(collected) == 3
    # 每个元素是 echo 返回的 dict
    assert collected[0]["echo"]["val"] == "a"
    assert collected[2]["echo"]["val"] == "c"


# ---------------------------------------------------------------------------
# iter 模式:append(增量累加)
# ---------------------------------------------------------------------------
async def test_loop_iter_append(echo_tool):
    """append:每次 body 产出作为增量,累加为字符串。"""
    ctx = Context()
    ctx.set("items", ["a", "b", "c"])
    loop = LoopStep(
        id="loop",
        iter="{{items}}",
        step=_echo_body(),
        output="accumulated",
        output_mode="append",
        separator="\n",
    )
    await loop.execute(ctx)

    result = ctx.get("accumulated")
    assert isinstance(result, str)
    assert "a" in result
    assert "b" in result
    assert "c" in result
    assert "\n" in result


# ---------------------------------------------------------------------------
# iter 模式:last(仅保留最后一次)
# ---------------------------------------------------------------------------
async def test_loop_iter_last(echo_tool):
    """last:仅保留 body 最后一次产出。"""
    ctx = Context()
    ctx.set("items", ["a", "b", "c"])
    loop = LoopStep(
        id="loop",
        iter="{{items}}",
        step=_echo_body(),
        output="body_out",
        output_mode="last",
    )
    await loop.execute(ctx)

    # body_out 自然保留最后一次写入
    result = ctx.get("body_out")
    assert result["echo"]["val"] == "c"


# ---------------------------------------------------------------------------
# until 模式:条件满足后停止
# ---------------------------------------------------------------------------
async def test_loop_until_success():
    """body 设置 done=yes → until 条件满足 → 停止。

    用 last 模式让 body 的 output 自然保留,不被 collect 聚合覆盖。
    """
    ctx = Context()
    body = SetterStep(id="body", key="done", value="yes")
    loop = LoopStep(
        id="loop",
        until="{{done}} == 'yes'",
        step=body,
        max=5,
        on_max="fail",
        output_mode="last",
    )
    await loop.execute(ctx)
    assert ctx.get("done") == "yes"


# ---------------------------------------------------------------------------
# until 模式:达到 max 上限(fail)
# ---------------------------------------------------------------------------
async def test_loop_until_max_fail():
    """body 始终不满足 until → 达到 max → on_max=fail 抛异常。"""
    ctx = Context()
    body = SetterStep(id="body", key="done", value="no")
    loop = LoopStep(
        id="loop",
        until="{{done}} == 'yes'",
        step=body,
        max=2,
        on_max="fail",
    )
    with pytest.raises(LoopMaxReachedError):
        await loop.execute(ctx)


# ---------------------------------------------------------------------------
# until 模式:达到 max 上限(continue)
# ---------------------------------------------------------------------------
async def test_loop_until_max_continue():
    """body 始终不满足 until → 达到 max → on_max=continue 静默继续。

    用 last 模式让 body 的 output 自然保留,不被 collect 聚合覆盖。
    """
    ctx = Context()
    body = SetterStep(id="body", key="done", value="no")
    loop = LoopStep(
        id="loop",
        until="{{done}} == 'yes'",
        step=body,
        max=2,
        on_max="continue",
        output_mode="last",
    )
    # 不抛异常
    await loop.execute(ctx)
    assert ctx.get("done") == "no"


# ---------------------------------------------------------------------------
# cancel 传播:iter 模式在迭代边界停止
# ---------------------------------------------------------------------------
async def test_loop_cancel_between_iterations(echo_tool):
    """graceful cancel:第一次迭代后触发 token → 第二次迭代边界停止。"""
    token = CancelToken()
    ctx = Context()
    ctx.set("items", ["a", "b", "c"])
    # body:触发 token 并写入 output
    body = CallbackStep(id="body", callback=token.trigger, output="body_out")
    loop = LoopStep(
        id="loop",
        iter="{{items}}",
        step=body,
        output="collected",
        output_mode="collect",
    )
    await loop.execute(ctx, cancel_token=token)

    # 只完成 1 次迭代
    collected = ctx.get("collected")
    assert len(collected) == 1
