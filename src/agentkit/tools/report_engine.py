"""tools.report_engine —— report_engine_sdk 集成适配器。

将已有的 ``report_engine_sdk.ReportEngine`` 包装为 AgentKit 的 ``Tool``,
使 ``LLMStep`` 可通过 Function Call 调用报告生成能力。

集成思路:
    - ``ReportEngineTool`` 持有一个 ``ReportEngine`` 实例,在 ``call`` 中
      驱动 evaluate → render 两步流程。
    - 参数通过 ``param_model``(Pydantic BaseModel)声明 JSON Schema,
      供 LLM Function Call 理解参数结构。
    - ``role`` 设为 ``sink``:报告生成是输出终端操作。
    - 错误不抛异常,返回 ``{"error": ...}`` dict,与 MCP 适配器一致。

依赖:
    - report_engine_sdk(同仓库已有项目)
    - jinja2(report_engine_sdk 的渲染依赖)

公开 API:
    - ReportEngineTool:    AgentKit Tool 适配器
    - create_report_tool:  便捷工厂函数
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from agentkit.tools.base import Tool, register

if TYPE_CHECKING:
    from agentkit.core.context import Context
    from report_engine_sdk import ReportEngine

__all__ = ["ReportEngineTool", "create_report_tool", "ReportToolParams"]


class ReportToolParams(BaseModel):
    """报告生成工具的参数模型(供 LLM Function Call 理解)。"""

    report_id: str = Field(
        ...,
        description="报告全局标识 '<pack_id>:<report_name>',如 'work_report:daily_briefing'",
    )
    data: dict = Field(
        ...,
        description="报告输入数据,需符合该报告 input_schema 的结构",
    )
    view: str = Field(
        "default",
        description="视图名(如 'summary'、'manager'、'teacher'),默认 'default'",
    )


class ReportEngineTool(Tool):
    """将 ``report_engine_sdk.ReportEngine`` 包装为 AgentKit Tool。

    在 ``call`` 中驱动 evaluate → render 流程,返回报告 file_uri 或错误信息。
    错误不抛异常,返回 ``{"error": ...}`` dict,与 MCP 适配器行为一致。

    Attributes:
        name:        注册名,默认 ``"report.generate"``。
        role:        语义角色 ``"sink"``(输出终端)。
        description: 供 LLM 理解用途的自然语言描述。
    """

    name = "report.generate"
    role = "sink"
    description = (
        "生成格式化的 Markdown 报告。输入报告 ID、数据和视图名,"
        "返回报告的 file_uri 与内容预览。失败时返回 error 字段。"
    )

    def __init__(self, engine: "ReportEngine") -> None:
        """初始化报告工具。

        Args:
            engine: 已配置的 ``ReportEngine`` 实例。
        """
        self._engine = engine

    @property
    def param_model(self) -> type[BaseModel]:
        """返回参数 Pydantic Model,用于 JSON Schema 生成。"""
        return ReportToolParams

    async def call(self, params: dict, ctx: "Context") -> dict[str, Any]:
        """驱动 evaluate → render 流程。

        Args:
            params: 调用参数,含 ``report_id`` / ``data`` / ``view``。
            ctx:    会话上下文(只读)。

        Returns:
            dict: 成功时 ``{"file_uri": ..., "preview": ...}``;
                  失败时 ``{"error": {"message": ...}}`` 或 ``{"error": <errors>}``。
        """
        report_id = params.get("report_id", "")
        data = params.get("data", {})
        view = params.get("view", "default")

        # evaluate
        try:
            eval_res = self._engine.evaluate(report_id, data)
        except Exception as exc:
            return {"error": {"message": str(exc)}}

        if not eval_res.success:
            return {"error": eval_res.errors}

        # render
        try:
            render_res = self._engine.render(report_id, eval_res.data, view=view)
        except Exception as exc:
            return {"error": {"message": str(exc)}}

        if not render_res.success:
            return {"error": render_res.errors}

        return {
            "file_uri": render_res.file_uri,
            "preview": render_res.preview,
        }


def create_report_tool(
    engine: "ReportEngine",
    name: str = "report.generate",
) -> ReportEngineTool:
    """便捷工厂:创建 ``ReportEngineTool`` 并注册到全局 ToolRegistry。

    Args:
        engine: 已配置的 ``ReportEngine`` 实例。
        name:   注册名,默认 ``"report.generate"``。

    Returns:
        ReportEngineTool: 已注册的工具实例。
    """
    tool = ReportEngineTool(engine)
    tool.name = name
    register(tool)
    return tool
