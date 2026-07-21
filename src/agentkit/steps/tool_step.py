"""steps.tool_step —— ToolStep:直接调用 Tool(不经 LLM)。

本模块实现 AgentKit 中最直接的 Step 类型 :class:`ToolStep`。它从
``ToolRegistry`` 按名取出工具,解析参数模板后直接调用,把结果写入 Context。
适用于数据库查询、HTTP 请求、消息发送等不需要 LLM 介入的确定性操作。

与 :class:`~agentkit.steps.llm_step.LLMStep` 的区别:
    - 不调用 LLM,无 Function Call 循环,无输出契约保障链。
    - 参数经 ``resolve_value`` 解析 ``{{var}}`` / ``${ENV}`` 后直接传入 Tool。
    - 失败由 ``execute`` 的执行级重试处理(网络/瞬时错误)。

设计原则:
    - 高度模块化:仅依赖 ``steps.base`` / ``core.template`` / ``tools.base``。
    - 可观测:触发 ``on_tool_call`` Hook 并把调用记录回填到 ``StepTrace``。
    - 类型注解完整,中文 docstring。

公开 API:
    - ToolStep: Tool Step 实现
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentkit.core.template import resolve_value
from agentkit.steps.base import BaseStep, StepTrace, register_step
from agentkit.tools.base import get_tool

if TYPE_CHECKING:
    from agentkit.config import RetryPolicy
    from agentkit.core.context import Context
    from agentkit.core.hooks import LifecycleHooks

__all__ = ["ToolStep"]


@register_step("tool")
class ToolStep(BaseStep):
    """Tool 调用 Step:直接执行 Tool,不经 LLM。

    从 ``ToolRegistry`` 按名取工具,解析参数模板后调用,结果写入 Context。
    适用于 DB 查询、HTTP 请求、消息发送等确定性操作。

    Args:
        id:      Step 实例标识。
        tool:    工具注册名(如 ``"db.query"`` / ``"sink.wecom"``)。
        params:  调用参数模板(dict),支持 ``{{var}}`` / ``${ENV}``。
        output:  输出键名;工具返回的 dict 通过 ``ctx.set(output, result)`` 写入。
        role:    语义角色覆盖(可选);不指定时用工具自身的 ``role``。
        retry:   实例级重试策略。
        timeout: 实例级超时秒数。

    用法示例(YAML)::

        - id: fetch_orders
          type: tool
          tool: db.query
          params:
            sql: "SELECT * FROM orders WHERE date='{{date}}'"
          output: orders_raw
    """

    type = "tool"

    def __init__(
        self,
        id: str = "",
        tool: str = "",
        params: dict | None = None,
        output: str | None = None,
        role: str | None = None,
        retry: "RetryPolicy | None" = None,
        timeout: float | None = None,
    ) -> None:
        super().__init__(id=id, output=output, retry=retry, timeout=timeout)
        self.tool: str = tool
        self.params: dict = params or {}
        self.role_override: str | None = role
        # 运行期 scratch:供 _enrich_trace 回填
        self._tool_calls_record: list[dict] = []
        self._current_hooks: "LifecycleHooks | None" = None

    async def run(self, ctx: "Context") -> "Context":
        """执行工具调用并写入 output。

        流程:重置 scratch → 解析参数模板 → 取工具 → 调用 →
        触发 on_tool_call Hook → 写入 output。
        """
        self._tool_calls_record = []

        # 解析参数模板({{var}} / ${ENV})
        resolved_params = resolve_value(self.params, ctx)
        if not isinstance(resolved_params, dict):
            resolved_params = {"value": resolved_params}

        # 取工具(未注册抛 KeyError,由 execute 重试/钩子处理)
        tool_instance = get_tool(self.tool)

        # 调用工具
        result = await tool_instance.call(resolved_params, ctx)
        if not isinstance(result, dict):
            result = {"value": result}

        # 记录调用(供 _enrich_trace)
        record: dict[str, Any] = {
            "tool": self.tool,
            "arguments": resolved_params,
            "status": "ok",
            "result_summary": self._summarize(result),
        }
        self._tool_calls_record.append(record)

        # 触发 on_tool_call Hook
        if self._current_hooks is not None:
            await self._current_hooks.on_tool_call(
                tool_instance, resolved_params, result
            )

        # 写入 output
        if self.output:
            ctx.set(self.output, result)
        return ctx

    async def execute(
        self,
        ctx: "Context",
        hooks: "LifecycleHooks | None" = None,
        *,
        retry_policy: "RetryPolicy | None" = None,
    ) -> StepTrace:
        """重写:在调用 super().execute 前暂存 hooks,供 run 中触发 on_tool_call。"""
        self._current_hooks = hooks
        return await super().execute(ctx, hooks, retry_policy=retry_policy)

    def _enrich_trace(self, trace: StepTrace) -> None:
        """把工具调用记录回填到 trace。"""
        trace.tool_calls = list(self._tool_calls_record)
