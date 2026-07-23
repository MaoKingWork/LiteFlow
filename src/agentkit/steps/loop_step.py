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

import ast
from typing import TYPE_CHECKING, Any

from agentkit.config import get_default
from agentkit.core.template import eval_expression, resolve_value
from agentkit.steps.base import BaseStep, register_step

if TYPE_CHECKING:
    from agentkit.config import RetryPolicy
    from agentkit.core.cancel import CancelToken
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
        output:  聚合目标键名;``collect`` 模式收集为列表,``append``/``last`` 写入该键。
        output_mode: 迭代结果聚合方式:

            - ``collect``(默认):每次 body 产出收集为列表,写入 ``output``。
            - ``append``:每次 body 产出作为增量,累加为字符串写入 ``output``。
            - ``last``:仅保留 body 最后一次产出,不收集。

        separator: ``append`` 模式的拼接分隔符(默认空串)。
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

    用法示例 — 增量累加(YAML)::

        - id: write_chapters
          type: loop
          iter: "[2, 3, 4, 5]"
          as: chapter_num
          output_mode: append
          output: story_markdown
          separator: "\\n\\n"
          step:
            id: write_next
            type: llm
            agent: story_writer
            prompt: "续写第{{chapter_num}}章,只输出新章节正文。"
            output: story_markdown
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
        output_mode: str = "collect",
        separator: str = "",
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
        self.output_mode: str = output_mode
        self.separator: str = separator
        # 运行期 scratch
        self._current_hooks: "LifecycleHooks | None" = None
        self._current_retry_policy: "RetryPolicy | None" = None
        self._current_cancel_token: "CancelToken | None" = None
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

    def bind_blocking_executor(self, executor: "Any") -> None:
        """重写:递归传播到循环体 body,与 bind_mcp_manager 保持一致。"""
        super().bind_blocking_executor(executor)
        if self.body is not None:
            self.body.bind_blocking_executor(executor)

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
        """遍历 ``iter`` 列表,按 ``output_mode`` 聚合结果。

        - ``collect``:每次 body 产出收集为列表,写入 ``output``。
        - ``append``:每次 body 产出作为增量,累加为字符串写入 ``output``。
        - ``last``:仅保留 body 最后一次产出,不收集。
        """
        assert self.body is not None
        assert self.iter_tpl is not None

        # 解析迭代列表
        items = resolve_value(self.iter_tpl, ctx)
        if items is None:
            items = []
        elif isinstance(items, str):
            # 字符串:[...] / (...) 字面量尝试解析为 list;否则视为单元素
            stripped = items.strip()
            if stripped.startswith(("[", "(")) and stripped.endswith(("]", ")")):
                try:
                    parsed = ast.literal_eval(stripped)
                    items = list(parsed) if isinstance(parsed, (list, tuple)) else [items]
                except (ValueError, SyntaxError):
                    items = [items]
            else:
                items = [items]
        elif isinstance(items, (bytes, dict)):
            # bytes / dict 不可迭代为列表,包装为单元素列表
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

        body_out = self.body.output
        out_key = self.output or body_out

        if self.output_mode == "append":
            acc = self._seed_acc(ctx, out_key)
            for item in items:
                if self._cancelled():
                    break
                ctx.set(self.item_var, item)
                await self._exec_body(ctx)
                acc = self._append_delta(ctx, acc, out_key, body_out)
        elif self.output_mode == "last":
            for item in items:
                if self._cancelled():
                    break
                ctx.set(self.item_var, item)
                await self._exec_body(ctx)
            # body.output 自然保留最后一次写入,无需额外处理
        else:  # collect
            collected: list[Any] = []
            for item in items:
                if self._cancelled():
                    break
                ctx.set(self.item_var, item)
                await self._exec_body(ctx)
                # 修复:从 body 实际写入的键读取,而非聚合目标
                if body_out and ctx.has(body_out):
                    collected.append(ctx.get(body_out))
            if out_key:
                ctx.set(out_key, collected)

    # ------------------------------------------------------------------
    # 条件重试模式
    # ------------------------------------------------------------------
    async def _run_until_mode(self, ctx: "Context") -> None:
        """重复执行内部 Step 直到 ``until`` 为真或达到 ``max`` 上限。

        ``output_mode`` 与迭代模式对称:
        ``collect`` 记录每次尝试、``append`` 渐进累加、``last`` 保留最终结果。
        """
        assert self.body is not None
        assert self.until_expr is not None

        max_iter = self._effective_max()
        body_out = self.body.output
        out_key = self.output or body_out

        collected: list[Any] = []
        acc = self._seed_acc(ctx, out_key) if self.output_mode == "append" else ""

        for _ in range(max_iter):
            if self._cancelled():
                break
            await self._exec_body(ctx)

            if self.output_mode == "append":
                acc = self._append_delta(ctx, acc, out_key, body_out)
            elif self.output_mode == "collect":
                if body_out and ctx.has(body_out):
                    collected.append(ctx.get(body_out))

            if eval_expression(self.until_expr, ctx):
                if self.output_mode == "collect" and out_key:
                    ctx.set(out_key, collected)
                return

        # 达到上限
        if self.output_mode == "collect" and out_key:
            ctx.set(out_key, collected)
        if self.on_max == "fail":
            raise LoopMaxReachedError(
                f"LoopStep {self.id!r} 达到最大迭代次数 {max_iter} "
                f"且 until 条件未满足"
            )
        # on_max == "continue":静默继续

    # ------------------------------------------------------------------
    # 辅助:执行 body 单次
    # ------------------------------------------------------------------
    async def _exec_body(self, ctx: "Context") -> None:
        """执行循环体一次,递增计数。"""
        await self.body.execute(
            ctx,
            self._current_hooks,
            retry_policy=self._current_retry_policy,
            cancel_token=self._current_cancel_token,
        )
        self._loop_count += 1

    def _seed_acc(self, ctx: "Context", out_key: str | None) -> str:
        """append 模式:读取累加种子(已有值续写,否则空串起步)。

        无种子时预设空串到 ctx,使 body 可安全引用 ``{{out_key}}``
        (配合 ``{{#if out_key}}`` 区分首章与续写),避免缺失 key 抛 KeyError。
        """
        if out_key and ctx.has(out_key):
            acc = ctx.get(out_key, "")
            return acc if isinstance(acc, str) else str(acc)
        if out_key:
            ctx.set(out_key, "")
        return ""

    def _append_delta(
        self,
        ctx: "Context",
        acc: str,
        out_key: str | None,
        body_out: str | None,
    ) -> str:
        """append 模式:读取 body 增量,累加到 acc 并同步写回 out_key。

        每次迭代后写回 acc,保证同名式(body.output == output)下
        body 下次能读到完整累加值。
        """
        delta = ""
        if body_out and ctx.has(body_out):
            delta = ctx.get(body_out, "") or ""
            if not isinstance(delta, str):
                delta = str(delta)
        if delta:
            acc = f"{acc}{self.separator}{delta}" if acc else delta
        if out_key:
            ctx.set(out_key, acc)
        return acc

    # ------------------------------------------------------------------
    # 辅助:执行编排
    # ------------------------------------------------------------------
    def _cancelled(self) -> bool:
        """检查取消令牌是否已触发(graceful 取消)。"""
        return (
            self._current_cancel_token is not None
            and self._current_cancel_token.is_cancelled
        )

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
        cancel_token: "CancelToken | None" = None,
    ) -> "StepTrace":
        """重写:暂存 hooks / retry_policy / cancel_token,供内部 Step 使用。"""
        self._current_hooks = hooks
        self._current_retry_policy = retry_policy
        self._current_cancel_token = cancel_token
        return await super().execute(
            ctx, hooks, retry_policy=retry_policy, cancel_token=cancel_token
        )

    def _enrich_trace(self, trace: "StepTrace") -> None:
        """记录实际迭代次数。"""
        trace.input_summary = f"iterations={self._loop_count}"
