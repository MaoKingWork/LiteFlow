"""steps.skill_step —— SkillStep:调用已注册 Skill 执行 LLM 任务。

本模块实现 AgentKit 的 Skill 调用 Step:按名从 ``SkillRegistry`` 取出
``SkillManifest``,以其系统提示词 / 输出契约 / 工具集构造 ``AgentConfig``,
再委托 :class:`~agentkit.steps.llm_step.LLMStep` 执行。

设计要点:
    - Skill 是可复用的能力封装;SkillStep 使 Workflow 能以声明式方式调用。
    - Skill 的 ``system_prompt`` / ``output_model`` / ``tools`` 直接映射到
      ``AgentConfig`` 对应字段。
    - ``prompt_injection_append`` 追加到系统提示词末尾。
    - Skill 的 MCP 依赖(``requires_mcp``)由 Workflow 层确保已连接。
    - 字段命名与 :class:`~agentkit.steps.llm_step.LLMStep` 统一为 ``prompt``;
      ``input`` 作为向后兼容的别名保留(触发 ``DeprecationWarning``)。

设计原则:
    - 高度模块化:依赖 ``steps.base`` / ``steps.llm_step`` / ``skill.registry``
      / ``core.agent``。
    - 委托模式:不重复 LLM 调用逻辑,构造 AgentConfig 后委托 LLMStep。
    - 类型注解完整,中文 docstring。

公开 API:
    - SkillStep: Skill 调用 Step
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from agentkit.core.agent import AgentConfig
from agentkit.skill.registry import get_skill
from agentkit.steps.base import BaseStep, register_step
from agentkit.steps.llm_step import LLMStep

if TYPE_CHECKING:
    from agentkit.config import RetryPolicy
    from agentkit.core.context import Context
    from agentkit.core.hooks import LifecycleHooks
    from agentkit.llm.base import LLMClient
    from agentkit.steps.base import StepTrace

__all__ = ["SkillStep"]


@register_step("skill")
class SkillStep(BaseStep):
    """Skill 调用 Step:按名加载 Skill 并以 LLMStep 执行。

    从 ``SkillRegistry`` 取出 ``SkillManifest``,以其配置构造 ``AgentConfig``,
    委托 ``LLMStep`` 完成 LLM 调用、Function Call 循环与输出契约保障。

    Args:
        id:      Step 实例标识。
        skill:   Skill 注册名(如 ``"web_search"``)。
        prompt:  user 提示词模板,支持 ``{{var}}`` / ``${ENV}``。
        output:  输出键名。
        model:   模型覆盖(可选);不指定时用 AgentConfig 默认。
        retry:   实例级重试策略。
        timeout: 实例级超时秒数。
        input:   **已废弃**,等价于 ``prompt``。为向后兼容保留,传入时触发
                 :class:`DeprecationWarning`。``prompt`` 与 ``input`` 不可同时指定。

    用法示例(YAML)::

        - id: search_and_summarize
          type: skill
          skill: web_search
          prompt: "{{user_query}}"
          output: search_result
    """

    type = "skill"

    def __init__(
        self,
        id: str = "",
        skill: str = "",
        prompt: str | None = None,
        output: str | None = None,
        model: str | None = None,
        retry: "RetryPolicy | None" = None,
        timeout: float | None = None,
        *,
        input: str | None = None,
    ) -> None:
        super().__init__(id=id, output=output, retry=retry, timeout=timeout)
        # prompt / input 互斥:同时指定视为配置错误,避免歧义
        if prompt is not None and input is not None:
            raise ValueError(
                f"SkillStep {id!r} 的 prompt 与 input 不可同时指定(input 已废弃,请用 prompt)"
            )
        if input is not None:
            warnings.warn(
                f"SkillStep {id!r} 的 `input` 参数已废弃,请改用 `prompt`",
                DeprecationWarning,
                stacklevel=2,
            )
            prompt = input
        self.skill_name: str = skill
        # 命名与 LLMStep.prompt 对齐,消除 input/prompt 二义性
        self.prompt: str | None = prompt
        self.model_override: str | None = model
        # 运行期 scratch
        self._current_hooks: "LifecycleHooks | None" = None
        self._current_retry_policy: "RetryPolicy | None" = None
        self._skill_used: str = ""

    async def run(self, ctx: "Context") -> "Context":
        """加载 Skill 配置,构造 LLMStep 并委托执行。"""
        # 取 SkillManifest
        manifest = get_skill(self.skill_name)
        self._skill_used = manifest.name

        # 构造系统提示词(追加 injection)
        system = manifest.system_prompt
        if manifest.prompt_injection_append:
            system = f"{system}\n\n{manifest.prompt_injection_append}"

        # 构造 AgentConfig
        agent_config = AgentConfig(
            name=manifest.name,
            model=self.model_override or "gpt-4o-mini",
            system=system,
            output_model=manifest.output_model,
            tools=list(manifest.tools),
            mcp=list(manifest.requires_mcp),
            max_tool_iterations=manifest.max_tool_iterations,
        )

        # 委托 LLMStep 执行(prompt / mcp_manager / llm_client 一并透传)
        llm_step = LLMStep(
            id=self.id or f"skill_{manifest.name}",
            agent=agent_config,
            prompt=self.prompt,
            output=self.output,
            retry=self.retry,
            timeout=self.timeout,
            llm_client=self.llm_client,
        )
        # 把 Workflow 注入的 mcp_manager 传递给内部 LLMStep,使其能注入 MCP 工具
        llm_step.mcp_manager = self.mcp_manager
        await llm_step.execute(
            ctx,
            self._current_hooks,
            retry_policy=self._current_retry_policy,
        )
        return ctx

    async def execute(
        self,
        ctx: "Context",
        hooks: "LifecycleHooks | None" = None,
        *,
        retry_policy: "RetryPolicy | None" = None,
    ) -> "StepTrace":
        """重写:暂存 hooks 与 retry_policy,供 LLMStep ``execute`` 使用。"""
        self._current_hooks = hooks
        self._current_retry_policy = retry_policy
        return await super().execute(ctx, hooks, retry_policy=retry_policy)

    def _enrich_trace(self, trace: "StepTrace") -> None:
        """记录使用的 Skill 名。"""
        if self._skill_used:
            trace.input_summary = f"skill={self._skill_used}"
