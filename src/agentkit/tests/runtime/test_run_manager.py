"""RunManager：状态机编排 + 取消编排 + 内存注册表。

出口标准（对齐 P1 §A2）：
    - start() 后 EventLog 有 run_created（seq=1），内存注册表有 handle
    - cancel(graceful/immediate) 后有 run_cancelling + run_cancelled，无 run_completed 误发
    - cancel 幂等：重复不重复发 run_cancelled
    - cancel 未知 run 抛 KeyError
    - resume 从 interrupted/failed 恢复，跳过 completed_steps（sink 不重复执行）
    - resume 状态非 interrupted/failed 抛 ValueError
    - list_runs 合并 checkpoint + 内存状态，is_active 标记正确
    - shutdown 后所有 task done，EventBus closed
    - 事件 seq 单调递增
"""
from __future__ import annotations

import asyncio

import pytest

from agentkit.core.checkpoint import Checkpoint, RunStatus
from agentkit.core.hooks import MockToolHooks
from agentkit.core.workflow import Workflow
from agentkit.runtime.event import EventType
from agentkit.runtime.run_manager import RunHandle, RunManager, RunSummary
from agentkit.tests.conftest import (
    BlockingStep,
    CallbackStep,
    FailStep,
    SetterStep,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def run_manager(checkpoint_store, tmp_path) -> RunManager:
    """基于 tmp_path 的 RunManager，测试结束自动清理。"""
    return RunManager(checkpoint_store, base_dir=str(tmp_path / "runs"))


async def _wait_task_done(mgr: RunManager, run_id: str, timeout: float = 5.0):
    """等待 run 的 task 完成（正常结束或被 cancel）。"""
    handle = mgr.get(run_id)
    if handle is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(handle.task), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


# ---------------------------------------------------------------------------
# start —— run_created + handle 注册
# ---------------------------------------------------------------------------
async def test_start_emits_run_created(run_manager, checkpoint_store):
    """start() 后 EventLog 有 run_created 事件，seq=1。"""
    wf = Workflow(
        name="test_wf",
        steps=[SetterStep(id="s1", key="v1", value="a")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_test_1")
    assert run_id == "run_test_1"

    # 等待 task 完成
    await _wait_task_done(run_manager, run_id)

    handle = run_manager.get("run_test_1")
    # handle 可能已被 done 回调移除（task 已完成）
    if handle is not None:
        events = list(handle.event_log.read_from())
    else:
        from agentkit.runtime.event import EventLog
        log = EventLog("run_test_1", base_dir=run_manager._base_dir)
        events = list(log.read_from())

    types = [e.type for e in events]
    assert EventType.RUN_CREATED in types
    created = next(e for e in events if e.type == EventType.RUN_CREATED)
    assert created.seq == 1
    assert created.payload["workflow_name"] == "test_wf"


async def test_start_registers_handle(run_manager, checkpoint_store):
    """get(run_id) 返回非 None，含 task/cancel_token/event_bus。"""
    wf = Workflow(
        name="test_wf",
        steps=[BlockingStep(id="s1", delay=5.0, output="v1")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_active")

    handle = run_manager.get(run_id)
    assert handle is not None
    assert handle.run_id == run_id
    assert handle.workflow_name == "test_wf"
    assert handle.task is not None
    assert handle.cancel_token is not None
    assert handle.event_bus is not None
    assert handle.hooks is not None
    assert not handle.cancelling

    # 清理
    await run_manager.shutdown()


async def test_start_auto_generates_run_id(run_manager, checkpoint_store):
    """run_id=None 时自动生成 run_<uuid> 格式。"""
    wf = Workflow(
        name="test_wf",
        steps=[SetterStep(id="s1", key="v1", value="a")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf)
    assert run_id.startswith("run_")
    assert len(run_id) > 4
    await _wait_task_done(run_manager, run_id)


async def test_start_with_inputs(run_manager, checkpoint_store):
    """inputs 正确传入 Context。"""
    wf = Workflow(
        name="test_wf",
        steps=[SetterStep(id="s1", key="v2", value="b")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, inputs={"v1": "a"}, run_id="run_inputs")
    await _wait_task_done(run_manager, run_id)

    cp = await checkpoint_store.load(run_id)
    assert cp is not None
    assert cp.status == RunStatus.COMPLETED
    assert cp.completed_steps == ["s1"]


# ---------------------------------------------------------------------------
# cancel —— graceful / immediate / 幂等 / 未知 run
# ---------------------------------------------------------------------------
async def test_graceful_cancel(run_manager, checkpoint_store):
    """cancel(graceful) 后有 run_cancelling + run_cancelled；无 run_completed。

    graceful 语义:当前 step(s2)完成后停止,s3 不执行。
    """
    token_triggerred = asyncio.Event()
    wf = Workflow(
        name="test_wf",
        steps=[
            CallbackStep(
                id="s1",
                callback=token_triggerred.set,
                output="v1",
            ),
            BlockingStep(id="s2", delay=1.0, output="v2"),
            SetterStep(id="s3", key="v3", value="c"),
        ],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_graceful")

    # 等待 s1 完成（s1 回调设置 event）
    await asyncio.wait_for(token_triggerred.wait(), timeout=5.0)
    # 等一小步让 s2 开始
    await asyncio.sleep(0.1)

    await run_manager.cancel(run_id, mode="graceful")

    from agentkit.runtime.event import EventLog
    log = EventLog(run_id, base_dir=run_manager._base_dir)
    events = list(log.read_from())
    types = [e.type for e in events]

    assert EventType.RUN_CANCELLING in types
    assert EventType.RUN_CANCELLED in types
    assert EventType.RUN_COMPLETED not in types
    assert EventType.RUN_FAILED not in types

    # checkpoint 状态
    cp = await checkpoint_store.load(run_id)
    assert cp is not None
    assert cp.status == RunStatus.CANCELLED
    assert "s1" in cp.completed_steps
    # graceful: s2 完成后才停止,s3 不执行
    assert "s3" not in cp.completed_steps


async def test_immediate_cancel(run_manager, checkpoint_store):
    """cancel(immediate) 后 task 收到 CancelledError，状态正确。"""
    wf = Workflow(
        name="test_wf",
        steps=[BlockingStep(id="s1", delay=30.0, output="v1")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_immediate")

    # 等 BlockingStep 进入
    await asyncio.sleep(0.2)

    await run_manager.cancel(run_id, mode="immediate")

    from agentkit.runtime.event import EventLog
    log = EventLog(run_id, base_dir=run_manager._base_dir)
    events = list(log.read_from())
    types = [e.type for e in events]

    assert EventType.RUN_CANCELLING in types
    assert EventType.RUN_CANCELLED in types
    assert EventType.RUN_COMPLETED not in types

    cp = await checkpoint_store.load(run_id)
    assert cp is not None
    assert cp.status == RunStatus.CANCELLED


async def test_cancel_idempotent(run_manager, checkpoint_store):
    """重复 cancel() 不重复发 run_cancelled。"""
    wf = Workflow(
        name="test_wf",
        steps=[BlockingStep(id="s1", delay=30.0, output="v1")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_idem")
    await asyncio.sleep(0.2)

    await run_manager.cancel(run_id, mode="immediate")

    # 第二次 cancel 应该抛 KeyError（handle 已移除）
    with pytest.raises(KeyError):
        await run_manager.cancel(run_id, mode="immediate")

    from agentkit.runtime.event import EventLog
    log = EventLog(run_id, base_dir=run_manager._base_dir)
    events = list(log.read_from())
    cancelled = [e for e in events if e.type == EventType.RUN_CANCELLED]
    assert len(cancelled) == 1


async def test_cancel_unknown_run(run_manager):
    """cancel 不存在的 run_id 抛 KeyError。"""
    with pytest.raises(KeyError):
        await run_manager.cancel("nonexistent_run", mode="graceful")


async def test_cancel_invalid_mode(run_manager, checkpoint_store):
    """非法 mode 抛 ValueError。"""
    wf = Workflow(
        name="test_wf",
        steps=[BlockingStep(id="s1", delay=30.0)],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_bad_mode")
    await asyncio.sleep(0.1)

    with pytest.raises(ValueError):
        await run_manager.cancel(run_id, mode="invalid")

    await run_manager.shutdown()


async def test_cancel_already_completed(run_manager, checkpoint_store):
    """cancel 已完成的 run 抛 KeyError。"""
    wf = Workflow(
        name="test_wf",
        steps=[SetterStep(id="s1", key="v1", value="a")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_done")
    await _wait_task_done(run_manager, run_id)

    with pytest.raises(KeyError):
        await run_manager.cancel(run_id, mode="graceful")


# ---------------------------------------------------------------------------
# resume —— 从 interrupted/failed 恢复
# ---------------------------------------------------------------------------
async def test_resume_from_interrupted(run_manager, checkpoint_store):
    """checkpoint 置 interrupted → resume() → 跳过 completed_steps。"""
    mock_hooks = MockToolHooks()
    wf = Workflow(
        name="test_wf",
        steps=[
            SetterStep(id="s1", key="v1", value="a"),
            SetterStep(id="s2", key="v2", value="b"),
        ],
        checkpoint_store=checkpoint_store,
        hooks=mock_hooks,
        auto_hooks=False,
    )
    # 先 start 让 s1 完成
    run_id = await run_manager.start(wf, run_id="run_resume_int")
    await _wait_task_done(run_manager, run_id)

    # 手动置 interrupted
    cp = await checkpoint_store.load(run_id)
    assert cp is not None
    cp.status = RunStatus.INTERRUPTED
    cp.error = "process_restart"
    await checkpoint_store.save(cp)

    # resume → 从 s2 继续
    await run_manager.resume(run_id)
    await _wait_task_done(run_manager, run_id)

    cp2 = await checkpoint_store.load(run_id)
    assert cp2 is not None
    assert cp2.status == RunStatus.COMPLETED
    assert "s1" in cp2.completed_steps
    assert "s2" in cp2.completed_steps


async def test_resume_from_failed(run_manager, checkpoint_store):
    """checkpoint 置 failed → resume() → 从失败 step 续跑。"""
    wf = Workflow(
        name="test_wf",
        steps=[
            SetterStep(id="s1", key="v1", value="a"),
            FailStep(id="s2", output="v2"),
            SetterStep(id="s3", key="v3", value="c"),
        ],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    # 第一次 run：s1 完成，s2 失败
    run_id = await run_manager.start(wf, run_id="run_resume_fail")
    await _wait_task_done(run_manager, run_id)

    cp = await checkpoint_store.load(run_id)
    assert cp is not None
    assert cp.status == RunStatus.FAILED
    assert cp.completed_steps == ["s1"]

    # resume → s2 仍失败（FailStep 总是抛），但 s1 被跳过
    await run_manager.resume(run_id)
    await _wait_task_done(run_manager, run_id)

    cp2 = await checkpoint_store.load(run_id)
    assert cp2 is not None
    assert cp2.status == RunStatus.FAILED
    # s1 被跳过（不在新的 completed_steps 里追加）
    assert "s1" in cp2.completed_steps


async def test_resume_wrong_status(run_manager, checkpoint_store):
    """checkpoint 为 completed → resume() 抛 ValueError。"""
    wf = Workflow(
        name="test_wf",
        steps=[SetterStep(id="s1", key="v1", value="a")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_completed")
    await _wait_task_done(run_manager, run_id)

    with pytest.raises(ValueError):
        await run_manager.resume(run_id)


async def test_resume_unknown_run(run_manager):
    """resume 不存在的 run_id 抛 KeyError。"""
    with pytest.raises(KeyError):
        await run_manager.resume("nonexistent_run")


async def test_resume_workflow_not_registered(run_manager, checkpoint_store):
    """workflow 未注册时 resume 抛 KeyError。"""
    # 手动创建一个 checkpoint（不经 start，workflow 未注册）
    cp = Checkpoint.new("unregistered_wf", run_id="run_orphan")
    cp.status = RunStatus.INTERRUPTED
    cp.error = "process_restart"
    await checkpoint_store.save(cp)

    with pytest.raises(KeyError, match="未注册"):
        await run_manager.resume("run_orphan")


async def test_resume_sink_not_re_executed(run_manager, checkpoint_store):
    """resume 跳过 completed_steps，sink 类工具不重复执行。

    用 MockToolHooks 计数：s1 执行后计数 N，resume 后计数仍为 N
    （s1 被跳过，不重复执行）。
    """
    mock_hooks = MockToolHooks()
    wf = Workflow(
        name="test_wf",
        steps=[
            SetterStep(id="s1", key="v1", value="a"),
            SetterStep(id="s2", key="v2", value="b"),
        ],
        checkpoint_store=checkpoint_store,
        hooks=mock_hooks,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_sink")
    await _wait_task_done(run_manager, run_id)

    # SetterStep 不触发 on_tool_call，所以 calls 应为空
    count_before = len(mock_hooks.calls)

    # 置 interrupted
    cp = await checkpoint_store.load(run_id)
    cp.status = RunStatus.INTERRUPTED
    cp.error = "process_restart"
    await checkpoint_store.save(cp)

    # resume
    await run_manager.resume(run_id)
    await _wait_task_done(run_manager, run_id)

    # sink 计数不变
    assert len(mock_hooks.calls) == count_before


# ---------------------------------------------------------------------------
# list_runs —— 合并 checkpoint + 内存状态
# ---------------------------------------------------------------------------
async def test_list_runs(run_manager, checkpoint_store):
    """多个 run 后 list_runs() 返回全量，is_active 标记正确。"""
    wf = Workflow(
        name="test_wf",
        steps=[SetterStep(id="s1", key="v1", value="a")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    # 第一个 run：快速完成
    await run_manager.start(wf, run_id="run_list_1")
    await _wait_task_done(run_manager, run_id="run_list_1")

    # 第二个 run：阻塞（活跃）
    wf2 = Workflow(
        name="test_wf",
        steps=[BlockingStep(id="s1", delay=30.0)],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    await run_manager.start(wf2, run_id="run_list_2")
    # 等待 checkpoint 落盘（start 返回时 task 已创建但 checkpoint 可能尚未保存）
    for _ in range(100):
        if await checkpoint_store.load("run_list_2") is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("run_list_2 checkpoint 未在预期时间内落盘")

    summaries = await run_manager.list_runs()
    assert len(summaries) == 2

    by_id = {s.run_id: s for s in summaries}
    assert by_id["run_list_1"].is_active is False
    assert by_id["run_list_1"].status == RunStatus.COMPLETED
    assert by_id["run_list_2"].is_active is True
    assert by_id["run_list_2"].status == RunStatus.RUNNING

    await run_manager.shutdown()


async def test_list_runs_filter_workflow(run_manager, checkpoint_store):
    """list_runs(workflow_name=...) 按 workflow 过滤。"""
    wf_a = Workflow(
        name="wf_a",
        steps=[SetterStep(id="s1", key="v1", value="a")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    wf_b = Workflow(
        name="wf_b",
        steps=[SetterStep(id="s1", key="v1", value="b")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    await run_manager.start(wf_a, run_id="run_a1")
    await _wait_task_done(run_manager, run_id="run_a1")
    await run_manager.start(wf_b, run_id="run_b1")
    await _wait_task_done(run_manager, run_id="run_b1")

    summaries = await run_manager.list_runs(workflow_name="wf_a")
    assert len(summaries) == 1
    assert summaries[0].run_id == "run_a1"
    assert summaries[0].workflow_name == "wf_a"


# ---------------------------------------------------------------------------
# shutdown —— 清理所有 Task 与 EventBus
# ---------------------------------------------------------------------------
async def test_shutdown_cleans_tasks(run_manager, checkpoint_store):
    """shutdown() 后所有 task done，EventBus closed。"""
    wf = Workflow(
        name="test_wf",
        steps=[BlockingStep(id="s1", delay=30.0)],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_shutdown")
    await asyncio.sleep(0.1)

    handle = run_manager.get(run_id)
    assert handle is not None

    await run_manager.shutdown()

    assert handle.task.done()
    # get 返回 None（已从内存移除）
    assert run_manager.get(run_id) is None


async def test_shutdown_with_no_active_runs(run_manager):
    """无活跃 run 时 shutdown() 安全。"""
    await run_manager.shutdown()
    assert len(run_manager._handles) == 0


# ---------------------------------------------------------------------------
# 事件 seq 单调递增
# ---------------------------------------------------------------------------
async def test_events_seq_monotonic(run_manager, checkpoint_store):
    """同一 run 所有事件 seq 严格递增。"""
    wf = Workflow(
        name="test_wf",
        steps=[SetterStep(id="s1", key="v1", value="a")],
        checkpoint_store=checkpoint_store,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_seq")
    await _wait_task_done(run_manager, run_id)

    from agentkit.runtime.event import EventLog
    log = EventLog(run_id, base_dir=run_manager._base_dir)
    events = list(log.read_from())

    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert all(s > 0 for s in seqs)
    # run_created 应该是 seq=1
    created = next(e for e in events if e.type == EventType.RUN_CREATED)
    assert created.seq == 1


# ---------------------------------------------------------------------------
# hooks 注入 —— 原 hooks 保留 + EventBusHooks 合并
# ---------------------------------------------------------------------------
async def test_hooks_injection_preserves_original(run_manager, checkpoint_store):
    """start() 后原 hooks 仍被调用（经 CompositeHooks 合并）。"""
    from agentkit.tests.conftest import RecordingHooks

    recording = RecordingHooks()
    wf = Workflow(
        name="test_wf",
        steps=[SetterStep(id="s1", key="v1", value="a")],
        checkpoint_store=checkpoint_store,
        hooks=recording,
        auto_hooks=False,
    )
    run_id = await run_manager.start(wf, run_id="run_hooks")
    await _wait_task_done(run_manager, run_id)

    # 原 RecordingHooks 应被调用
    assert "before_workflow" in recording.events
    assert "after_workflow" in recording.events
    assert "before_step:s1" in recording.events


async def test_hooks_reused_across_runs(run_manager, checkpoint_store):
    """同一 workflow 多次 start：原 hooks 不被 EventBusHooks 污染。"""
    from agentkit.tests.conftest import RecordingHooks

    recording = RecordingHooks()
    wf = Workflow(
        name="test_wf",
        steps=[SetterStep(id="s1", key="v1", value="a")],
        checkpoint_store=checkpoint_store,
        hooks=recording,
        auto_hooks=False,
    )
    # 第一次 run
    await run_manager.start(wf, run_id="run_reuse_1")
    await _wait_task_done(run_manager, run_id="run_reuse_1")
    count_1 = len(recording.events)

    # 第二次 run：recording.events 应继续追加（原 hooks 仍生效）
    await run_manager.start(wf, run_id="run_reuse_2")
    await _wait_task_done(run_manager, run_id="run_reuse_2")
    count_2 = len(recording.events)

    assert count_2 > count_1  # 第二次 run 也调用了 hooks
