"""P0 核心:协作式取消原语 + 两阶段取消(graceful / immediate)+ resume。

出口标准:cancel(graceful/immediate) 后状态正确落盘并可 resume。
"""
from __future__ import annotations

import asyncio

import pytest

from agentkit.core.cancel import CancelToken
from agentkit.core.checkpoint import RunStatus
from agentkit.core.workflow import Workflow
from agentkit.tests.conftest import BlockingStep, CallbackStep, SetterStep


# ---------------------------------------------------------------------------
# CancelToken 原语
# ---------------------------------------------------------------------------
async def test_token_initial_not_cancelled():
    token = CancelToken()
    assert not token.is_cancelled


async def test_token_trigger_sets_flag():
    token = CancelToken()
    token.trigger()
    assert token.is_cancelled


async def test_token_trigger_idempotent():
    token = CancelToken()
    token.trigger()
    token.trigger()
    assert token.is_cancelled


async def test_token_check_raises_when_cancelled():
    token = CancelToken()
    token.trigger()
    with pytest.raises(asyncio.CancelledError):
        token.check()


async def test_token_check_safe_when_not_cancelled():
    token = CancelToken()
    token.check()  # 不抛异常


# ---------------------------------------------------------------------------
# Graceful 取消:step 边界检查令牌
# ---------------------------------------------------------------------------
async def test_graceful_cancel_before_any_step(checkpoint_store):
    """token 已触发 → 第一个 step 边界即停止,completed_steps 为空。"""
    token = CancelToken()
    token.trigger()
    wf = Workflow(
        name="test",
        steps=[SetterStep(id="s1", key="v1", value="a")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    result = await wf.run(cancel_token=token)

    assert result.status == RunStatus.CANCELLED
    assert result.completed_steps == []
    # 检查点正确落盘
    cp = await checkpoint_store.load(result.run_id)
    assert cp is not None
    assert cp.status == RunStatus.CANCELLED


async def test_graceful_cancel_after_first_step(checkpoint_store):
    """step1 完成后触发 token → step2 边界停止,completed_steps=[s1]。"""
    token = CancelToken()
    wf = Workflow(
        name="test",
        steps=[
            CallbackStep(id="s1", callback=token.trigger, output="v1"),
            SetterStep(id="s2", key="v2", value="b"),
        ],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    result = await wf.run(cancel_token=token)

    assert result.status == RunStatus.CANCELLED
    assert result.completed_steps == ["s1"]
    assert result.context.has("v1")
    assert not result.context.has("v2")
    # 检查点落盘
    cp = await checkpoint_store.load(result.run_id)
    assert cp.status == RunStatus.CANCELLED
    assert cp.completed_steps == ["s1"]


# ---------------------------------------------------------------------------
# Immediate 取消:Task.cancel → CancelledError → 落盘 → 重抛
# ---------------------------------------------------------------------------
async def test_immediate_cancel_persists_status(checkpoint_store):
    """Task.cancel 注入 CancelledError → 落盘 cancelled → 重抛。"""
    wf = Workflow(
        name="test",
        steps=[BlockingStep(id="s1", delay=30.0, output="v1")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    task = asyncio.create_task(wf.run(run_id="imm_cancel"))
    await asyncio.sleep(0.1)  # 等待 workflow 进入 BlockingStep
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cp = await checkpoint_store.load("imm_cancel")
    assert cp is not None
    assert cp.status == RunStatus.CANCELLED


# ---------------------------------------------------------------------------
# Resume after cancel
# ---------------------------------------------------------------------------
async def test_resume_after_graceful_cancel(checkpoint_store):
    """graceful cancel 后 resume → 从断点继续执行未完成 Step。"""
    token = CancelToken()
    wf = Workflow(
        name="test",
        steps=[
            CallbackStep(id="s1", callback=token.trigger, output="v1"),
            SetterStep(id="s2", key="v2", value="done"),
        ],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    result = await wf.run(run_id="resume_cancel", cancel_token=token)
    assert result.status == RunStatus.CANCELLED
    assert result.completed_steps == ["s1"]

    # resume(不带 cancel_token)→ 从 s2 继续
    result2 = await wf.resume("resume_cancel")
    assert result2.status == RunStatus.COMPLETED
    assert "s2" in result2.completed_steps
    assert result2.context.get("v2") == "done"


# ---------------------------------------------------------------------------
# 零侵入:cancel_token=None 时行为完全同现状
# ---------------------------------------------------------------------------
async def test_no_cancel_token_behaves_unchanged(checkpoint_store):
    """不传 cancel_token 时工作流正常完成,行为同现状。"""
    wf = Workflow(
        name="test",
        steps=[
            SetterStep(id="s1", key="v1", value="a"),
            SetterStep(id="s2", key="v2", value="b"),
        ],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    result = await wf.run()
    assert result.status == RunStatus.COMPLETED
    assert result.completed_steps == ["s1", "s2"]
