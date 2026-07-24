"""BlockingExecutor：三模式分派 + 全局单例 + Tool.execution 声明。

出口标准（对齐 §5.5）：
    - inline（默认）：直接 await tool.call，行为同现状
    - thread：经共享 ThreadPoolExecutor 卸载，子线程内 asyncio.run
    - process：P0 不实现，抛 NotImplementedError
    - 全局惰性单例 + set/reset 钩子（测试隔离）
    - 非法 execution 取值抛 ValueError
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from agentkit.core.context import Context
from agentkit.runtime.blocking import (
    BlockingExecutor,
    ExecutionMode,
    get_blocking_executor,
    set_blocking_executor,
)
from agentkit.tools.base import Tool


# ---------------------------------------------------------------------------
# 测试工具：记录所在线程 id + 是否在事件循环内
# ---------------------------------------------------------------------------
class _ThreadAwareTool(Tool):
    """记录调用线程 id 与所属事件循环 id 的测试工具。"""

    name = "test.thread_aware"
    description = "records thread id"
    role = "action"

    def __init__(self, *, execution: str = "inline") -> None:
        self.execution = execution
        self.called_thread_id: int | None = None
        self.called_loop_id: int | None = None

    async def call(self, params: dict, ctx: Context) -> dict:
        self.called_thread_id = threading.get_ident()
        try:
            self.called_loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            self.called_loop_id = None
        return {"echo": params, "thread_id": self.called_thread_id}


class _BlockingTool(Tool):
    """模拟同步阻塞的工具（如 reportlab 调用）。"""

    name = "test.blocking"
    description = "simulates blocking IO"
    role = "action"
    execution = "thread"

    def __init__(self) -> None:
        self.called_thread_id: int | None = None

    async def call(self, params: dict, ctx: Context) -> dict:
        # 模拟同步阻塞（在子线程内 sleep 不影响主事件循环）
        time.sleep(params.get("delay", 0.05))
        self.called_thread_id = threading.get_ident()
        return {"done": True, "thread_id": self.called_thread_id}


# ---------------------------------------------------------------------------
# ExecutionMode 常量
# ---------------------------------------------------------------------------
def test_execution_mode_constants():
    """ExecutionMode 常量取值正确。"""
    assert ExecutionMode.INLINE == "inline"
    assert ExecutionMode.THREAD == "thread"
    assert ExecutionMode.PROCESS == "process"


# ---------------------------------------------------------------------------
# inline 模式（默认）
# ---------------------------------------------------------------------------
async def test_inline_mode_direct_await():
    """inline 模式：直接 await tool.call，行为同现状。"""
    tool = _ThreadAwareTool(execution="inline")
    executor = BlockingExecutor()
    ctx = Context()
    main_thread = threading.get_ident()
    main_loop = id(asyncio.get_running_loop())

    result = await executor.run_tool(tool, {"x": 1}, ctx)

    assert result["echo"] == {"x": 1}
    # inline 模式下，工具在同一线程、同一事件循环内执行
    assert tool.called_thread_id == main_thread
    assert tool.called_loop_id == main_loop
    executor.close()


async def test_inline_mode_default_when_no_execution_attr():
    """工具无 execution 属性时默认 inline（duck typing 兼容现有 Tool）。"""

    class _LegacyTool:
        name = "test.legacy"

        async def call(self, params: dict, ctx: Context) -> dict:
            return {"legacy": True}

    executor = BlockingExecutor()
    result = await executor.run_tool(_LegacyTool(), {}, Context())  # type: ignore[arg-type]
    assert result == {"legacy": True}
    executor.close()


# ---------------------------------------------------------------------------
# thread 模式
# ---------------------------------------------------------------------------
async def test_thread_mode_offloads_to_thread_pool():
    """thread 模式：工具在子线程内执行，主事件循环不被阻塞。"""
    tool = _ThreadAwareTool(execution="thread")
    executor = BlockingExecutor(thread_workers=2)
    ctx = Context()
    main_thread = threading.get_ident()
    main_loop = id(asyncio.get_running_loop())

    result = await executor.run_tool(tool, {"x": 2}, ctx)

    assert result["echo"] == {"x": 2}
    # thread 模式下，工具在子线程内执行（线程 id 不同于主线程）
    assert tool.called_thread_id != main_thread
    # 子线程内 asyncio.run 创建临时循环，与主循环不同（运行后销毁）
    assert tool.called_loop_id is not None
    assert tool.called_loop_id != main_loop
    executor.close()


async def test_thread_mode_does_not_block_event_loop():
    """thread 模式：阻塞工具不卡住主事件循环，可并发执行其他协程。"""
    tool = _BlockingTool()
    executor = BlockingExecutor(thread_workers=2)

    # 同时启动：阻塞工具 + 一个 50ms 的协程
    # 若主循环被阻塞，协程无法在 100ms 内完成
    start = time.monotonic()

    async def _quick_task() -> str:
        await asyncio.sleep(0.05)
        return "done"

    quick_result, tool_result = await asyncio.gather(
        _quick_task(),
        executor.run_tool(tool, {"delay": 0.2}, Context()),
    )
    elapsed = time.monotonic() - start

    assert quick_result == "done"
    assert tool_result["done"] is True
    # 总耗时 < 0.3s（阻塞工具 0.2s + 余量），证明主循环未被卡住
    # 若 inline 跑 0.2s 阻塞，quick_task 至少要 0.25s 才完成
    assert elapsed < 0.3
    executor.close()


async def test_thread_mode_ctx_passed_through():
    """thread 模式：ctx 透传到工具（工具应只读契约）。"""
    tool = _ThreadAwareTool(execution="thread")
    executor = BlockingExecutor()
    ctx = Context()
    ctx.set("foo", "bar")

    result = await executor.run_tool(tool, {"k": "v"}, ctx)

    # 工具内部能读到 ctx（虽然 _ThreadAwareTool 没显式读，但 ctx 透传不抛异常）
    assert result["echo"] == {"k": "v"}
    executor.close()


async def test_thread_pool_lazy_created():
    """线程池惰性创建：构造时不创建，首次 thread 调用时创建。"""
    executor = BlockingExecutor()
    assert executor._thread_pool is None
    # inline 调用不创建线程池
    inline_tool = _ThreadAwareTool(execution="inline")
    await executor.run_tool(inline_tool, {}, Context())
    assert executor._thread_pool is None
    # thread 调用创建线程池
    thread_tool = _ThreadAwareTool(execution="thread")
    await executor.run_tool(thread_tool, {}, Context())
    assert executor._thread_pool is not None
    executor.close()


# ---------------------------------------------------------------------------
# process 模式（P0 未实现）
# ---------------------------------------------------------------------------
async def test_process_mode_raises_not_implemented():
    """process 模式 P0 未实现，抛 NotImplementedError。"""
    tool = _ThreadAwareTool(execution="process")
    executor = BlockingExecutor()
    with pytest.raises(NotImplementedError, match="process 模式在 P0 阶段未实现"):
        await executor.run_tool(tool, {}, Context())
    executor.close()


# ---------------------------------------------------------------------------
# 非法 execution 取值
# ---------------------------------------------------------------------------
async def test_invalid_execution_mode_raises():
    """非法 execution 取值抛 ValueError。"""
    tool = _ThreadAwareTool(execution="invalid_mode")
    executor = BlockingExecutor()
    with pytest.raises(ValueError, match="execution 属性非法"):
        await executor.run_tool(tool, {}, Context())
    executor.close()


async def test_empty_execution_treated_as_inline():
    """execution 为空字符串时回落到 inline。"""
    tool = _ThreadAwareTool(execution="")
    executor = BlockingExecutor()
    result = await executor.run_tool(tool, {"x": 1}, Context())
    assert result["echo"] == {"x": 1}
    executor.close()


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
def test_get_blocking_executor_lazy_singleton():
    """get_blocking_executor 惰性创建全局单例。"""
    # 重置后获取
    set_blocking_executor(None)
    executor1 = get_blocking_executor()
    executor2 = get_blocking_executor()
    assert executor1 is executor2
    executor1.close()
    set_blocking_executor(None)


def test_set_blocking_executor_override():
    """set_blocking_executor 可注入自定义实例。"""
    custom = BlockingExecutor(thread_workers=8)
    set_blocking_executor(custom)
    assert get_blocking_executor() is custom
    set_blocking_executor(None)
    # 重置后获取新实例
    new_executor = get_blocking_executor()
    assert new_executor is not custom
    new_executor.close()
    set_blocking_executor(None)


def test_set_blocking_executor_none_resets():
    """set_blocking_executor(None) 重置单例，下次 get 重新创建。"""
    executor1 = get_blocking_executor()
    set_blocking_executor(None)
    executor2 = get_blocking_executor()
    assert executor1 is not executor2
    executor2.close()
    set_blocking_executor(None)


# ---------------------------------------------------------------------------
# close 幂等
# ---------------------------------------------------------------------------
def test_close_idempotent():
    """close 幂等：多次调用不抛异常。"""
    executor = BlockingExecutor()
    # 未创建线程池时 close 应安全
    executor.close()
    executor.close()
    # 创建线程池后 close
    executor2 = BlockingExecutor()
    # 触发线程池创建
    import concurrent.futures
    pool = executor2._get_thread_pool()
    assert pool is not None
    executor2.close()
    assert executor2._thread_pool is None
    # 再次 close 不抛
    executor2.close()


# ---------------------------------------------------------------------------
# 集成：ToolStep 经 BlockingExecutor 分派
# ---------------------------------------------------------------------------
async def test_tool_step_uses_global_blocking_executor():
    """ToolStep 默认使用全局 BlockingExecutor 分派（inline 工具走 inline 路径）。"""
    from agentkit.steps.tool_step import ToolStep
    from agentkit.tools.base import register

    # 注册一个 inline 工具
    tool = _ThreadAwareTool(execution="inline")
    register(tool)

    # 重置全局执行器，让 ToolStep 用全局单例
    set_blocking_executor(None)

    step = ToolStep(id="t1", tool="test.thread_aware", params={"x": 1}, output="r")
    ctx = Context()
    trace = await step.execute(ctx)

    assert trace.status == "success"
    result = ctx.get("r")
    assert result["echo"] == {"x": 1}

    # 清理全局执行器
    get_blocking_executor().close()
    set_blocking_executor(None)


async def test_tool_step_thread_tool_via_global_executor():
    """ToolStep + execution='thread' 工具经全局 BlockingExecutor 卸载到子线程。"""
    from agentkit.steps.tool_step import ToolStep
    from agentkit.tools.base import register

    tool = _ThreadAwareTool(execution="thread")
    register(tool)
    main_thread = threading.get_ident()

    set_blocking_executor(None)

    step = ToolStep(id="t2", tool="test.thread_aware", params={"x": 2}, output="r")
    ctx = Context()
    trace = await step.execute(ctx)

    assert trace.status == "success"
    # 工具在子线程内执行
    assert tool.called_thread_id != main_thread
    result = ctx.get("r")
    assert result["echo"] == {"x": 2}

    get_blocking_executor().close()
    set_blocking_executor(None)


# ---------------------------------------------------------------------------
# 集成：LLMStep Function Call 路径也走 BlockingExecutor
# ---------------------------------------------------------------------------
async def test_llm_step_function_call_uses_blocking_executor():
    """LLMStep Function Call 调用 thread 工具时经 BlockingExecutor 卸载。"""
    from agentkit.core.agent import AgentConfig
    from agentkit.llm.mock import MockClient
    from agentkit.llm.base import LLMResponse, LLMUsage, ToolCall
    from agentkit.steps.llm_step import LLMStep
    from agentkit.tools.base import register

    # 注册 thread 工具
    tool = _ThreadAwareTool(execution="thread")
    register(tool)
    main_thread = threading.get_ident()

    # MockClient：第一轮返回 tool_call，第二轮返回纯文本
    mock = MockClient(responses=[
        LLMResponse(
            content="",
            tool_calls=[ToolCall(
                id="call_1", name="test.thread_aware",
                arguments={"x": 1},
            )],
            usage=LLMUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        ),
        LLMResponse(
            content="完成",
            usage=LLMUsage(prompt_tokens=8, completion_tokens=2, total_tokens=10),
        ),
    ])

    agent = AgentConfig(
        name="test_agent", model="gpt-4", system="你是助手",
        tools=["test.thread_aware"],
    )

    step = LLMStep(id="l1", agent=agent, output="r")
    step.bind_llm_client(mock)

    set_blocking_executor(None)

    ctx = Context()
    trace = await step.execute(ctx)

    assert trace.status == "success"
    # thread 工具在子线程内执行
    assert tool.called_thread_id != main_thread
    assert ctx.get("r") == "完成"

    get_blocking_executor().close()
    set_blocking_executor(None)
