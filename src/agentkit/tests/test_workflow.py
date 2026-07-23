"""Workflow 引擎:run / resume / failure / hook 序列 / checkpoint 往返。

出口标准:事件与 hook 序列一一对应;after_workflow 成功/失败/取消三种路径都只触发一次。
"""
from __future__ import annotations

import pytest

from agentkit.core.cancel import CancelToken
from agentkit.core.checkpoint import RunStatus
from agentkit.core.workflow import Workflow
from agentkit.tests.conftest import (
    CallbackStep,
    FailStep,
    RecordingHooks,
    SetterStep,
)


# ---------------------------------------------------------------------------
# Run 成功
# ---------------------------------------------------------------------------
async def test_run_success(checkpoint_store):
    wf = Workflow(
        name="test",
        steps=[
            SetterStep(id="s1", key="v1", value="a"),
            SetterStep(id="s2", key="v2", value="b"),
        ],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    result = await wf.run(inputs={"init": 1})

    assert result.status == RunStatus.COMPLETED
    assert result.completed_steps == ["s1", "s2"]
    assert result.context.get("v1") == "a"
    assert result.context.get("v2") == "b"
    assert result.context.get("init") == 1
    # 检查点落盘
    cp = await checkpoint_store.load(result.run_id)
    assert cp.status == RunStatus.COMPLETED
    assert cp.completed_steps == ["s1", "s2"]


# ---------------------------------------------------------------------------
# Run 失败
# ---------------------------------------------------------------------------
async def test_run_failure(checkpoint_store):
    wf = Workflow(
        name="test",
        steps=[
            SetterStep(id="s1", key="v1", value="a"),
            FailStep(id="s2", output="v2"),
        ],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    result = await wf.run()

    assert result.status == RunStatus.FAILED
    assert result.completed_steps == ["s1"]
    assert result.error is not None
    assert "故意失败" in result.error
    # 检查点落盘
    cp = await checkpoint_store.load(result.run_id)
    assert cp.status == RunStatus.FAILED
    assert cp.completed_steps == ["s1"]
    assert cp.error is not None


# ---------------------------------------------------------------------------
# Resume:跳过已完成 Step,从失败处重新执行
# ---------------------------------------------------------------------------
async def test_resume_skips_completed(checkpoint_store):
    wf = Workflow(
        name="test",
        steps=[
            SetterStep(id="s1", key="v1", value="a"),
            FailStep(id="s2", output="v2"),
        ],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    result = await wf.run(run_id="resume_test")
    assert result.status == RunStatus.FAILED

    # 替换 s2 为正常 Step 后 resume
    wf.steps[1] = SetterStep(id="s2", key="v2", value="recovered")
    result2 = await wf.resume("resume_test")

    assert result2.status == RunStatus.COMPLETED
    assert result2.completed_steps == ["s1", "s2"]
    assert result2.context.get("v1") == "a"
    assert result2.context.get("v2") == "recovered"


# ---------------------------------------------------------------------------
# Hook 序列:成功路径
# ---------------------------------------------------------------------------
async def test_hook_sequence_success(recording_hooks, checkpoint_store):
    """成功:before_workflow → (before_step → after_step)* → after_workflow。"""
    wf = Workflow(
        name="test",
        steps=[
            SetterStep(id="s1", key="v1", value="a"),
            SetterStep(id="s2", key="v2", value="b"),
        ],
        checkpoint_store=checkpoint_store,
        hooks=recording_hooks,
        auto_hooks=False,
    )
    await wf.run()

    assert recording_hooks.events == [
        "before_workflow",
        "before_step:s1", "after_step:s1",
        "before_step:s2", "after_step:s2",
        "after_workflow",
    ]


# ---------------------------------------------------------------------------
# Hook 序列:失败路径(after_workflow 恰好一次 — 修复重复调用 bug)
# ---------------------------------------------------------------------------
async def test_hook_after_workflow_once_on_failure(recording_hooks, checkpoint_store):
    """失败路径:after_workflow 只调用一次(修复此前在失败分支+finally 重复调用 bug)。"""
    wf = Workflow(
        name="test",
        steps=[
            SetterStep(id="s1", key="v1", value="a"),
            FailStep(id="s2", output="v2"),
        ],
        checkpoint_store=checkpoint_store,
        hooks=recording_hooks,
        auto_hooks=False,
    )
    await wf.run()

    assert recording_hooks.events.count("after_workflow") == 1
    # 失败 Step 仍触发 before_step / on_step_error / after_step
    assert "before_step:s2" in recording_hooks.events
    assert "on_step_error:s2" in recording_hooks.events
    assert "after_step:s2" in recording_hooks.events


# ---------------------------------------------------------------------------
# Hook 序列:取消路径(after_workflow 恰好一次)
# ---------------------------------------------------------------------------
async def test_hook_after_workflow_once_on_cancel(recording_hooks, checkpoint_store):
    """取消路径:after_workflow 只调用一次。"""
    token = CancelToken()
    wf = Workflow(
        name="test",
        steps=[
            CallbackStep(id="s1", callback=token.trigger, output="v1"),
            SetterStep(id="s2", key="v2", value="b"),
        ],
        checkpoint_store=checkpoint_store,
        hooks=recording_hooks,
        auto_hooks=False,
    )
    await wf.run(cancel_token=token)

    assert recording_hooks.events.count("after_workflow") == 1


# ---------------------------------------------------------------------------
# Checkpoint 往返:save → load → 字段完整
# ---------------------------------------------------------------------------
async def test_checkpoint_roundtrip(checkpoint_store):
    wf = Workflow(
        name="roundtrip",
        steps=[SetterStep(id="s1", key="v1", value="data")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    result = await wf.run(run_id="rt_test")

    cp = await checkpoint_store.load("rt_test")
    assert cp is not None
    assert cp.run_id == "rt_test"
    assert cp.workflow_name == "roundtrip"
    assert cp.status == RunStatus.COMPLETED
    assert cp.completed_steps == ["s1"]
    assert cp.error is None
    # list_runs
    runs = await checkpoint_store.list_runs()
    assert "rt_test" in runs
