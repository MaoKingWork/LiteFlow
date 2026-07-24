"""ConditionStep:then/else 分支选择 + cancel 传播。"""
from __future__ import annotations

from agentkit.core.cancel import CancelToken
from agentkit.core.context import Context
from agentkit.steps.condition_step import ConditionStep
from agentkit.tests.conftest import CallbackStep, SetterStep


# ---------------------------------------------------------------------------
# then 分支:条件为真
# ---------------------------------------------------------------------------
async def test_condition_then_branch():
    ctx = Context()
    ctx.set("go", "yes")
    cond = ConditionStep(
        id="cond",
        when="{{go}} == 'yes'",
        then_steps=[SetterStep(id="t1", key="t1_out", value="then")],
        else_steps=[SetterStep(id="e1", key="e1_out", value="else")],
    )
    await cond.execute(ctx)

    assert ctx.get("t1_out") == "then"
    assert not ctx.has("e1_out")


# ---------------------------------------------------------------------------
# else 分支:条件为假
# ---------------------------------------------------------------------------
async def test_condition_else_branch():
    ctx = Context()
    ctx.set("go", "no")
    cond = ConditionStep(
        id="cond",
        when="{{go}} == 'yes'",
        then_steps=[SetterStep(id="t1", key="t1_out", value="then")],
        else_steps=[SetterStep(id="e1", key="e1_out", value="else")],
    )
    await cond.execute(ctx)

    assert ctx.get("e1_out") == "else"
    assert not ctx.has("t1_out")


# ---------------------------------------------------------------------------
# 空分支:条件为真但 then_steps 为空
# ---------------------------------------------------------------------------
async def test_condition_empty_then():
    ctx = Context()
    ctx.set("go", "yes")
    cond = ConditionStep(
        id="cond",
        when="{{go}} == 'yes'",
        then_steps=[],
        else_steps=[SetterStep(id="e1", key="e1_out", value="else")],
    )
    await cond.execute(ctx)
    # then 为空,无副作用
    assert not ctx.has("e1_out")


# ---------------------------------------------------------------------------
# cancel 传播:then 分支有多个子步骤,第一个后触发 token → 第二个不执行
# ---------------------------------------------------------------------------
async def test_condition_cancel_between_substeps():
    token = CancelToken()
    ctx = Context()
    ctx.set("go", "yes")
    cond = ConditionStep(
        id="cond",
        when="{{go}} == 'yes'",
        then_steps=[
            CallbackStep(id="t1", callback=token.trigger, output="t1_out"),
            SetterStep(id="t2", key="t2_out", value="should_not_run"),
        ],
    )
    await cond.execute(ctx, cancel_token=token)

    assert ctx.has("t1_out")
    assert not ctx.has("t2_out")


# ---------------------------------------------------------------------------
# trace 记录走了哪个分支
# ---------------------------------------------------------------------------
async def test_condition_trace_branch():
    ctx = Context()
    ctx.set("go", "yes")
    cond = ConditionStep(
        id="cond",
        when="{{go}} == 'yes'",
        then_steps=[SetterStep(id="t1", key="t1_out", value="then")],
    )
    trace = await cond.execute(ctx)

    assert trace.input_summary == "branch=then"
