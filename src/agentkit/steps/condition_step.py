"""steps.condition_step —— ConditionStep:内联条件分支,天然汇合。

本模块实现 AgentKit 的条件分支 Step:求值 ``when`` 表达式,为真执行 ``then``
子步骤序列,为假执行 ``else`` 子步骤序列。子步骤执行完毕后自动汇合到同级
下一步,结构天然无环、静态可检测。

设计要点:
    - ``when`` 表达式经 :func:`eval_expression` 安全求值(ast 白名单,无 eval)。
    - 子步骤为 ``BaseStep`` 实例列表,通过各自的 ``execute`` 执行(自动触发
      钩子 / 重试 / trace),ConditionStep 仅负责分支选择与编排。
    - 多路分支用 condition 链实现(上一个 condition 的 else 中放下一个 condition)。
    - ``output`` 通常为 None:子步骤各自写自己的 output key。

设计原则:
    - 高度模块化:仅依赖 ``steps.base`` / ``core.template``。
    - 可观测:子步骤的 trace 由各自 ``execute`` 记录,ConditionStep 自身 trace
      记录走了哪个分支。
    - 类型注解完整,中文 docstring。

公开 API:
    - ConditionStep: 条件分支 Step
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentkit.core.template import eval_expression
from agentkit.steps.base import BaseStep, register_step

if TYPE_CHECKING:
    from agentkit.config import RetryPolicy
    from agentkit.core.cancel import CancelToken
    from agentkit.core.context import Context
    from agentkit.core.hooks import LifecycleHooks
    from agentkit.llm.base import LLMClient
    from agentkit.mcp.manager import MCPManager

__all__ = ["ConditionStep"]


@register_step("condition")
class ConditionStep(BaseStep):
    """条件分支 Step:内联 ``then`` / ``else`` 子步骤,执行完天然汇合。

    求值 ``when`` 表达式,为真执行 ``then_steps``,为假执行 ``else_steps``。
    子步骤执行完毕后自动汇合到同级下一步。

    Args:
        id:          Step 实例标识。
        when:        条件表达式字符串,支持 ``{{var}}`` 与比较/布尔/算术运算。
        then_steps:  条件为真时执行的子步骤列表。
        else_steps:  条件为假时执行的子步骤列表(可为空)。
        output:      输出键名(通常为 None,子步骤各自写自己的 output)。
        retry:       实例级重试策略。
        timeout:     实例级超时秒数。

    用法示例(YAML)::

        - id: route
          type: condition
          when: "{{intent}} == 'query'"
          then:
            - id: fetch
              type: tool
              tool: db.query
              params: { sql: "..." }
              output: db_result
          else:
            - id: escalate
              type: tool
              tool: sink.wecom
              params: { content: "转人工" }
              output: escalate_result
    """

    type = "condition"

    def __init__(
        self,
        id: str = "",
        when: str = "",
        then_steps: list[BaseStep] | None = None,
        else_steps: list[BaseStep] | None = None,
        output: str | None = None,
        retry: "RetryPolicy | None" = None,
        timeout: float | None = None,
    ) -> None:
        super().__init__(id=id, output=output, retry=retry, timeout=timeout)
        self.when: str = when
        self.then_steps: list[BaseStep] = then_steps or []
        self.else_steps: list[BaseStep] = else_steps or []
        # 运行期 scratch:供 _enrich_trace 记录走了哪个分支
        self._branch_taken: str = ""
        self._current_hooks: "LifecycleHooks | None" = None
        self._current_retry_policy: "RetryPolicy | None" = None
        self._current_cancel_token: "CancelToken | None" = None

    def bind_mcp_manager(self, manager: "MCPManager | None") -> None:
        """重写:递归传播到 then/else 子步骤,确保分支内 LLMStep 也能拿到 MCP 工具。"""
        super().bind_mcp_manager(manager)
        for s in self.then_steps:
            s.bind_mcp_manager(manager)
        for s in self.else_steps:
            s.bind_mcp_manager(manager)

    def bind_llm_client(self, client: "LLMClient | None") -> None:
        """重写:递归传播到 then/else 子步骤,与 bind_mcp_manager 保持一致。"""
        super().bind_llm_client(client)
        for s in self.then_steps:
            s.bind_llm_client(client)
        for s in self.else_steps:
            s.bind_llm_client(client)

    def bind_blocking_executor(self, executor: Any) -> None:
        """重写:递归传播到 then/else 子步骤,与 bind_mcp_manager 保持一致。"""
        super().bind_blocking_executor(executor)
        for s in self.then_steps:
            s.bind_blocking_executor(executor)
        for s in self.else_steps:
            s.bind_blocking_executor(executor)

    def iter_child_steps(self) -> list[BaseStep]:
        """返回 then + else 子 Step。"""
        return list(self.then_steps) + list(self.else_steps)

    async def run(self, ctx: "Context") -> "Context":
        """求值条件并执行对应分支的子步骤序列。

        流程:求值 ``when`` → 选择分支 → 顺序执行子步骤(各自经 ``execute``
        编排,含钩子/重试/trace)→ 返回 ctx。
        """
        # 求值条件表达式
        result = eval_expression(self.when, ctx)
        taken = bool(result)
        self._branch_taken = "then" if taken else "else"

        steps = self.then_steps if taken else self.else_steps
        for step in steps:
            if self._current_cancel_token is not None and self._current_cancel_token.is_cancelled:
                break
            await step.execute(
                ctx,
                self._current_hooks,
                retry_policy=self._current_retry_policy,
                cancel_token=self._current_cancel_token,
            )
        return ctx

    async def execute(
        self,
        ctx: "Context",
        hooks: "LifecycleHooks | None" = None,
        *,
        retry_policy: "RetryPolicy | None" = None,
        cancel_token: "CancelToken | None" = None,
    ) -> "StepTrace":
        """重写:暂存 hooks / retry_policy / cancel_token,供子步骤 ``execute`` 使用。"""
        self._current_hooks = hooks
        self._current_retry_policy = retry_policy
        self._current_cancel_token = cancel_token
        return await super().execute(
            ctx, hooks, retry_policy=retry_policy, cancel_token=cancel_token
        )

    def _enrich_trace(self, trace: "StepTrace") -> None:
        """记录走了哪个分支。"""
        if self._branch_taken:
            trace.input_summary = f"branch={self._branch_taken}"
