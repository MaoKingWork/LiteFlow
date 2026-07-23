"""agentkit ↔ report_engine_sdk 端到端集成测试。

本测试用真实 :class:`ReportEngine` + 真实 SDK 配置包（``config/packs/``）
驱动 ``ReportEngineTool`` 完整链路：evaluate → render → file_uri → preview →
artifact 落盘 + 事件分发。验证两个模块的深度适配在真实环境下无缺陷。

覆盖场景：
    1. 单步 ToolStep 直接调用 report.generate，结果写入 Context
    2. 注入 ArtifactStore 后产物落盘 + 发 ARTIFACT_PRODUCED 事件
    3. LLMStep Function Call 路径触发报告生成
    4. 多视图渲染（manager / teacher）
    5. evaluate 失败时错误折叠为 ``{"error": ...}``
    6. 框架无关核心逻辑 :func:`generate_report_impl` 直接调用
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentkit.core.context import Context
from agentkit.runtime.artifact import ArtifactStore
from agentkit.runtime.blocking import (
    get_blocking_executor,
    set_blocking_executor,
)
from agentkit.runtime.event import EventBus, EventLog, EventType
from report_engine_sdk import MemoryStorage, ReportEngine
from report_engine_sdk.adapters.agentkit import (
    ReportEngineTool,
    create_report_tool,
    generate_report_impl,
)
from report_engine_sdk.core.pack_loader import PackError

# SDK 自带的 config/packs 路径（含 teacher_eval / ops_report / work_report 等示例）
CONFIG_DIR = str(
    Path(__file__).parent.parent.parent.parent
    / "report_engine_sdk"
    / "config"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def engine() -> ReportEngine:
    """基于 SDK 自带 config + MemoryStorage 的真实 ReportEngine。"""
    return ReportEngine(CONFIG_DIR, MemoryStorage())


@pytest.fixture
async def engine_with_artifact(tmp_path) -> tuple[ReportEngine, EventBus, ArtifactStore]:
    """返回 (engine, bus, store)，store 已注入 bus 用于事件分发。

    异步 fixture：结束时自动关闭 EventBus 释放订阅者队列。
    """
    run_id = "e2e_run"
    log = EventLog(run_id, base_dir=str(tmp_path))
    bus = EventBus(run_id, log=log)
    store = ArtifactStore(run_id, event_bus=bus, base_dir=str(tmp_path))
    eng = ReportEngine(CONFIG_DIR, MemoryStorage())
    yield eng, bus, store
    await bus.close()


# ---------------------------------------------------------------------------
# 框架无关核心逻辑（不依赖 agentkit 运行时）
# ---------------------------------------------------------------------------
def test_generate_report_impl_success(engine: ReportEngine) -> None:
    """框架无关核心逻辑：成功路径返回结构化 dict。"""
    facts = {
        "teacher_name": "张老师",
        "base_score": 95,
        "bonus": 5,
        "class_size": 45,
    }
    result = generate_report_impl(
        engine, "teacher_eval:performance", facts, view="manager",
    )
    assert result["success"] is True
    assert result["file_uri"].startswith("memory://")
    assert "管理层视图" in result["preview"]
    assert "张老师" in result["preview"]


def test_generate_report_impl_unknown_report(engine: ReportEngine) -> None:
    """未知 report_id → PackError 折叠为 error dict。"""
    result = generate_report_impl(
        engine, "unknown_pack:unknown_report", {},
    )
    assert result["success"] is False
    assert "message" in result["error"]


def test_generate_report_impl_validation_failure(engine: ReportEngine) -> None:
    """evaluate 校验失败 → errors 字段透传。"""
    # teacher_eval:performance 需要 teacher_name / base_score 等字段
    result = generate_report_impl(
        engine, "teacher_eval:performance", {},  # 缺所有 required 字段
    )
    assert result["success"] is False
    assert "missing_fields" in result["error"]


def test_generate_report_impl_render_unknown_view(engine: ReportEngine) -> None:
    """render 未知 view → 错误折叠为 error dict。"""
    facts = {
        "teacher_name": "张老师",
        "base_score": 95,
        "bonus": 5,
        "class_size": 45,
    }
    result = generate_report_impl(
        engine, "teacher_eval:performance", facts, view="nonexistent_view",
    )
    assert result["success"] is False
    assert "view" in result["error"]
    assert result["error"]["view"] == "nonexistent_view"


# ---------------------------------------------------------------------------
# ReportEngineTool 直接调用（不经 Step / 不挂 ArtifactStore）
# ---------------------------------------------------------------------------
async def test_tool_call_success_no_artifact_store(engine: ReportEngine) -> None:
    """无 ArtifactStore 时行为同原版：返回 file_uri + preview，无 artifact 字段。"""
    tool = ReportEngineTool(engine)
    ctx = Context()
    result = await tool.call(
        {
            "report_id": "ops_report:health_check",
            "data": {
                "service_name": "api-gateway",
                "uptime_pct": 99.9,
                "error_count": 3,
                "latency_ms": 45.2,
                "date_str": "2026-07-23",
            },
            "view": "default",
        },
        ctx,
    )
    assert "error" not in result
    assert result["file_uri"].startswith("memory://")
    assert "系统健康检查报告" in result["preview"]
    assert "api-gateway" in result["preview"]
    assert "artifact" not in result  # 未注入 ArtifactStore


async def test_tool_call_multi_view_rendering(engine: ReportEngine) -> None:
    """多视图渲染：manager / teacher 视图各自返回不同 preview。"""
    facts = {
        "teacher_name": "李老师",
        "base_score": 88,
        "bonus": 3,
        "class_size": 40,
    }
    tool = ReportEngineTool(engine)

    mgr_result = await tool.call(
        {"report_id": "teacher_eval:performance", "data": facts, "view": "manager"},
        Context(),
    )
    tch_result = await tool.call(
        {"report_id": "teacher_eval:performance", "data": facts, "view": "teacher"},
        Context(),
    )

    assert "error" not in mgr_result
    assert "error" not in tch_result
    assert "管理层视图" in mgr_result["preview"]
    assert "教师视图" in tch_result["preview"]


async def test_tool_call_evaluate_failure_returns_error(engine: ReportEngine) -> None:
    """evaluate 失败 → 返回 {"error": {...}}，不抛异常。"""
    tool = ReportEngineTool(engine)
    result = await tool.call(
        {"report_id": "teacher_eval:performance", "data": {}},  # 缺字段
        Context(),
    )
    assert "error" in result
    assert "missing_fields" in result["error"]


async def test_tool_call_unknown_report_returns_error(engine: ReportEngine) -> None:
    """未知 report_id → 返回 {"error": {"message": ...}}，不抛异常。"""
    tool = ReportEngineTool(engine)
    result = await tool.call(
        {"report_id": "no:such_report", "data": {}},
        Context(),
    )
    assert "error" in result
    assert "message" in result["error"]


# ---------------------------------------------------------------------------
# ArtifactStore 联动（真实 SDK + 真实 ArtifactStore + 真实 EventBus）
# ---------------------------------------------------------------------------
async def test_tool_call_with_artifact_store_emits_event(
    engine_with_artifact,
) -> None:
    """注入 ArtifactStore 后产物落盘 + 发 ARTIFACT_PRODUCED 事件。"""
    engine, bus, store = engine_with_artifact
    tool = ReportEngineTool(engine, artifact_store=store)

    sub = await bus.subscribe()
    result = await tool.call(
        {
            "report_id": "ops_report:health_check",
            "data": {
                "service_name": "api-gateway",
                "uptime_pct": 99.9,
                "error_count": 3,
                "latency_ms": 45.2,
                "date_str": "2026-07-23",
            },
        },
        Context(),
    )

    # 主流程成功
    assert "error" not in result
    assert result["file_uri"].startswith("memory://")

    # artifact 字段含完整 ArtifactRef
    assert "artifact" in result
    art = result["artifact"]
    assert art["step_id"] == "report.generate"
    assert art["content_type"] == "text/markdown"
    assert art["size"] > 0
    assert art["md5"]
    assert "api-gateway" in art["summary"] or "Report" in art["summary"]

    # 事件总线收到 ARTIFACT_PRODUCED 事件
    event = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert event is not None
    assert event.type == EventType.ARTIFACT_PRODUCED
    assert event.payload["id"] == art["id"]
    assert event.payload["uri"] == art["uri"]
    assert event.payload["content_type"] == "text/markdown"

    await bus.close()


async def test_tool_call_artifact_persisted_to_disk(
    engine_with_artifact, tmp_path,
) -> None:
    """产物文件实际落盘到 output/runs/{run_id}/artifacts/{step_id}/。"""
    engine, bus, store = engine_with_artifact
    tool = ReportEngineTool(engine, artifact_store=store)

    result = await tool.call(
        {
            "report_id": "work_report:daily_briefing",
            "data": {
                "user_name": "Bob",
                "summary_text": "完成 P0 可视化适配。",
                "action_items": ["review PR", "部署"],
                "date_str": "2026-07-23",
            },
            "view": "summary",
        },
        Context(),
    )

    assert "artifact" in result
    art = result["artifact"]
    # 文件实际存在
    art_path = Path(art["uri"])
    assert art_path.exists(), f"产物文件未落盘: {art_path}"
    # 内容可读
    content = art_path.read_text(encoding="utf-8")
    assert len(content) > 0
    assert "Bob" in content or "daily" in content.lower()

    await bus.close()


# ---------------------------------------------------------------------------
# ToolStep 集成（声明式 YAML 调用路径）
# ---------------------------------------------------------------------------
async def test_tool_step_integration(engine: ReportEngine) -> None:
    """ToolStep 按 tool='report.generate' 取工具并调用，结果写入 Context。"""
    from agentkit.steps.tool_step import ToolStep

    tool = ReportEngineTool(engine)
    register_local_tool(tool)

    set_blocking_executor(None)  # 重置全局单例

    step = ToolStep(
        id="gen_ops_report",
        tool="report.generate",
        params={
            "report_id": "ops_report:health_check",
            "data": {
                "service_name": "test-svc",
                "uptime_pct": 99.5,
                "error_count": 5,
                "latency_ms": 80.0,
                "date_str": "2026-07-23",
            },
            "view": "default",
        },
        output="ops_report_result",
    )
    ctx = Context()
    trace = await step.execute(ctx)

    assert trace.status == "success"
    result = ctx.get("ops_report_result")
    assert "error" not in result
    assert "test-svc" in result["preview"]

    get_blocking_executor().close()
    set_blocking_executor(None)


async def test_tool_step_with_artifact_store(engine_with_artifact) -> None:
    """ToolStep 调用带 ArtifactStore 的工具，事件流正常分发。"""
    from agentkit.steps.tool_step import ToolStep

    engine, bus, store = engine_with_artifact
    tool = ReportEngineTool(engine, artifact_store=store)
    register_local_tool(tool)

    sub = await bus.subscribe()
    set_blocking_executor(None)

    step = ToolStep(
        id="gen_with_artifact",
        tool="report.generate",
        params={
            "report_id": "ops_report:health_check",
            "data": {
                "service_name": "artifact-svc",
                "uptime_pct": 99.9,
                "error_count": 1,
                "latency_ms": 30.0,
                "date_str": "2026-07-23",
            },
        },
        output="ops_result",
    )
    ctx = Context()
    trace = await step.execute(ctx)

    assert trace.status == "success"
    result = ctx.get("ops_result")
    assert "artifact" in result

    # 收到 ARTIFACT_PRODUCED 事件
    event = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert event.type == EventType.ARTIFACT_PRODUCED

    get_blocking_executor().close()
    set_blocking_executor(None)
    await bus.close()


# ---------------------------------------------------------------------------
# LLMStep Function Call 集成
# ---------------------------------------------------------------------------
async def test_llm_step_function_call(engine: ReportEngine) -> None:
    """LLMStep Function Call 路径调用 report.generate，thread 卸载生效。"""
    from agentkit.core.agent import AgentConfig
    from agentkit.llm.mock import MockClient
    from agentkit.steps.llm_step import LLMStep

    tool = ReportEngineTool(engine)
    register_local_tool(tool)

    # MockClient：第一轮 tool_call，第二轮纯文本
    mock = MockClient(script=[
        {"tool_calls": [{
            "id": "call_1", "name": "report.generate",
            "arguments": {
                "report_id": "ops_report:health_check",
                "data": {
                    "service_name": "llm-svc",
                    "uptime_pct": 99.9,
                    "error_count": 2,
                    "latency_ms": 50.0,
                    "date_str": "2026-07-23",
                },
                "view": "default",
            },
        }]},
        {"content": "报告已生成并落盘。"},
    ])

    agent = AgentConfig(
        name="report_agent", model="gpt-4",
        system="你是报告生成助手", tools=["report.generate"],
    )
    step = LLMStep(id="llm_gen", agent=agent, output="reply")
    step.bind_llm_client(mock)

    set_blocking_executor(None)

    ctx = Context()
    trace = await step.execute(ctx)

    assert trace.status == "success"
    assert ctx.get("reply") == "报告已生成并落盘。"

    get_blocking_executor().close()
    set_blocking_executor(None)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def register_local_tool(tool) -> None:
    """注册工具到全局 ToolRegistry（conftest 会自动清理）。"""
    from agentkit.tools.base import register
    register(tool)
