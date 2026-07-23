"""AgentKit Tool adapter — exposes ReportEngine as an agentkit ``Tool``.

This module is optional; install with: ``pip install report-engine-sdk[agentkit]``.

设计要点（对齐 langchain_tool.py / mcp_server.py 模式）：
    - **一等适配器**：与 ``api_router`` / ``langchain_tool`` / ``mcp_server`` 并列，
      把 ``ReportEngine`` 包装为 agentkit ``Tool``，使 ``ToolStep`` 与
      ``LLMStep`` 的 Function Call 路径都能调用报告生成能力。
    - **框架无关核心逻辑**：:func:`generate_report_impl` 与 :class:`ReportToolParams`
      仅依赖 SDK 核心 + pydantic，可在不安装 agentkit 的环境下单测（对齐
      langchain / mcp 适配器的懒加载契约）。
    - **懒加载框架依赖**：``ReportEngineTool`` 类定义需要 agentkit 的 ``Tool``
      基类，因此 agentkit 在模块级尝试导入；缺失时类定义为 ``None``，
      ``create_agentkit_tool`` 抛带提示的 ``ImportError``。
    - **统一错误折叠**：捕获 ``PackError`` 与 ``success=False`` 的结构化失败，
      转为 ``{"error": ...}`` dict，从不抛异常（与 MCP 适配器一致）。

深度适配 agentkit 框架特性：
    - **``execution="thread"``**：经 :class:`BlockingExecutor` 卸载到子线程，
      避免 reportlab / python-docx / markdown2 同步阻塞库卡住事件循环
      （对齐 ``docs/visualization-design.md`` §5.5）。
    - **``role="sink"``**：语义角色为输出终端。
    - **``param_model=ReportToolParams``**：自动生成 JSON Schema 供 LLM
      Function Call。
    - **可选 ``ArtifactStore`` 联动**：注入后成功渲染时自动把 preview 落盘 +
      发 ``ARTIFACT_PRODUCED`` 事件，统一接入可视化事件流（对齐 §5.6）。
    - **Pydantic 参数模型**：供 LLM Function Call 的 JSON Schema 自动生成。

公开 API：
    - ReportEngineTool:    AgentKit Tool 适配器（agentkit 缺失时为 ``None``）
    - create_agentkit_tool: 便捷工厂（注册到全局 ToolRegistry）
    - create_report_tool:   ``create_agentkit_tool`` 的别名（向后兼容）
    - ReportToolParams:    参数 Pydantic Model（框架无关）
    - generate_report_impl: 框架无关核心逻辑（可独立单测）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from report_engine_sdk.core.engine import ReportEngine
from report_engine_sdk.core.pack_loader import PackError

# agentkit 为可选依赖：尝试导入，缺失时标记不可用。
# ReportToolParams / generate_report_impl 不依赖 agentkit，始终可导入。
try:
    from agentkit.tools.base import Tool, register
    from agentkit.runtime.artifact import ArtifactStore
    _AGENTKIT_AVAILABLE = True
except ImportError:
    _AGENTKIT_AVAILABLE = False
    Tool = None  # type: ignore[assignment,misc]
    register = None  # type: ignore[assignment]
    ArtifactStore = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from agentkit.core.context import Context


__all__ = [
    "ReportEngineTool",
    "create_agentkit_tool",
    "create_report_tool",
    "ReportToolParams",
    "generate_report_impl",
]


# ---------------------------------------------------------------------------
# 参数模型（仅依赖 pydantic，可独立单测）
# ---------------------------------------------------------------------------
class ReportToolParams(BaseModel):
    """报告生成工具的参数模型（供 LLM Function Call 理解）。

    Attributes:
        report_id: 报告全局标识 ``"<pack_id>:<report_name>"``，
                   如 ``"work_report:daily_briefing"``。
        data:      报告输入数据，需符合该报告 ``input_schema`` 的结构。
        view:      视图名（如 ``"summary"`` / ``"manager"``），默认 ``"default"``。
    """

    report_id: str = Field(
        ...,
        description="报告全局标识 '<pack_id>:<report_name>'，如 'work_report:daily_briefing'",
    )
    data: dict = Field(
        ...,
        description="报告输入数据，需符合该报告 input_schema 的结构",
    )
    view: str = Field(
        "default",
        description="视图名（如 'summary'、'manager'），默认 'default'",
    )


# ---------------------------------------------------------------------------
# 框架无关核心逻辑（可独立单测，不依赖 agentkit）
# ---------------------------------------------------------------------------
def generate_report_impl(
    engine: ReportEngine,
    report_id: str,
    data: dict,
    view: str = "default",
) -> dict:
    """驱动 evaluate → render 两步流程，返回结构化结果 dict。

    可在不依赖 agentkit 的环境下测试。捕获 ``PackError`` 与结构化失败，
    统一折叠为 ``{"success": False, "error": ...}``；普通流程错误从不抛异常。

    Args:
        engine:    已配置的 :class:`ReportEngine` 实例。
        report_id: 报告全局标识。
        data:      报告输入数据。
        view:      视图名。

    Returns:
        dict: 成功时 ``{"success": True, "file_uri": ..., "preview": ...}``；
              失败时 ``{"success": False, "error": {...}}``。
    """
    # evaluate
    try:
        eval_res = engine.evaluate(report_id, data)
    except PackError as e:
        return {"success": False, "error": {"message": str(e)}}
    except Exception as e:
        return {"success": False, "error": {"message": str(e)}}

    if not eval_res.success:
        return {"success": False, "error": eval_res.errors}

    # render
    try:
        render_res = engine.render(report_id, eval_res.data, view=view)
    except PackError as e:
        return {"success": False, "error": {"message": str(e)}}
    except Exception as e:
        return {"success": False, "error": {"message": str(e)}}

    if not render_res.success:
        return {"success": False, "error": render_res.errors}

    return {
        "success": True,
        "file_uri": render_res.file_uri,
        "preview": render_res.preview,
    }


# ---------------------------------------------------------------------------
# ReportEngineTool —— AgentKit Tool 适配器（agentkit 缺失时不定义）
# ---------------------------------------------------------------------------
if _AGENTKIT_AVAILABLE:

    class ReportEngineTool(Tool):  # type: ignore[misc,valid-type]
        """将 :class:`ReportEngine` 包装为 AgentKit :class:`Tool`。

        在 :meth:`call` 中驱动 evaluate → render 流程，返回报告 ``file_uri`` 与
        预览；可选联动 :class:`ArtifactStore` 把产物落盘并发 ``ARTIFACT_PRODUCED``
        事件。错误从不抛异常，返回 ``{"error": ...}`` dict（与 MCP 适配器一致）。

        Attributes:
            name:        注册名，默认 ``"report.generate"``。
            role:        语义角色 ``"sink"``（输出终端）。
            execution:   ``"thread"`` —— render 链（reportlab / python-docx /
                         markdown2）为同步阻塞库，经 :class:`BlockingExecutor`
                         卸载到子线程，避免阻塞主事件循环（对齐 §5.5）。
            description: 供 LLM Function Call 理解用途的自然语言描述。

        Args:
            engine:            已配置的 :class:`ReportEngine` 实例。
            artifact_store:    可选 :class:`ArtifactStore`；注入后成功渲染时自动
                               把 preview 落盘 + 发 ``ARTIFACT_PRODUCED`` 事件。
                               ``None`` 时仅返回 ``file_uri`` / ``preview``（行为
                               同未联动）。
            content_type:      产物 MIME 类型，默认 ``"text/markdown"``。
            artifact_step_id:  产物目录的 ``step_id``，默认用工具名 ``"report.generate"``。
                               仅供 :meth:`ArtifactStore.save` 的目录布局使用，
                               不影响事件 payload。
        """

        name = "report.generate"
        role = "sink"
        execution = "thread"
        description = (
            "生成格式化的 Markdown 报告。输入报告 ID、数据和视图名，"
            "返回报告的 file_uri 与内容预览。失败时返回 error 字段。"
        )

        def __init__(
            self,
            engine: ReportEngine,
            *,
            artifact_store: "ArtifactStore | None" = None,
            content_type: str = "text/markdown",
            artifact_step_id: str | None = None,
        ) -> None:
            """初始化报告工具。

            Args:
                engine:           已配置的 :class:`ReportEngine` 实例。
                artifact_store:   可选 :class:`ArtifactStore`，启用产物联动。
                content_type:     产物 MIME 类型。
                artifact_step_id: 产物目录 ``step_id``。
            """
            self._engine = engine
            self._artifact_store = artifact_store
            self._content_type = content_type
            self._artifact_step_id = artifact_step_id or self.name

        @property
        def param_model(self) -> type[BaseModel]:
            """返回参数 Pydantic Model，用于 JSON Schema 生成。"""
            return ReportToolParams

        async def call(self, params: dict, ctx: "Context") -> dict[str, Any]:
            """驱动 evaluate → render 流程，可选联动 ArtifactStore 落盘 + 发事件。

            Args:
                params: 调用参数，含 ``report_id`` / ``data`` / ``view``。
                ctx:    会话上下文（只读）。

            Returns:
                dict: 成功时 ``{"file_uri": ..., "preview": ...}``，注入
                      ``artifact_store`` 时额外含 ``"artifact": <ArtifactRef dict>``；
                      产物落盘失败时含 ``"artifact_error": <str>``（不阻断主流程）。
                      失败时 ``{"error": {...}}``。
            """
            report_id = params.get("report_id", "")
            data = params.get("data", {})
            view = params.get("view", "default")

            result = generate_report_impl(self._engine, report_id, data, view)

            if not result.get("success"):
                return {"error": result.get("error", {"message": "unknown error"})}

            response: dict[str, Any] = {
                "file_uri": result["file_uri"],
                "preview": result["preview"],
            }

            # 可选联动 ArtifactStore：把 preview 落盘 + 发 ARTIFACT_PRODUCED 事件。
            # 失败不阻断工具成功返回，但通过 artifact_error 字段可见。
            if self._artifact_store is not None:
                try:
                    ref = await self._artifact_store.save(
                        step_id=self._artifact_step_id,
                        content=result["preview"] or "",
                        content_type=self._content_type,
                        summary=f"Report {report_id} (view={view})",
                    )
                    response["artifact"] = ref.to_dict()
                except Exception as exc:
                    response["artifact_error"] = f"{type(exc).__name__}: {exc}"

            return response

else:  # agentkit 未安装时，类不可用
    ReportEngineTool = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# 便捷工厂
# ---------------------------------------------------------------------------
def create_agentkit_tool(
    engine: ReportEngine,
    *,
    name: str = "report.generate",
    artifact_store: "ArtifactStore | None" = None,
    content_type: str = "text/markdown",
    artifact_step_id: str | None = None,
) -> "ReportEngineTool":
    """便捷工厂：创建 :class:`ReportEngineTool` 并注册到全局 ``ToolRegistry``。

    Args:
        engine:            已配置的 :class:`ReportEngine` 实例。
        name:              注册名，默认 ``"report.generate"``。
        artifact_store:    可选 :class:`ArtifactStore`，启用产物联动。
        content_type:      产物 MIME 类型。
        artifact_step_id:  产物目录 ``step_id``。

    Returns:
        ReportEngineTool: 已注册的工具实例。

    Raises:
        ImportError: agentkit 未安装时。
    """
    if not _AGENTKIT_AVAILABLE:
        raise ImportError(
            "agentkit is required for the AgentKit adapter. "
            "Install it with: pip install report-engine-sdk[agentkit]"
        )
    tool = ReportEngineTool(  # type: ignore[misc]
        engine,
        artifact_store=artifact_store,
        content_type=content_type,
        artifact_step_id=artifact_step_id,
    )
    tool.name = name
    register(tool)  # type: ignore[misc]
    return tool


# 向后兼容别名：保留原 agentkit.tools.report_engine 模块的工厂名
create_report_tool = create_agentkit_tool
