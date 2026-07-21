"""steps.loop_step —— LoopStep:循环 / 重试。

本模块实现 AgentKit 的循环 Step,支持两种模式:

1. **迭代列表**(``iter``):遍历 ``{{items}}`` 解析出的列表,对每个元素执行
   内部 Step,结果自动 append 到 ``output``。
2. **条件重试**(``until``):重复执行内部 Step 直到 ``until`` 表达式为真,
   达到 ``max`` 上限时按 ``on_max`` 决策(fail / continue)。

约束:Loop 内只允许单 Step 或 ConditionStep。嵌套循环需拆成 SubWorkflow。

设计原则:
    - 高度模块化:仅依赖 ``steps.base`` / ``core.template`` / ``config``。
    - 安全:``max`` 硬上限防死循环;``iter`` 模式也受 ``max`` 约束。
    - 类型注解完整,中文 docstring。

公开 API:
    - LoopStep: 循环 Step
    - LoopMaxReachedError: on_max=fail 且达到上限时抛出
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentkit.config import get_default
from agentkit.core.template import eval_expression, resolve_value
from agentkit.steps.base import BaseStep, register_step

if TYPE_CHECKING:
    from agentkit.config import RetryPolicy
    from agentkit.core.context import Context
    from agentkit.core.hooks import LifecycleHooks
    from agentkit.mcp.manager import MCPManager
    from agentkit.steps.base import StepTrace

__all__ = ["LoopStep", "LoopMaxReachedError"]


class LoopMaxReachedError(Exception):
    """``until`` 模式达到 ``max`` 上限且 ``on_max='fail'`` 时抛出。"""


@register_step("loop")
class LoopStep(BaseStep):
    """循环 Step:迭代列表或条件重试。

    两种模式互斥:提供 ``iter`` 为迭代模式,提供 ``until`` 为条件重试模式。

    Args:
        id:      Step 实例标识。
        iter:    迭代模式:``{{var}}`` 模板,解析为列表进行遍历。
        item_var: 迭代模式:当前元素的变量名(YAML 中用 ``as``,内部 Step 通过 ``{{item}}`` 访问)。
        until:   条件重试模式:表达式,为真时停止循环。
        step:    循环体(单个 ``BaseStep`` 或 ``ConditionStep``)。
        max:     最大迭代次数(硬上限,防死循环);``None`` 用全局默认。
        on_max:  条件重试达到上限时的策略:``fail``(抛异常) | ``continue``(跳过)。
        output:  迭代模式:收集结果的键名;不指定时用内部 Step 的 output。
        retry:   实例级重试策略。
        timeout: 实例级超时秒数。

    用法示例 — 迭代列表(YAML)::

        - id: batch_process
          type: loop
          iter: "{{items}}"
          as: item
          max: 100
          step:
            id: process_one
            type: llm
            agent: processor
            input: "{{item}}"
            output: results

    用法示例 — 条件重试(YAML)::

        - id: retry_until_valid
          type: loop
          until: "'{{validation_pass}}' == true"
          max: 3
          step:
            id: regenerate
            type: llm
            agent: generator
            input: "{{draft}}"
            output: draft
          on_max: fail
    """

    type = "loop"

    def __init__(
        self,
        id: str = "",
        iter: str | None = None,
        item_var: str = "item",
        until: str | None = None,
        step: BaseStep | None = None,
        max: int | None = None,
        on_max: str = "fail",
        output: str | None = None,
        retry: "RetryPolicy | None" = None,
        timeout: float | None = None,
    ) -> None:
        super().__init__(id=id, output=output, retry=retry, timeout=timeout)
        self.iter_tpl: str | None = iter
        self.item_var: str = item_var
        self.until_expr: str | None = until
        self.body: BaseStep | None = step
        self.max_iterations: int | None = max
        self.on_max: str = on_max
        # 运行期 scratch
        self._current_hooks: "LifecycleHooks | None" = None
        self._current_retry_policy: "RetryPolicy | None" = None
        self._loop_count: int = 0

    def bind_mcp_manager(self, manager: "MCPManager | None") -> None:
        """重写:递归传播到循环体 body,确保循环内 LLMStep 也能拿到 MCP 工具。"""
        super().bind_mcp_manager(manager)
        if self.body is not None:
            self.body.bind_mcp_manager(manager)

    def bind_llm_client(self, client: "LLMClient | None") -> None:
        """重写:递归传播到循环体 body,与 bind_mcp_manager 保持一致。"""
        super().bind_llm_client(client)
        if self.body is not None:
            self.body.bind_llm_client(client)

    async def run(self, ctx: "Context") -> "Context":
        """根据模式执行迭代或条件重试。"""
        self._loop_count = 0

        if self.iter_tpl is not None:
            await self._run_iter_mode(ctx)
        elif self.until_expr is not None:
            await self._run_until_mode(ctx)
        else:
            raise ValueError(
                f"LoopStep {self.id!r} 必须配置 iter 或 until 之一"
            )
        return ctx

    # ------------------------------------------------------------------
    # 迭代模式
    # ------------------------------------------------------------------
    async def _run_iter_mode(self, ctx: "Context") -> None:
        """遍历 ``iter`` 列表,对每个元素执行内部 Step,收集结果。"""
        assert self.body is not None
        assert self.iter_tpl is not None

        # 解析迭代列表
        items = resolve_value(self.iter_tpl, ctx)
        if items is None:
            items = []
        elif isinstance(items, (str, bytes, dict)):
            # 标量 / dict 不可迭代为列表,包装为单元素列表
            items = [items]
        else:
            try:
                items = list(items)
            except TypeError:
                items = [items]

        # max 上限
        max_iter = self._effective_max()
        if len(items) > max_iter:
            items = items[:max_iter]

        # 确定收集结果的 output key
        output_key = self.output or self.body.output
        collected: list[Any] = []

        for item in items:
            # 设置当前元素到 Context
            ctx.set(self.item_var, item)
            # 执行内部 Step
            await self.body.execute(
                ctx,
                self._current_hooks,
                retry_policy=self._current_retry_policy,
            )
            self._loop_count += 1
            # 收集结果
            if output_key and ctx.has(output_key):
                collected.append(ctx.get(output_key))

        # 写入收集结果
        if output_key:
            ctx.set(output_key, collected)

    # ------------------------------------------------------------------
    # 条件重试模式
    # ------------------------------------------------------------------
    async def _run_until_mode(self, ctx: "Context") -> None:
        """重复执行内部 Step 直到 ``until`` 为真或达到 ``max`` 上限。"""
        assert self.body is not None
        assert self.until_expr is not None

        max_iter = self._effective_max()
        for _ in range(max_iter):
            await self.body.execute(
                ctx,
                self._current_hooks,
                retry_policy=self._current_retry_policy,
            )
            self._loop_count += 1

            # 检查退出条件
            if eval_expression(self.until_expr, ctx):
                return

        # 达到上限
        if self.on_max == "fail":
            raise LoopMaxReachedError(
                f"LoopStep {self.id!r} 达到最大迭代次数 {max_iter} "
                f"且 until 条件未满足"
            )
        # on_max == "continue":静默继续

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _effective_max(self) -> int:
        """返回生效的最大迭代次数。"""
        if self.max_iterations is not None:
            return self.max_iterations
        return int(get_default("default_max_loop_iterations"))

    async def execute(
        self,
        ctx: "Context",
        hooks: "LifecycleHooks | None" = None,
        *,
        retry_policy: "RetryPolicy | None" = None,
    ) -> "StepTrace":
        """重写:暂存 hooks 与 retry_policy,供内部 Step ``execute`` 使用。"""
        self._current_hooks = hooks
        self._current_retry_policy = retry_policy
        return await super().execute(ctx, hooks, retry_policy=retry_policy)

    def _enrich_trace(self, trace: "StepTrace") -> None:
        """记录实际迭代次数。"""
        trace.input_summary = f"iterations={self._loop_count}"
