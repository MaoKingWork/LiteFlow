"""steps.parallel_step —— ParallelStep:并行执行多个分支。

本模块实现 AgentKit 的并行 Step,通过 ``asyncio.Semaphore`` 控制最大并发数,
支持两种错误策略:

1. **fail_fast**:首个分支出错即取消所有未完成的分支(``asyncio.gather``
   默认行为)。
2. **collect_all**:等待所有分支完成,收集错误后统一 raise。

设计要点:
    - ``asyncio.Semaphore(max_concurrency)`` 限制并发数。
    - 整体 ``timeout`` 超时后取消所有分支。
    - branches 的 ``output`` key 不得重复(构造时校验)。
    - ``on_error`` 默认 ``fail_fast``。

设计原则:
    - 高度模块化:仅依赖 ``steps.base`` / ``config`` / ``asyncio``。
    - 安全:timeout + max_concurrency 双重保护。
    - 类型注解完整,中文 docstring。

公开 API:
    - ParallelStep: 并行 Step
    - ParallelError: collect_all 模式下多个分支失败时抛出的聚合异常
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agentkit.config import get_default
from agentkit.steps.base import BaseStep, register_step

if TYPE_CHECKING:
    from agentkit.config import RetryPolicy
    from agentkit.core.cancel import CancelToken
    from agentkit.core.context import Context
    from agentkit.core.hooks import LifecycleHooks
    from agentkit.mcp.manager import MCPManager
    from agentkit.steps.base import StepTrace

__all__ = ["ParallelStep", "ParallelError"]


class ParallelError(Exception):
    """``collect_all`` 模式下多个分支失败时抛出的聚合异常。

    Attributes:
        errors: ``(branch_id, exception)`` 列表。
    """

    def __init__(self, errors: list[tuple[str, BaseException]]) -> None:
        self.errors = errors
        summary = "; ".join(
            f"{bid}: {type(e).__name__}: {e}" for bid, e in errors
        )
        super().__init__(f"ParallelStep 有 {len(errors)} 个分支失败: {summary}")


@register_step("parallel")
class ParallelStep(BaseStep):
    """并行执行多个分支,受 ``max_concurrency`` 与 ``timeout`` 约束。

    所有分支完成后,各分支的 ``output`` 在 Context 中可直接被后续 Step 读取。
    若指定了 ``output``,则把所有分支输出汇总为 dict 写入该键。

    Args:
        id:              Step 实例标识。
        branches:        并行执行的子步骤列表。
        max_concurrency: 最大并发数;``None`` 用全局默认(5)。
        timeout:         整体超时秒数;``None`` 用全局默认(60)。
        on_error:        错误策略:``fail_fast`` | ``collect_all``。
        output:          汇总输出键名(可选);指定时把各分支 output 收为 dict。
        retry:           实例级重试策略。
        timeout_step:    单分支超时(可选,一般不用,用整体 timeout 即可)。

    用法示例(YAML)::

        - id: fan_out
          type: parallel
          max_concurrency: 3
          on_error: collect_all
          branches:
            - id: fetch_a
              type: tool
              tool: db.query
              params: { sql: "SELECT * FROM a" }
              output: result_a
            - id: fetch_b
              type: tool
              tool: http.get
              params: { url: "https://api.example.com/b" }
              output: result_b
    """

    type = "parallel"

    def __init__(
        self,
        id: str = "",
        branches: list[BaseStep] | None = None,
        max_concurrency: int | None = None,
        timeout: float | None = None,
        on_error: str = "fail_fast",
        output: str | None = None,
        retry: "RetryPolicy | None" = None,
        timeout_step: float | None = None,
    ) -> None:
        super().__init__(id=id, output=output, retry=retry, timeout=timeout)
        self.branches: list[BaseStep] = branches or []
        self.max_concurrency: int | None = max_concurrency
        self.timeout_seconds: float | None = timeout
        self.on_error: str = on_error

        # 校验:branches 的 output key 不得重复
        output_keys = [b.output for b in self.branches if b.output]
        if len(output_keys) != len(set(output_keys)):
            dupes = [k for k in output_keys if output_keys.count(k) > 1]
            raise ValueError(
                f"ParallelStep {id!r} 的 branches output key 重复: "
                f"{set(dupes)}"
            )

        # 运行期 scratch
        self._current_hooks: "LifecycleHooks | None" = None
        self._current_retry_policy: "RetryPolicy | None" = None
        self._current_cancel_token: "CancelToken | None" = None
        self._branch_statuses: list[dict[str, Any]] = []

    def bind_mcp_manager(self, manager: "MCPManager | None") -> None:
        """重写:递归传播到所有分支,确保分支内 LLMStep 也能拿到 MCP 工具。"""
        super().bind_mcp_manager(manager)
        for b in self.branches:
            b.bind_mcp_manager(manager)

    def bind_llm_client(self, client: "LLMClient | None") -> None:
        """重写:递归传播到所有分支,与 bind_mcp_manager 保持一致。"""
        super().bind_llm_client(client)
        for b in self.branches:
            b.bind_llm_client(client)

    def bind_blocking_executor(self, executor: "Any") -> None:
        """重写:递归传播到所有分支,与 bind_mcp_manager 保持一致。"""
        super().bind_blocking_executor(executor)
        for b in self.branches:
            b.bind_blocking_executor(executor)

    async def run(self, ctx: "Context") -> "Context":
        """并发执行所有分支。"""
        self._branch_statuses = []
        if not self.branches:
            return ctx

        max_conc = self._effective_max_concurrency()
        timeout = self._effective_timeout()
        sem = asyncio.Semaphore(max_conc)

        async def _run_branch(branch: BaseStep) -> None:
            async with sem:
                await branch.execute(
                    ctx,
                    self._current_hooks,
                    retry_policy=self._current_retry_policy,
                    cancel_token=self._current_cancel_token,
                )

        if self.on_error == "fail_fast":
            await self._run_fail_fast(_run_branch, timeout)
        else:
            await self._run_collect_all(_run_branch, timeout)

        # 可选:汇总各分支 output 为 dict
        if self.output:
            merged: dict[str, Any] = {}
            for b in self.branches:
                if b.output and ctx.has(b.output):
                    merged[b.output] = ctx.get(b.output)
            ctx.set(self.output, merged)
        return ctx

    # ------------------------------------------------------------------
    # 错误策略实现
    # ------------------------------------------------------------------
    async def _run_fail_fast(self, runner, timeout: float) -> None:
        """fail_fast:首个错误即取消所有。"""
        try:
            await asyncio.wait_for(
                asyncio.gather(*(runner(b) for b in self.branches)),
                timeout=timeout,
            )
        except TimeoutError:
            raise TimeoutError(
                f"ParallelStep {self.id!r} 整体超时({timeout}s)"
            )

    async def _run_collect_all(self, runner, timeout: float) -> None:
        """collect_all:全部等待,收集错误后统一 raise。"""
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(runner(b) for b in self.branches),
                    return_exceptions=True,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            raise TimeoutError(
                f"ParallelStep {self.id!r} 整体超时({timeout}s)"
            )

        errors: list[tuple[str, BaseException]] = []
        for branch, result in zip(self.branches, results):
            if isinstance(result, BaseException):
                errors.append((branch.id or branch.type, result))
        if errors:
            raise ParallelError(errors)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _effective_max_concurrency(self) -> int:
        if self.max_concurrency is not None:
            return self.max_concurrency
        return int(get_default("default_max_concurrency"))

    def _effective_timeout(self) -> float:
        if self.timeout_seconds is not None:
            return self.timeout_seconds
        return float(get_default("default_parallel_timeout_seconds"))

    async def execute(
        self,
        ctx: "Context",
        hooks: "LifecycleHooks | None" = None,
        *,
        retry_policy: "RetryPolicy | None" = None,
        cancel_token: "CancelToken | None" = None,
    ) -> "StepTrace":
        """重写:暂存 hooks / retry_policy / cancel_token,供分支 ``execute`` 使用。"""
        self._current_hooks = hooks
        self._current_retry_policy = retry_policy
        self._current_cancel_token = cancel_token
        return await super().execute(
            ctx, hooks, retry_policy=retry_policy, cancel_token=cancel_token
        )

    def _enrich_trace(self, trace: "StepTrace") -> None:
        """记录分支执行状态。"""
        trace.input_summary = f"branches={len(self.branches)}"
