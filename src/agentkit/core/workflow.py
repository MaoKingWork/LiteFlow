"""core.workflow —— 工作流引擎:Step 编排 / 检查点 / 断点续传 / 钩子。

本模块实现 AgentKit 的 ``Workflow`` 引擎,负责:
    - 按顺序编排 Step 列表,每个 Step 经 ``execute`` 执行(含钩子 / 重试 / trace)。
    - 每完成一个 Step 保存检查点(``CheckpointStore``),失败后可 ``resume``
      跳过已完成 Step 从断点恢复。
    - 触发 ``before_workflow`` / ``after_workflow`` 生命周期钩子。
    - 管理 MCP Server 连接生命周期(``connect_all`` → 执行 → ``close_all``)。

设计原则:
    - 高度模块化:依赖 ``core.context`` / ``core.checkpoint`` / ``core.hooks``
      / ``steps.base``;MCPManager 仅作类型注解(``TYPE_CHECKING``),运行时
      不硬依赖 ``mcp`` 子包,避免循环。
    - 断点续传:每个 Step 成功后保存检查点;``resume`` 从检查点恢复 Context
      并跳过已完成 Step。
    - 类型注解完整,中文 docstring。

公开 API:
    - Workflow:      工作流引擎
    - WorkflowResult: 工作流运行结果
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentkit.config import get_default
from agentkit.core.checkpoint import Checkpoint, CheckpointStore, LocalCheckpointStore
from agentkit.core.context import Context
from agentkit.core.hooks import (
    CompositeHooks,
    LifecycleHooks,
    LoggingHooks,
    TokenAccountingHooks,
)
from agentkit.steps.base import BaseStep

if TYPE_CHECKING:
    from agentkit.llm.base import LLMClient
    from agentkit.mcp.manager import MCPManager

logger = logging.getLogger(__name__)

__all__ = ["Workflow", "WorkflowResult"]


@dataclass
class WorkflowResult:
    """工作流运行结果。

    Attributes:
        run_id:       运行 id。
        context:      最终上下文。
        status:       运行状态:``completed`` | ``failed``。
        completed_steps: 已完成的 Step id 列表。
        error:        失败时的错误信息。
    """

    run_id: str
    context: Context
    status: str = "completed"
    completed_steps: list[str] = field(default_factory=list)
    error: str | None = None


class Workflow:
    """工作流引擎:编排 Step 列表,支持检查点与断点续传。

    Args:
        name:             Workflow 名称(用于检查点 ``workflow_name`` 字段)。
        steps:            Step 列表(按顺序执行)。
        checkpoint_store: 检查点存储;``None`` 时使用 ``LocalCheckpointStore``。
        hooks:            生命周期钩子;``None`` 时由 ``auto_hooks`` 决定是否
                          自动装配默认可观测性 hooks。
        mcp_manager:      MCP 管理器;``None`` 时不管理 MCP 连接。
        llm_client:       注入的 LLM 客户端;绑定到所有 LLMStep / SkillStep,
                          优先于全局默认客户端。配合 ``owns_llm_client`` 可让
                          Workflow 在结束时自动关闭,避免连接泄漏。
        owns_llm_client:  是否由 Workflow 负责 ``llm_client`` 的生命周期关闭。
                          ``True`` 时,``run`` / ``resume`` 结束或退出 ``async with``
                          时自动 ``await client.close()``。``False``(默认)时不关闭
                          (调用方自行管理)。
        auto_hooks:       是否在 ``hooks`` 为 ``None`` 时自动装配默认可观测性
                          hooks(``LoggingHooks`` + ``TokenAccountingHooks``)。
                          受全局配置 ``default_hooks_enabled`` 开关控制。
                          默认 ``True``,使日志与 token 计量开箱即用。

    用法示例::

        wf = Workflow(
            name="daily_report",
            steps=[step1, step2, step3],
            hooks=LoggingHooks(),
        )
        result = await wf.run(inputs={"date": "2024-01-01"})

    LLM 客户端自动管理(避免手动 close)::

        client = OpenAIClient(api_key="...")
        async with Workflow(
            name="report", steps=[...], llm_client=client, owns_llm_client=True
        ) as wf:
            result = await wf.run(inputs={...})
        # 退出 async with 后 client 已自动关闭

    断点续传::

        # 上次运行失败后,用相同 run_id 恢复
        result = await wf.resume("run_abc123")
    """

    def __init__(
        self,
        name: str = "",
        steps: list[BaseStep] | None = None,
        checkpoint_store: CheckpointStore | None = None,
        hooks: LifecycleHooks | None = None,
        mcp_manager: "MCPManager | None" = None,
        llm_client: "LLMClient | None" = None,
        owns_llm_client: bool = False,
        auto_hooks: bool = True,
    ) -> None:
        self.name: str = name
        self.steps: list[BaseStep] = steps or []
        self.checkpoint_store: CheckpointStore = (
            checkpoint_store or LocalCheckpointStore()
        )
        # auto_hooks:仅在未显式传入 hooks 时生效,避免覆盖用户自定义 hooks。
        if hooks is None and auto_hooks and get_default("default_hooks_enabled"):
            hooks = CompositeHooks([LoggingHooks(), TokenAccountingHooks()])
        self.hooks: LifecycleHooks | None = hooks
        self.mcp_manager: "MCPManager | None" = mcp_manager
        self.llm_client: "LLMClient | None" = llm_client
        self.owns_llm_client: bool = owns_llm_client
        # 客户端是否已关闭(防止 run finally 与 async with 退出重复关闭)
        self._llm_closed: bool = False

    # ------------------------------------------------------------------
    # run —— 全新执行
    # ------------------------------------------------------------------
    async def run(
        self,
        inputs: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> WorkflowResult:
        """从起点执行工作流。

        创建新检查点,设置输入,顺序执行所有 Step,每步成功后保存检查点。
        失败时保存失败状态检查点并返回 ``status="failed"``。

        Args:
            inputs: 输入变量 dict,每个 key-value 通过 ``ctx.set`` 写入上下文。
            run_id: 自定义运行 id;``None`` 时自动生成。

        Returns:
            WorkflowResult: 运行结果。
        """
        # 创建检查点
        checkpoint = Checkpoint.new(self.name, run_id)

        # 创建上下文并写入输入
        ctx = Context()
        if inputs:
            for key, value in inputs.items():
                ctx.set(key, value)

        return await self._execute(ctx, checkpoint)

    # ------------------------------------------------------------------
    # resume —— 断点续传
    # ------------------------------------------------------------------
    async def resume(self, run_id: str) -> WorkflowResult:
        """从检查点恢复执行。

        加载检查点,恢复上下文,跳过已完成 Step,从失败处重新执行。

        Args:
            run_id: 要恢复的运行 id。

        Returns:
            WorkflowResult: 运行结果。

        Raises:
            KeyError: 检查点不存在时。
        """
        checkpoint = await self.checkpoint_store.load(run_id)
        if checkpoint is None:
            raise KeyError(f"检查点 {run_id!r} 不存在,无法恢复")

        # 恢复上下文
        ctx = Context.restore(checkpoint.context_snapshot)
        # 重置状态为 running
        checkpoint.status = "running"
        checkpoint.error = None

        return await self._execute(ctx, checkpoint)

    # ------------------------------------------------------------------
    # _execute —— 内部执行核心
    # ------------------------------------------------------------------
    async def _execute(
        self, ctx: Context, checkpoint: Checkpoint
    ) -> WorkflowResult:
        """执行工作流核心逻辑。

        管理 MCP 连接生命周期,触发钩子,顺序执行 Step(跳过已完成的),
        每步保存检查点。
        """
        run_id = checkpoint.run_id
        completed_set = set(checkpoint.completed_steps)

        # before_workflow 钩子
        if self.hooks:
            await self.hooks.before_workflow(self, ctx)

        # 保存初始检查点
        checkpoint.updated_at = time.time()
        await self.checkpoint_store.save(checkpoint)

        # MCP 连接
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.connect_all()
            except Exception as exc:
                logger.warning("MCP 连接失败(继续执行): %r", exc)
            # 连接后把 mcp_manager 绑定到所有 Step,使 LLMStep / SkillStep 能
            # 据此把 MCP 工具名注入 agent.tools,补齐「注册表有、schema 无」缺口。
            # 容器型 Step(Loop/Parallel/Condition)会递归传播给嵌套子 Step。
            for step in self.steps:
                step.bind_mcp_manager(self.mcp_manager)

        # 绑定注入的 LLM 客户端到所有 Step(优先于全局默认客户端)。
        # 容器型 Step 同样会递归传播。None 表示用全局默认,不覆盖。
        if self.llm_client is not None:
            for step in self.steps:
                step.bind_llm_client(self.llm_client)

        try:
            # 顺序执行 Step
            for step in self.steps:
                step_id = step.id or step.type

                # 跳过已完成的 Step
                if step_id in completed_set:
                    logger.debug("跳过已完成 Step: %s", step_id)
                    continue

                # 执行 Step
                try:
                    await step.execute(ctx, self.hooks)
                except Exception as exc:
                    # Step 失败:保存检查点并返回失败结果
                    checkpoint.status = "failed"
                    checkpoint.error = f"{type(exc).__name__}: {exc}"
                    checkpoint.context_snapshot = ctx.snapshot()
                    checkpoint.updated_at = time.time()
                    await self.checkpoint_store.save(checkpoint)

                    if self.hooks:
                        await self.hooks.after_workflow(self, ctx, ctx)

                    return WorkflowResult(
                        run_id=run_id,
                        context=ctx,
                        status="failed",
                        completed_steps=list(checkpoint.completed_steps),
                        error=checkpoint.error,
                    )

                # Step 成功:记录并保存检查点
                checkpoint.completed_steps.append(step_id)
                completed_set.add(step_id)
                checkpoint.context_snapshot = ctx.snapshot()
                checkpoint.updated_at = time.time()
                await self.checkpoint_store.save(checkpoint)

            # 全部完成
            checkpoint.status = "completed"
            checkpoint.error = None
            checkpoint.updated_at = time.time()
            await self.checkpoint_store.save(checkpoint)

            return WorkflowResult(
                run_id=run_id,
                context=ctx,
                status="completed",
                completed_steps=list(checkpoint.completed_steps),
            )
        finally:
            # 资源清理:MCP 断开 + (可选)LLM 客户端关闭
            await self._cleanup_resources()

            # after_workflow 钩子
            if self.hooks:
                await self.hooks.after_workflow(self, ctx, ctx)

    # ------------------------------------------------------------------
    # 资源清理 / async 上下文管理
    # ------------------------------------------------------------------
    async def _cleanup_resources(self) -> None:
        """统一释放 Workflow 持有的运行期资源。

        - MCP:每次 ``run`` / ``resume`` 结束都断开(与既有语义一致)。
        - LLM 客户端:仅当 ``owns_llm_client`` 为真且尚未关闭时关闭,并用
          ``_llm_closed`` 标志保证幂等(避免 run finally 与 ``async with``
          退出重复关闭)。

        任何资源关闭异常都被捕获并降级为 warning,不影响工作流返回。
        """
        # MCP 断开(每次执行结束)
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.close_all()
            except Exception as exc:
                logger.warning("MCP 关闭异常(已忽略): %r", exc)

        # LLM 客户端关闭(仅 owns 且未关)
        if (
            self.owns_llm_client
            and self.llm_client is not None
            and not self._llm_closed
        ):
            try:
                await self.llm_client.close()
            except Exception as exc:
                logger.warning("LLM 客户端关闭异常(已忽略): %r", exc)
            finally:
                self._llm_closed = True

    async def close(self) -> None:
        """显式释放 Workflow 持有的资源(幂等)。

        适用于不使用 ``async with`` 的场景:在 ``run`` / ``resume`` 之后手动调用,
        确保 ``owns_llm_client`` 的客户端被关闭。多次调用安全。
        """
        await self._cleanup_resources()

    async def __aenter__(self) -> "Workflow":
        """进入异步上下文,返回自身。

        资源清理委托给 :meth:`__aexit__`,确保即便 ``run`` 抛出未捕获异常,
        LLM 客户端与 MCP 连接也能被释放。
        """
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """退出异步上下文,释放资源(幂等,不吞掉异常)。"""
        await self._cleanup_resources()

    # ------------------------------------------------------------------
    # dry_run —— 静态分析 / 执行计划
    # ------------------------------------------------------------------
    def dry_run(self, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        """生成执行计划(不实际执行)。

        遍历 Step 列表,记录每个 Step 的类型 / id / output,用于调试与验证。
        不调用 LLM / Tool / MCP,不产生副作用。

        Args:
            inputs: 输入变量(仅记录,不执行)。

        Returns:
            dict: 执行计划,含 ``workflow_name`` / ``inputs`` / ``steps`` /
            ``total_steps``。
        """
        plan_steps: list[dict[str, Any]] = []
        for step in self.steps:
            plan_steps.append(
                {
                    "id": step.id or step.type,
                    "type": step.type,
                    "output": step.output,
                }
            )
        return {
            "workflow_name": self.name,
            "inputs": list(inputs.keys()) if inputs else [],
            "steps": plan_steps,
            "total_steps": len(self.steps),
        }
