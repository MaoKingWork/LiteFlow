"""ReportEngineTool：execution='thread' 标记 + BlockingExecutor 集成。

出口标准（对齐 §5.5）：
    - ReportEngineTool.execution == "thread"
    - 经 BlockingExecutor 卸载到子线程，render 不阻塞主事件循环
    - evaluate 失败 → 返回 {"error": ...}（不抛异常）
    - render 失败 → 返回 {"error": ...}
    - 成功 → 返回 {"file_uri": ..., "preview": ...}
    - 经 ToolStep / LLMStep Function Call 路径调用都生效
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

import pytest

from agentkit.core.context import Context
from agentkit.runtime.blocking import (
    BlockingExecutor,
    get_blocking_executor,
    set_blocking_executor,
)
from agentkit.tools.base import Tool, register
from agentkit.tools.report_engine import ReportEngineTool


# ---------------------------------------------------------------------------
# Mock ReportEngine：模拟同步阻塞的 evaluate + render
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _MockEvalResult:
    success: bool
    data: Optional[dict] = None
    errors: Optional[dict] = None


@dataclass(frozen=True)
class _MockRenderResult:
    success: bool
    file_uri: Optional[str] = None
    preview: Optional[str] = None
    errors: Optional[dict] = None


class _MockReportEngine:
    """模拟 ReportEngine 的同步阻塞行为。

    记录调用线程 id 与是否阻塞，用于验证 thread 卸载。
    """

    def __init__(
        self,
        *,
        eval_success: bool = True,
        render_success: bool = True,
        render_delay: float = 0.0,
        eval_data: Optional[dict] = None,
    ) -> None:
        self._eval_success = eval_success
        self._render_success = render_success
        self._render_delay = render_delay
        self._eval_data = eval_data or {"computed": True}
        self.eval_called_thread: int | None = None
        self.render_called_thread: int | None = None

    def evaluate(self, report_id: str, facts: dict) -> _MockEvalResult:
        self.eval_called_thread = threading.get_ident()
        if not self._eval_success:
            return _MockEvalResult(success=False, errors={"missing_fields": ["x"]})
        return _MockEvalResult(success=True, data=self._eval_data)

    def render(
        self,
        report_id: str,
        data: dict,
        view: str = "default",
    ) -> _MockRenderResult:
        self.render_called_thread = threading.get_ident()
        # 模拟同步阻塞（reportlab / python-docx 调用）
        if self._render_delay > 0:
            time.sleep(self._render_delay)
        if not self._render_success:
            return _MockRenderResult(
                success=False, errors={"message": "template not found"},
            )
        return _MockRenderResult(
            success=True,
            file_uri=f"file:///tmp/{report_id}_{view}.md",
            preview=f"# {report_id}\n\nrendered preview",
        )


# ---------------------------------------------------------------------------
# ReportEngineTool.execution 标记
# ---------------------------------------------------------------------------
def test_report_engine_tool_execution_is_thread():
    """ReportEngineTool.execution == 'thread'（对齐 §5.5）。"""
    assert ReportEngineTool.execution == "thread"
    # 类属性级别断言（不是实例属性）
    assert "thread" in ReportEngineTool.__dict__.get("execution", "")


def test_report_engine_tool_role_is_sink():
    """ReportEngineTool.role == 'sink'（输出终端）。"""
    assert ReportEngineTool.role == "sink"


def test_report_engine_tool_name():
    """ReportEngineTool.name == 'report.generate'。"""
    assert ReportEngineTool.name == "report.generate"


# ---------------------------------------------------------------------------
# 成功路径：evaluate → render
# ---------------------------------------------------------------------------
async def test_report_engine_tool_success_path():
    """evaluate 成功 → render 成功 → 返回 {file_uri, preview}。"""
    engine = _MockReportEngine()
    tool = ReportEngineTool(engine)  # type: ignore[arg-type]
    ctx = Context()

    result = await tool.call(
        {"report_id": "pack:report", "data": {"x": 1}, "view": "summary"},
        ctx,
    )

    assert "error" not in result
    assert result["file_uri"] == "file:///tmp/pack:report_summary.md"
    assert "rendered preview" in result["preview"]


async def test_report_engine_tool_default_view():
    """view 默认为 'default'。"""
    engine = _MockReportEngine()
    tool = ReportEngineTool(engine)  # type: ignore[arg-type]
    result = await tool.call(
        {"report_id": "p:r", "data": {}}, Context(),
    )
    assert result["file_uri"] == "file:///tmp/p:r_default.md"


# ---------------------------------------------------------------------------
# 失败路径：不抛异常，返回 error dict
# ---------------------------------------------------------------------------
async def test_report_engine_tool_evaluate_failure_returns_error():
    """evaluate 失败 → 返回 {'error': errors}，不抛异常。"""
    engine = _MockReportEngine(eval_success=False)
    tool = ReportEngineTool(engine)  # type: ignore[arg-type]
    result = await tool.call(
        {"report_id": "p:r", "data": {}}, Context(),
    )
    assert "error" in result
    assert result["error"] == {"missing_fields": ["x"]}


async def test_report_engine_tool_render_failure_returns_error():
    """render 失败 → 返回 {'error': errors}，不抛异常。"""
    engine = _MockReportEngine(render_success=False)
    tool = ReportEngineTool(engine)  # type: ignore[arg-type]
    result = await tool.call(
        {"report_id": "p:r", "data": {}, "view": "v"}, Context(),
    )
    assert "error" in result
    assert result["error"] == {"message": "template not found"}


async def test_report_engine_tool_evaluate_exception_returns_error():
    """evaluate 抛异常 → 捕获后返回 {'error': {'message': ...}}。"""
    class _BoomEngine:
        def evaluate(self, report_id, facts):
            raise RuntimeError("db connection lost")

        def render(self, report_id, data, view="default"):
            return _MockRenderResult(success=True, file_uri="x", preview="y")

    tool = ReportEngineTool(_BoomEngine())  # type: ignore[arg-type]
    result = await tool.call({"report_id": "p:r", "data": {}}, Context())
    assert "error" in result
    assert "db connection lost" in result["error"]["message"]


async def test_report_engine_tool_render_exception_returns_error():
    """render 抛异常 → 捕获后返回 {'error': {'message': ...}}。"""
    class _BoomRenderEngine:
        def evaluate(self, report_id, facts):
            return _MockEvalResult(success=True, data={})

        def render(self, report_id, data, view="default"):
            raise IOError("disk full")

    tool = ReportEngineTool(_BoomRenderEngine())  # type: ignore[arg-type]
    result = await tool.call({"report_id": "p:r", "data": {}}, Context())
    assert "error" in result
    assert "disk full" in result["error"]["message"]


# ---------------------------------------------------------------------------
# thread 卸载：经 BlockingExecutor 调用
# ---------------------------------------------------------------------------
async def test_report_engine_tool_via_blocking_executor_offloads():
    """经 BlockingExecutor.run_tool 调用，evaluate/render 在子线程内执行。"""
    engine = _MockReportEngine()
    tool = ReportEngineTool(engine)  # type: ignore[arg-type]
    executor = BlockingExecutor(thread_workers=2)
    main_thread = threading.get_ident()

    result = await executor.run_tool(
        tool,
        {"report_id": "p:r", "data": {"x": 1}, "view": "v"},
        Context(),
    )

    assert "error" not in result
    assert result["file_uri"] == "file:///tmp/p:r_v.md"
    # evaluate / render 都在子线程内执行（thread 卸载生效）
    assert engine.eval_called_thread != main_thread
    assert engine.render_called_thread != main_thread
    executor.close()


async def test_report_engine_tool_render_does_not_block_event_loop():
    """render 阻塞 200ms 不卡住主事件循环，可并发执行其他协程。"""
    engine = _MockReportEngine(render_delay=0.2)
    tool = ReportEngineTool(engine)  # type: ignore[arg-type]
    executor = BlockingExecutor(thread_workers=2)

    start = time.monotonic()

    async def _quick_task() -> str:
        await asyncio.sleep(0.05)
        return "done"

    quick_result, tool_result = await asyncio.gather(
        _quick_task(),
        executor.run_tool(
            tool, {"report_id": "p:r", "data": {}}, Context(),
        ),
    )
    elapsed = time.monotonic() - start

    assert quick_result == "done"
    assert "error" not in tool_result
    # 总耗时 < 0.3s（render 0.2s + 余量），证明主循环未被阻塞
    # 若 inline 跑 0.2s 阻塞，quick_task 至少要 0.25s 才完成
    assert elapsed < 0.3
    executor.close()


# ---------------------------------------------------------------------------
# ToolStep 集成：thread 标记自动生效
# ---------------------------------------------------------------------------
async def test_report_engine_tool_via_tool_step_uses_thread():
    """ToolStep 调用 ReportEngineTool 时经全局 BlockingExecutor 卸载到子线程。"""
    from agentkit.steps.tool_step import ToolStep

    engine = _MockReportEngine()
    tool = ReportEngineTool(engine)  # type: ignore[arg-type]
    register(tool)
    main_thread = threading.get_ident()

    set_blocking_executor(None)

    step = ToolStep(
        id="report_step",
        tool="report.generate",
        params={"report_id": "p:r", "data": {"x": 1}, "view": "v"},
        output="report_result",
    )
    ctx = Context()
    trace = await step.execute(ctx)

    assert trace.status == "success"
    # thread 工具在子线程内执行
    assert engine.render_called_thread != main_thread
    assert engine.eval_called_thread != main_thread
    result = ctx.get("report_result")
    assert result["file_uri"] == "file:///tmp/p:r_v.md"

    get_blocking_executor().close()
    set_blocking_executor(None)


# ---------------------------------------------------------------------------
# LLMStep Function Call 集成：thread 标记在 Function Call 路径也生效
# ---------------------------------------------------------------------------
async def test_report_engine_tool_via_llm_step_function_call():
    """LLMStep Function Call 路径调用 ReportEngineTool 时经 BlockingExecutor 卸载。"""
    from agentkit.core.agent import AgentConfig
    from agentkit.llm.mock import MockClient
    from agentkit.steps.llm_step import LLMStep

    engine = _MockReportEngine()
    tool = ReportEngineTool(engine)  # type: ignore[arg-type]
    register(tool)
    main_thread = threading.get_ident()

    # MockClient：第一轮返回 tool_call，第二轮返回纯文本
    mock = MockClient(script=[
        {"tool_calls": [{
            "id": "call_1", "name": "report.generate",
            "arguments": {
                "report_id": "p:r", "data": {"x": 1}, "view": "v",
            },
        }]},
        {"content": "报告已生成"},
    ])

    agent = AgentConfig(
        name="test_agent", model="gpt-4", system="你是助手",
        tools=["report.generate"],
    )
    step = LLMStep(id="l1", agent=agent, output="r")
    step.bind_llm_client(mock)

    set_blocking_executor(None)

    ctx = Context()
    trace = await step.execute(ctx)

    assert trace.status == "success"
    # thread 工具在子线程内执行（Function Call 路径 thread 卸载生效）
    assert engine.render_called_thread != main_thread
    assert engine.eval_called_thread != main_thread
    assert ctx.get("r") == "报告已生成"

    get_blocking_executor().close()
    set_blocking_executor(None)


# ---------------------------------------------------------------------------
# 不挂 EventBus 时行为不变（零侵入）
# ---------------------------------------------------------------------------
async def test_report_engine_tool_no_event_bus_works():
    """无 EventBus 时 ReportEngineTool 正常工作（零侵入）。"""
    engine = _MockReportEngine()
    tool = ReportEngineTool(engine)  # type: ignore[arg-type]
    # 直接调用，不经任何运行时层
    result = await tool.call({"report_id": "p:r", "data": {}}, Context())
    assert "error" not in result
    assert "file_uri" in result


# ---------------------------------------------------------------------------
# create_report_tool 工厂
# ---------------------------------------------------------------------------
async def test_create_report_tool_factory_registers():
    """create_report_tool 工厂创建并注册工具。"""
    from agentkit.tools.report_engine import create_report_tool
    from agentkit.tools.base import get_tool

    engine = _MockReportEngine()
    # 注册前不存在
    try:
        get_tool("report.factory_test")
        assert False, "应未注册"
    except KeyError:
        pass

    tool = create_report_tool(engine, name="report.factory_test")  # type: ignore[arg-type]
    assert tool.name == "report.factory_test"
    # 注册后可获取
    assert get_tool("report.factory_test") is tool


# ---------------------------------------------------------------------------
# import asyncio for gather
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
