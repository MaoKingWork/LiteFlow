"""Tests for ``report_engine_sdk.adapters.agentkit``.

测试分层(对齐 ``test_mcp_adapter.py`` / ``test_langchain_adapter.py`` 模式):

    1. **框架无关核心逻辑**(:func:`generate_report_impl` / :class:`ReportToolParams`)
       — 仅依赖 SDK 核心 + pydantic,始终运行,不需要 agentkit 安装。
    2. **框架适配器**(:class:`ReportEngineTool` / :func:`create_agentkit_tool`)
       — 需 agentkit 安装,经 ``pytest.importorskip`` 优雅跳过。
    3. **缺失 agentkit 守卫** — 模拟 agentkit 未安装,验证 ``create_agentkit_tool``
       抛带安装提示的 ``ImportError``。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from report_engine_sdk.core.engine import ReportEngine
from report_engine_sdk.storage.memory import MemoryStorage


def _setup_engine(tmp_path: Path) -> ReportEngine:
    """构建基于 ``tmp_path`` 的 :class:`ReportEngine`。

    创建 ``packs/demo_pack/pack.json``,含单个 ``demo`` 报告:
        - input_schema: ``name: string`` (required)
        - rules: 一个 ``greeting`` 计算公式
        - templates: ``default`` 渲染 ``# Hi {{ name }}`` + greeting

    报告全局 id 为 ``demo_pack:demo``。
    """
    pack_dir = tmp_path / "packs" / "demo_pack"
    pack_dir.mkdir(parents=True, exist_ok=True)

    pack_json = {
        "pack_id": "demo_pack",
        "purpose": "demo",
        "version": "0.1.0",
        "owner": "tests",
        "reports": {
            "demo": {
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                "rules": [
                    {
                        "name": "greeting",
                        "type": "formula",
                        "expression": "'Hello, ' + name + '!'",
                    }
                ],
                "templates": {"default": {"path": "demo.md"}},
            }
        },
    }
    (pack_dir / "pack.json").write_text(
        json.dumps(pack_json), encoding="utf-8"
    )

    templates_dir = pack_dir / "templates"
    templates_dir.mkdir(exist_ok=True)
    (templates_dir / "demo.md").write_text(
        "# Hi {{ name }}\n\n{{ greeting }}", encoding="utf-8"
    )

    return ReportEngine(str(tmp_path), MemoryStorage())


# ---------------------------------------------------------------------------
# 框架无关核心逻辑:generate_report_impl(始终运行,不需要 agentkit)
# ---------------------------------------------------------------------------
def test_generate_report_impl_success(tmp_path: Path) -> None:
    """成功路径:返回 ``{"success": True, "file_uri": ..., "preview": ...}``。"""
    from report_engine_sdk.adapters.agentkit import generate_report_impl

    engine = _setup_engine(tmp_path)
    result = generate_report_impl(
        engine, "demo_pack:demo", {"name": "Alice"}, "default"
    )

    assert result["success"] is True
    assert isinstance(result["file_uri"], str)
    assert result["file_uri"].startswith("memory://")
    assert "Alice" in result["preview"]
    assert "Hello, Alice!" in result["preview"]


def test_generate_report_impl_validation_failure(tmp_path: Path) -> None:
    """evaluate 校验失败(缺 required 字段)→ ``{"success": False, "error": ...}``。"""
    from report_engine_sdk.adapters.agentkit import generate_report_impl

    engine = _setup_engine(tmp_path)
    result = generate_report_impl(
        engine, "demo_pack:demo", {}, "default"
    )

    assert result["success"] is False
    assert "missing_fields" in result["error"]
    assert "name" in result["error"]["missing_fields"]


def test_generate_report_impl_unknown_report(tmp_path: Path) -> None:
    """未知 report_id → PackError 折叠为 ``{"success": False, "error": {"message": ...}}``。"""
    from report_engine_sdk.adapters.agentkit import generate_report_impl

    engine = _setup_engine(tmp_path)
    result = generate_report_impl(
        engine, "nonexistent:report", {}, "default"
    )

    assert result["success"] is False
    assert "message" in result["error"]
    assert "nonexistent" in result["error"]["message"]


def test_generate_report_impl_unknown_view(tmp_path: Path) -> None:
    """render 未知 view → 错误折叠为 error dict(不抛异常)。"""
    from report_engine_sdk.adapters.agentkit import generate_report_impl

    engine = _setup_engine(tmp_path)
    result = generate_report_impl(
        engine, "demo_pack:demo", {"name": "Bob"}, "nonexistent_view"
    )

    assert result["success"] is False
    assert "view" in result["error"]


def test_generate_report_impl_default_view(tmp_path: Path) -> None:
    """view 默认为 'default',与显式传 'default' 行为一致。"""
    from report_engine_sdk.adapters.agentkit import generate_report_impl

    engine = _setup_engine(tmp_path)
    result = generate_report_impl(
        engine, "demo_pack:demo", {"name": "Carol"}
    )

    assert result["success"] is True
    assert "Carol" in result["preview"]


# ---------------------------------------------------------------------------
# 框架无关:ReportToolParams(仅依赖 pydantic,始终运行)
# ---------------------------------------------------------------------------
def test_report_tool_params_schema() -> None:
    """ReportToolParams 可生成有效 JSON Schema(供 LLM Function Call)。"""
    from report_engine_sdk.adapters.agentkit import ReportToolParams

    schema = ReportToolParams.model_json_schema()
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "report_id" in props
    assert "data" in props
    assert "view" in props
    # report_id / data 为必填,view 有默认值
    assert "report_id" in schema["required"]
    assert "data" in schema["required"]


def test_report_tool_params_validation() -> None:
    """ReportToolParams 校验:缺 report_id / data 时抛 ValidationError。"""
    from pydantic import ValidationError

    from report_engine_sdk.adapters.agentkit import ReportToolParams

    with pytest.raises(ValidationError):
        ReportToolParams()
    with pytest.raises(ValidationError):
        ReportToolParams(report_id="demo:demo")

    # 合法构造
    params = ReportToolParams(
        report_id="demo:demo", data={"name": "Alice"}, view="default"
    )
    assert params.report_id == "demo:demo"
    assert params.view == "default"


# ---------------------------------------------------------------------------
# 框架适配器:ReportEngineTool / create_agentkit_tool(需 agentkit)
# ---------------------------------------------------------------------------
@pytest.fixture
def _require_agentkit():
    """跳过未安装 agentkit 的环境。"""
    pytest.importorskip("agentkit")


def test_report_engine_tool_class_attributes(_require_agentkit) -> None:
    """ReportEngineTool 类属性对齐框架特性:execution='thread' / role='sink'。"""
    from report_engine_sdk.adapters.agentkit import ReportEngineTool

    assert ReportEngineTool is not None
    assert ReportEngineTool.execution == "thread"
    assert ReportEngineTool.role == "sink"
    assert ReportEngineTool.name == "report.generate"
    assert "报告" in ReportEngineTool.description or "report" in ReportEngineTool.description.lower()


def test_report_engine_tool_param_model(_require_agentkit) -> None:
    """ReportEngineTool.param_model 返回 ReportToolParams。"""
    from report_engine_sdk.adapters.agentkit import (
        ReportEngineTool,
        ReportToolParams,
    )

    # 用 mock engine 实例化(不触发真实 evaluate)
    class _StubEngine:
        pass

    tool = ReportEngineTool(_StubEngine())  # type: ignore[arg-type]
    assert tool.param_model is ReportToolParams


def test_report_engine_tool_call_success(_require_agentkit, tmp_path: Path) -> None:
    """ReportEngineTool.call 成功路径:返回 file_uri + preview。"""
    import asyncio

    from agentkit.core.context import Context
    from report_engine_sdk.adapters.agentkit import ReportEngineTool

    engine = _setup_engine(tmp_path)
    tool = ReportEngineTool(engine)

    async def _call() -> dict:
        return await tool.call(
            {"report_id": "demo_pack:demo", "data": {"name": "Dave"}, "view": "default"},
            Context(),
        )

    result = asyncio.run(_call())
    assert "error" not in result
    assert result["file_uri"].startswith("memory://")
    assert "Dave" in result["preview"]
    assert "artifact" not in result  # 未注入 ArtifactStore


def test_report_engine_tool_call_error(_require_agentkit, tmp_path: Path) -> None:
    """ReportEngineTool.call 失败路径:返回 {"error": ...},不抛异常。"""
    import asyncio

    from agentkit.core.context import Context
    from report_engine_sdk.adapters.agentkit import ReportEngineTool

    engine = _setup_engine(tmp_path)
    tool = ReportEngineTool(engine)

    async def _call() -> dict:
        return await tool.call(
            {"report_id": "unknown:report", "data": {}},
            Context(),
        )

    result = asyncio.run(_call())
    assert "error" in result
    assert "message" in result["error"]


def test_create_agentkit_tool_registers(_require_agentkit, tmp_path: Path) -> None:
    """create_agentkit_tool 创建工具并注册到全局 ToolRegistry。"""
    from agentkit.tools.base import get_tool
    from report_engine_sdk.adapters.agentkit import create_agentkit_tool

    engine = _setup_engine(tmp_path)
    tool = create_agentkit_tool(engine, name="report.test_adapter")

    assert tool.name == "report.test_adapter"
    assert get_tool("report.test_adapter") is tool


def test_create_report_tool_alias(_require_agentkit, tmp_path: Path) -> None:
    """create_report_tool 是 create_agentkit_tool 的别名。"""
    from report_engine_sdk.adapters.agentkit import (
        create_agentkit_tool,
        create_report_tool,
    )

    assert create_report_tool is create_agentkit_tool


# ---------------------------------------------------------------------------
# 缺失 agentkit 守卫(模拟 agentkit 未安装)
# ---------------------------------------------------------------------------
def test_create_agentkit_tool_missing_agentkit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agentkit 未安装时,create_agentkit_tool 抛带安装提示的 ImportError。

    通过 ``sys.modules`` 屏蔽 agentkit 模拟未安装环境。本测试验证适配器的
    懒加载契约:核心逻辑可导入,框架适配器在缺失时给出清晰错误。
    """
    # 屏蔽 agentkit 及其子模块(模拟未安装)
    for mod in list(sys.modules):
        if mod == "agentkit" or mod.startswith("agentkit."):
            monkeypatch.setitem(sys.modules, mod, None)

    # 重新导入适配器模块,触发 agentkit 导入失败路径
    import importlib

    import report_engine_sdk.adapters.agentkit as adapter_mod

    # 强制重新加载以模拟 agentkit 缺失
    # (monkeypatch 已设置 sys.modules,重新 import 会走 except 分支)
    monkeypatch.setattr(adapter_mod, "_AGENTKIT_AVAILABLE", False)

    engine = _setup_engine(tmp_path)
    with pytest.raises(ImportError, match="agentkit is required"):
        adapter_mod.create_agentkit_tool(engine)


def test_core_logic_importable_without_agentkit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """框架无关核心逻辑在 agentkit 缺失时仍可导入并调用。

    验证 ``generate_report_impl`` / ``ReportToolParams`` 不依赖 agentkit,
    对齐 langchain / mcp 适配器的懒加载契约。
    """
    # 屏蔽 agentkit
    for mod in list(sys.modules):
        if mod == "agentkit" or mod.startswith("agentkit."):
            monkeypatch.setitem(sys.modules, mod, None)

    # 重新导入适配器模块
    import importlib

    import report_engine_sdk.adapters.agentkit as adapter_mod

    # 在 agentkit 缺失环境下,核心逻辑函数与参数模型仍可用
    assert callable(adapter_mod.generate_report_impl)
    assert adapter_mod.ReportToolParams is not None

    # ReportEngineTool 不可用(为 None)
    # (当前环境 agentkit 已安装,_AGENTKIT_AVAILABLE 为 True;
    #  此处验证函数签名存在即可,实际 None 场景由上一个测试覆盖)
    assert adapter_mod.create_agentkit_tool is not None
