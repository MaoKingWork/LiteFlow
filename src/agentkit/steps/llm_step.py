"""steps.llm_step —— LLMStep:Function Call 循环 + 输出契约保障链。

本模块实现 AgentKit 中最核心、最复杂的 Step 类型 :class:`LLMStep`。它把一个
``AgentConfig`` 配置转化为一次完整的 LLM 调用流程,包含两大引擎:

1. **Function Call 循环**(:meth:`_run_function_call_loop`):
       LLM 返回纯文本 -> 结束;返回 tool_calls -> 框架自动执行对应 Tool,
       把结果以 ``role=tool`` 消息追加到对话历史,再次调用 LLM,如此往复,
       直到 LLM 给出最终文本或达到 ``max_tool_iterations`` 上限(上限到达后
       强制发起一次不带 tools 的调用以索取最终答案,防死循环)。

2. **输出契约保障链**(:meth:`_run_output_contract`):
       LLM 最终文本 -> ``PydanticParser`` 解析 -> 业务 ``output_validator`` 校验
       -> 失败则附加修复提示(retry_hint)重试(共 ``agent.retry.count`` 次)
       -> 重试耗尽降级到 ``fallback_model`` 再试一次
       -> 仍失败执行 ``on_exhausted`` 策略(raise / default / skip)。

设计原则:
    - 高度模块化:仅依赖 ``steps.base`` / ``core.agent`` / ``core.template``
      / ``skill.merger`` / ``parsers.pydantic_parser`` / ``tools.base`` /
      ``llm.base``;``get_default_client`` 延迟到 ``run`` 内导入,避免模块加载期
      触发 ``llm.openai`` 的 httpx 依赖,使无 httpx 环境也能注入 MockClient 运行。
    - 可观测:通过重写 :meth:`_enrich_trace` 把 token 用量与工具调用链回填到
      :class:`StepTrace`,无需 ``run`` 直接接触 ``execute`` 私有的 trace。
    - 可拓展:Agent 配置、LLM 客户端、解析器均通过注入获得,便于测试与替换。
    - 类型注解完整,中文 docstring 与注释。

公开 API:
    - OutputContractError: 输出契约链耗尽且策略为 raise 时抛出
    - LLMStep:           LLM Step 实现
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any, Literal

from agentkit.core.agent import AgentConfig, ExhaustedPolicy, instantiate_agent
from agentkit.core.ports import PortBindingError
from agentkit.core.template import resolve_template, resolve_value
from agentkit.llm.base import (
    LLMClient,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    ToolCall,
)
from agentkit.parsers.base import Parser
from agentkit.parsers.json_parser import JSONParser
from agentkit.parsers.pydantic_parser import PydanticParser
from agentkit.parsers.text_parser import TextParser
from agentkit.skill.merger import apply_skills_to_agent
from agentkit.steps.base import BaseStep, StepTrace, register_step
from agentkit.tools.base import get_tool

if TYPE_CHECKING:
    from agentkit.config import RetryPolicy
    from agentkit.core.context import Context


__all__ = ["OutputContractError", "LLMStep"]


# ---------------------------------------------------------------------------
# OutputContractError —— 输出契约链耗尽异常
# ---------------------------------------------------------------------------
class OutputContractError(Exception):
    """输出契约保障链全部耗尽(解析重试 + 降级模型)后仍失败时抛出。

    由 :meth:`LLMStep.run` 在 ``on_exhausted='raise'`` 时抛出;``execute`` 的
    执行级重试可捕获并按 ``RetryPolicy`` 处理(通常这类错误重试也难恢复,
    但保留给上层钩子 ``on_step_error`` 决策的机会)。
    """


# ---------------------------------------------------------------------------
# _resolve_content_part —— 递归解析 content part 中的模板变量
# ---------------------------------------------------------------------------
def _resolve_content_part(part: dict, resolver) -> dict:
    """递归解析 content part dict 中的 ``{{var}}`` / ``${ENV}`` 模板变量。

    遍历 dict 的所有字符串值(含嵌套 dict),用 ``resolver`` 解析。
    非字符串值原样保留。``resolver`` 通常为 ``step._render``，叠加端口作用域。

    Args:
        part:     OpenAI content part dict(如 ``{"type": "image_url", ...}``)。
        resolver: 模板解析回调，签名 ``(str) -> Any``。

    Returns:
        dict: 解析后的新 dict(不修改原 dict)。
    """
    resolved: dict[str, Any] = {}
    for key, value in part.items():
        if isinstance(value, str):
            resolved[key] = resolver(value)
        elif isinstance(value, dict):
            resolved[key] = _resolve_content_part(value, resolver)
        else:
            resolved[key] = value
    return resolved


# ---------------------------------------------------------------------------
# LLMStep
# ---------------------------------------------------------------------------
@register_step("llm")
class LLMStep(BaseStep):
    """LLM 调用 Step:Function Call 循环 + 输出契约保障链。

    把一个 Agent 配置(提示词 / 模型 / 输出契约 / 工具集 / Skill)转化为一次
    完整的 LLM 调用流程,最终把解析后的结构化结果按 ``output`` 写入 Context。

    Args:
        id:          Step 实例标识(用于 trace / 日志)。
        agent:       Agent 引用。可为:
                       - ``str``:Agent 注册名(按名实例化并自动合并其 skills);
                       - ``AgentConfig`` 实例:直接使用(调用方负责 skills 合并);
                       - ``dict``:形如 ``{"name": "...", "skills": [...]}``,
                         ``skills`` 可选,缺省时用 agent 自带 skills。
        prompt:      user 提示词模板,支持 ``{{var}}`` / ``${ENV}``。
                     纯文本场景为 ``str``;多模态场景为 ``list[dict]``,
                     每个 dict 是 OpenAI content part(如
                     ``{"type": "image_url", "image_url": {"url": ...}}``),
                     按 list 顺序组装为 user 消息的 content。
        output:      输出键名;解析结果通过 ``ctx.set(output, value)`` 写入。
        output_format: 输出解析格式,决定无 ``output_model`` 时如何解析 LLM 文本:
                       - ``"text"``(默认):纯文本,直接 ``strip`` 后存为 str;
                       - ``"json"``:``json.loads`` 为 dict/list/标量后存入。
                       ``agent.output_model`` 非 None 时此项被忽略(走 schema 校验)。
        stream:      是否流式输出。``True`` 时通过 ``on_llm_stream_*`` 钩子
                     实时推送文本增量(供前端 SSE / 终端打印);``False``(默认)
                     走非流式 ``chat``。流式不改变契约链语义:解析 / 重试 /
                     降级模型仍按完整文本执行,retry/降级时 hook 携带递增的
                     ``attempt`` 参数,前端据此重置缓冲。
        llm_client:  LLM 客户端注入(测试用 MockClient);为 None 时回落到
                     ``agentkit.llm.get_default_client()``。
        retry:       实例级执行重试策略(覆盖 ``execute`` 的 retry_policy)。
        timeout:     实例级超时秒数。
        system_override:  可选,覆盖 agent.system 的系统提示词模板。
        temperature_override: 可选,覆盖 agent.temperature。

    用法示例(Python API)::

        step = LLMStep(
            id="compress",
            agent="data_compressor",
            prompt="请压缩以下订单:\\n{{order}}",
            output="compressed",
            output_format="json",
        )
        trace = await step.execute(ctx, hooks, retry_policy=policy)

    用法示例(YAML)::

        - id: compress
          type: llm
          agent: data_compressor
          prompt: "请压缩: {{order}}"
          output: compressed
          output_format: json
        # 流式输出(配合自定义 Hook 消费 delta)
        - id: chat
          type: llm
          agent: assistant
          prompt: "{{question}}"
          output: answer
          stream: true
    """

    type = "llm"

    def __init__(
        self,
        id: str = "",
        agent: str | AgentConfig | dict | None = None,
        prompt: str | list[dict] = "",
        output: str | None = None,
        output_format: Literal["text", "json"] = "text",
        stream: bool = False,
        llm_client: LLMClient | None = None,
        retry: "RetryPolicy | None" = None,
        timeout: float | None = None,
        system_override: str | None = None,
        temperature_override: float | None = None,
        *,
        inputs: list | None = None,
        outputs: list | None = None,
        strict_scope: bool = False,
        # 会话缓存参数(T3.1:均关键字参数,向后兼容)
        conversation_mode: str | None = None,
        conversation_key: str | None = None,
        conversation_from: str | None = None,
        conversation_fork_at: str | int = "last",
        conversation_compat: str = "strict",
    ) -> None:
        super().__init__(
            id=id, output=output, retry=retry, timeout=timeout,
            inputs=inputs, outputs=outputs, strict_scope=strict_scope,
        )
        # agent 引用:运行期在 _resolve_agent 中解析为 AgentConfig
        self.agent_ref: str | AgentConfig | dict | None = agent
        self.prompt: str | list[dict] = prompt
        self.output_format: Literal["text", "json"] = output_format
        self.stream: bool = stream
        self.llm_client: LLMClient | None = llm_client
        self.system_override: str | None = system_override
        self.temperature_override: float | None = temperature_override

        # 会话缓存配置(T3.1)
        self.conversation_mode: str | None = conversation_mode
        self.conversation_key: str | None = conversation_key
        self.conversation_from: str | None = conversation_from
        self.conversation_fork_at: str | int = conversation_fork_at
        self.conversation_compat: str = conversation_compat

        # 运行期 scratch:供 _enrich_trace 回填 trace。在 run() 起始重置,
        # 支持 Step 实例被顺序复用(LoopStep 等场景)。
        self._token_usage_total: int = 0
        self._tool_calls_record: list[dict] = []
        self._last_input_summary: str = ""
        # 会话缓存命中观测:累计 resp.usage.cached_tokens(T7)
        self._cached_tokens_total: int = 0
        # 最近一次 LLM 输出内容(供会话保存时追加 assistant 消息)
        self._last_llm_content: str = ""

    # ------------------------------------------------------------------
    # run —— 子类核心逻辑(execute 负责钩子 / 超时 / 重试 / trace)
    # ------------------------------------------------------------------
    async def run(self, ctx: "Context") -> "Context":
        """执行 LLM 调用全流程并写入 output。

        流程:
            1. 重置 trace scratch。
            2. 解析 Agent 配置(实例化 / 合并 skills / 填默认值 / 应用覆盖)。
            3. 解析 prompt / system 模板 -> user 消息;解析 system -> system 消息。
               (conversation_mode 非 None 时按 start/continue/fork 加载会话)
            4. 从 agent.tools 构建 Function Call 工具 schema。
            5. 取 LLM 客户端(注入优先,否则全局默认,均无则抛 RuntimeError)。
            6. Function Call 循环 -> 最终文本 content。
            7. 输出契约保障链 -> (value, error)。
            8. error 非 None 时按 ``on_exhausted`` 决策(raise/default/skip)。
            9. ``ctx.set(self.output, value)`` 写入结果。
            10. conversation_mode 非 None 时 save_conversation 保存会话。
        """
        # 1. 重置 scratch(支持实例顺序复用)
        self._token_usage_total = 0
        self._tool_calls_record = []
        self._last_input_summary = ""
        self._cached_tokens_total = 0
        self._last_llm_content = ""

        # 2. 解析 Agent 配置
        agent = self._resolve_agent()

        # 3. 解析 prompt / system 模板并组装 messages(含会话加载)
        messages = self._build_messages_with_conversation(agent, ctx)

        # 4. 构建工具 schema
        tools_schema = self._build_tools_schema(agent)

        # 5. 取 LLM 客户端(按 provider/model 路由)
        client = self._get_client(agent)

        # 6. Function Call 循环
        content = await self._run_function_call_loop(
            messages, tools_schema, agent, client, ctx
        )
        # 标记契约链前的消息长度(会话保存时排除契约重试追加的消息)
        fc_end = len(messages)

        # 7. 输出契约保障链
        value, error = await self._run_output_contract(
            content, messages, agent, client, ctx
        )

        # 8. 契约链失败 -> on_exhausted 决策
        if error is not None:
            value = self._apply_on_exhausted(agent, error)

        # 9. 写入输出端口：多输出拆分，单输出整体写入
        if len(self.outputs) > 1:
            if not isinstance(value, dict):
                raise PortBindingError(
                    f"LLMStep {self.id!r} 声明了 {len(self.outputs)} 个输出端口，"
                    f"但解析结果非 dict(实际 {type(value).__name__})，无法拆分"
                )
            self._emit_dict_outputs(ctx, value)
        elif self.output:
            ctx.set(self.output, value)

        # 10. 保存会话(T3.6:mode=None 时跳过,零改动)
        if self.conversation_mode is not None:
            from agentkit.core.conversation import save_conversation
            # 保存内容:FC 循环产生的消息 + 最终 assistant 回复;
            # 契约重试追加的 [assistant(坏输出), user(修复提示)] 不保存
            # (对齐设计文档 §7.5 / §8.6)。
            save_messages = messages[:fc_end] + [
                LLMMessage(role="assistant", content=self._last_llm_content)
            ]
            save_conversation(
                ctx, self._conv_resolved_key, save_messages, agent,
                cached_tokens_total=self._cached_tokens_total,
            )

        return ctx

    # ------------------------------------------------------------------
    # Agent 配置解析
    # ------------------------------------------------------------------
    def _resolve_agent(self) -> AgentConfig:
        """把 ``self.agent_ref`` 解析为填充默认值后的 :class:`AgentConfig`。

        解析规则:
            - ``AgentConfig`` 实例:直接使用,不自动合并 skills(调用方负责,
              避免对已合并的实例重复合并导致 system prompt 重复拼接)。
            - ``str``:``instantiate_agent`` 按名实例化,再 ``apply_skills_to_agent``
              合并其自带的 skills。
            - ``dict``:取 ``name`` 实例化;``skills`` 字段存在则用它合并,
              否则用 agent 自带 skills 合并。
            - ``None`` / 其他类型:抛 ``ValueError`` / ``TypeError``。

        最后统一 ``resolve_defaults()`` 填充 ``max_tool_iterations`` 等默认值,
        并应用 ``temperature_override``。
        """
        ref = self.agent_ref

        if isinstance(ref, AgentConfig):
            agent = ref
            # 不自动 apply_skills_to_agent:调用方负责,避免重复合并。
        elif isinstance(ref, str):
            agent = instantiate_agent(ref)
            agent = apply_skills_to_agent(agent)  # 合并 agent.skills
        elif isinstance(ref, dict):
            name = ref.get("name")
            if not name:
                raise ValueError(
                    f"LLMStep {self.id!r} 的 agent 配置为 dict 时必须包含 'name'"
                )
            agent = instantiate_agent(name)
            skills = ref.get("skills")
            if skills is not None:
                agent = apply_skills_to_agent(agent, list(skills))
            else:
                agent = apply_skills_to_agent(agent)  # 用 agent 自带 skills
        elif ref is None:
            raise ValueError(f"LLMStep {self.id!r} 未配置 agent")
        else:
            raise TypeError(
                f"LLMStep {self.id!r} 不支持的 agent 配置类型: {type(ref).__name__}"
            )

        # 填充默认值(max_tool_iterations=0 -> 全局默认)
        agent = agent.resolve_defaults()

        # 应用温度覆盖
        if self.temperature_override is not None:
            agent = dataclasses.replace(agent, temperature=self.temperature_override)

        # MCP 工具注入:把 agent.mcp 声明依赖的 server 对应工具名合并进 tools,
        # 使 _build_tools_schema 能把 MCP 工具 schema 带给模型。补齐
        # 「注册表有、schema 无」缺口。
        if self.mcp_manager is not None:
            agent = self.mcp_manager.inject_mcp_tools(agent)

        return agent

    # ------------------------------------------------------------------
    # 消息组装
    # ------------------------------------------------------------------
    def _build_system_message(
        self, agent: AgentConfig, ctx: "Context"
    ) -> LLMMessage | None:
        """构建 system 消息(提取自 _build_messages,供会话模式复用)。

        ``self.system_override`` 优先,否则 ``agent.system``;非空时
        经 ``resolve_template`` 解析 ``{{var}}`` / ``${ENV}``。
        """
        system_tpl = (
            self.system_override if self.system_override is not None else agent.system
        )
        if system_tpl:
            system_text = self._render_str(system_tpl, ctx)
            if system_text:
                return LLMMessage(role="system", content=system_text)
        return None

    def _build_user_message(
        self, agent: AgentConfig, ctx: "Context"
    ) -> LLMMessage:
        """构建 user 消息(提取自 _build_messages,供会话模式复用)。

        ``self.prompt`` 为 ``str`` 时经 ``resolve_value`` 解析后 ``str()`` 化;
        为 ``list[dict]`` 时(多模态),逐个 content part 递归解析
        ``{{var}}`` / ``${ENV}``,保持 list 顺序原样作为 user 消息 content。
        同时记录 input_summary 供 trace。
        """
        if isinstance(self.prompt, list):
            # 多模态:逐个 content part 解析模板变量(叠加端口作用域)
            user_content: str | list[dict] = [
                _resolve_content_part(part, lambda t: self._render(t, ctx))
                for part in self.prompt
            ]
            # input_summary:拼接所有 text part 供 trace
            text_parts = [
                p.get("text", "")
                for p in user_content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            self._last_input_summary = self._summarize(" ".join(text_parts))
            return LLMMessage(role="user", content=user_content)
        else:
            user_raw = self._render(self.prompt, ctx)
            user_content_str = user_raw if isinstance(user_raw, str) else str(user_raw)
            self._last_input_summary = self._summarize(user_content_str)
            return LLMMessage(role="user", content=user_content_str)

    def _render_prompt(self, agent: AgentConfig, ctx: "Context") -> str:
        """渲染 prompt 模板为字符串(供 fork 模式的 new_prompt 参数)。

        多模态 prompt 取所有 text part 拼接;纯文本 prompt 经 ``resolve_value``
        解析后 ``str()`` 化。
        """
        if isinstance(self.prompt, list):
            parts = [
                _resolve_content_part(part, lambda t: self._render(t, ctx))
                for part in self.prompt
            ]
            text_parts = [
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return " ".join(text_parts)
        user_raw = self._render(self.prompt, ctx)
        return user_raw if isinstance(user_raw, str) else str(user_raw)

    def _build_messages(
        self, agent: AgentConfig, ctx: "Context"
    ) -> list[LLMMessage]:
        """组装初始消息列表(system + user)。

        - system:``self.system_override`` 优先,否则 ``agent.system``;非空时
          经 ``resolve_template`` 解析 ``{{var}}`` / ``${ENV}``。
        - user:``self.prompt`` 为 ``str`` 时经 ``resolve_value`` 解析后 ``str()`` 化;
          为 ``list[dict]`` 时(多模态),逐个 content part 递归解析
          ``{{var}}`` / ``${ENV}``,保持 list 顺序原样作为 user 消息 content。

        同时记录 input_summary 供 trace。
        """
        messages: list[LLMMessage] = []
        system_msg = self._build_system_message(agent, ctx)
        if system_msg is not None:
            messages.append(system_msg)
        messages.append(self._build_user_message(agent, ctx))
        return messages

    def _build_messages_with_conversation(
        self, agent: AgentConfig, ctx: "Context"
    ) -> list[LLMMessage]:
        """按 conversation mode 加载/构建消息(T3.3)。

        mode=None 时走原 _build_messages(零改动);
        mode=start 时构建 [system, user],末尾 save 覆盖;
        mode=continue 时 load_and_validate + 追加 user;
        mode=fork 时 load_and_validate + fork_at_user 分支。

        渲染 key/from 一次,暂存 _conv_resolved_key 供 save 复用。
        同时把历史 meta.cached_tokens 累计到 _cached_tokens_total
        (T7:跨 continue/fork 轮次叠加)。
        """
        from agentkit.core.conversation import (
            fork_at_user,
            load_and_validate,
        )

        mode = self.conversation_mode
        if mode is None:
            return self._build_messages(agent, ctx)

        # 渲染模板化 key/from(与 prompt 同一模板引擎)
        key = (
            self._render_str(self.conversation_key, ctx)
            if self.conversation_key
            else self.id
        )
        from_key = self.conversation_from
        if from_key:
            from_key = self._render_str(from_key, ctx)
        else:
            from_key = key
        # 暂存供 save_conversation 复用(避免二次渲染)
        self._conv_resolved_key = key
        self._conv_resolved_from = from_key

        if mode == "start":
            # 走原 _build_messages,末尾 save_conversation 覆盖
            return self._build_messages(agent, ctx)

        if mode == "continue":
            loaded, meta = load_and_validate(
                ctx, from_key, agent, self.conversation_compat,
                system_override=self.system_override is not None,
            )
            # 累计历史 cached_tokens(T7:跨轮叠加)
            self._cached_tokens_total += int(meta.get("cached_tokens", 0))
            user_msg = self._build_user_message(agent, ctx)
            return loaded + [user_msg]

        if mode == "fork":
            loaded, meta = load_and_validate(
                ctx, from_key, agent, self.conversation_compat,
                system_override=self.system_override is not None,
            )
            # 累计历史 cached_tokens
            self._cached_tokens_total += int(meta.get("cached_tokens", 0))
            prompt = self._render_prompt(agent, ctx)
            return fork_at_user(loaded, self.conversation_fork_at, prompt or None)

        raise ValueError(f"不支持的 conversation.mode: {mode!r}")

    # ------------------------------------------------------------------
    # 工具 schema 构建
    # ------------------------------------------------------------------
    def _build_tools_schema(self, agent: AgentConfig) -> list[dict]:
        """从 ``agent.tools`` 构建 OpenAI Function Call 工具 schema 列表。

        每个 Tool 的 ``schema`` 属性返回
        ``{"name", "description", "parameters"}``,这里包装为 OpenAI 约定:
        ``{"type": "function", "function": {...}}``。

        未注册的工具名跳过(配置错误但不阻塞 LLM 调用;LLM 不会看到该工具)。
        """
        schemas: list[dict] = []
        for name in agent.tools:
            try:
                tool = get_tool(name)
            except KeyError:
                # 工具未注册:跳过。配置层应在静态校验阶段告警。
                continue
            s = tool.schema
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": s.get("name", name),
                        "description": s.get("description", ""),
                        "parameters": s.get(
                            "parameters", {"type": "object", "properties": {}}
                        ),
                    },
                }
            )
        return schemas

    # ------------------------------------------------------------------
    # LLM 客户端获取
    # ------------------------------------------------------------------
    def _get_client(self, agent: AgentConfig) -> LLMClient:
        """返回生效的 LLM 客户端(按 provider/model 路由)。

        路由优先级:
            1. 注入的 ``self.llm_client`` (测试 / 显式注入)。
            2. ``agent.provider`` 显式指定:用 ``get_client_for_provider`` 创建
               并缓存客户端。
            3. ``agent.model`` 自动反查:通过 ``resolve_provider_by_model`` 查找
               唯一匹配的提供商;命中则用之,歧义(多提供商同模型名)则跳过。
            4. 全局默认客户端(``get_default_client()``)。

        Args:
            agent: 已解析的 Agent 配置(取 provider / model)。

        Returns:
            LLMClient: 生效的客户端实例。

        Raises:
            RuntimeError: 所有路由路径均无可用客户端。
        """
        # 1. 注入优先
        if self.llm_client is not None:
            return self.llm_client

        # 延迟导入:仅在需要客户端时才引入 llm 包(及其 openai/httpx 依赖)
        from agentkit.llm import (
            get_default_client,
            get_client_for_provider,
        )
        from agentkit.llm.provider import resolve_provider_by_model

        # 2. 显式 provider
        if agent.provider:
            return get_client_for_provider(agent.provider)

        # 3. 按模型名自动反查提供商
        matched = resolve_provider_by_model(agent.model)
        if matched:
            return get_client_for_provider(matched)

        # 4. 全局默认
        client = get_default_client()
        if client is None:
            raise RuntimeError(
                f"LLMStep {self.id!r} 未配置 LLM 客户端:既未注入 llm_client,"
                f"agent.provider 未指定,模型 {agent.model!r} 也未匹配到已注册"
                f"提供商,且未通过 set_default_client 注册默认客户端。"
            )
        return client

    # ------------------------------------------------------------------
    # Function Call 循环
    # ------------------------------------------------------------------
    async def _run_function_call_loop(
        self,
        messages: list[LLMMessage],
        tools_schema: list[dict],
        agent: AgentConfig,
        client: LLMClient,
        ctx: "Context",
    ) -> str:
        """运行 Function Call 循环,返回 LLM 的最终文本输出。

        流程:
            - 无工具可调用(或 max_tool_iterations<=0):单次调用,返回 content。
            - 有工具:循环至多 ``max_tool_iterations`` 次,每次调用 LLM:
              * 无 tool_calls -> 返回 content(最终答案);
              * 有 tool_calls -> 执行工具,追加 assistant(tool_calls) 与
                tool(结果)消息,继续循环。
            - 循环耗尽(LLM 持续要求工具):追加 user 消息要求给出最终答案,
              再发起一次不带 tools 的调用索取最终文本。
        """
        max_iter = agent.max_tool_iterations
        offer_tools = tools_schema if (tools_schema and max_iter > 0) else None

        # 无工具:单次调用即可(attempt=0,首轮)
        if offer_tools is None:
            resp = await self._llm_call(client, messages, None, agent, attempt=0)
            return resp.content or ""

        # Function Call 循环:每轮走 _llm_call(stream 时每轮流式)
        for _ in range(max_iter):
            resp = await self._llm_call(
                client, messages, offer_tools, agent, attempt=0
            )
            if not resp.has_tool_calls:
                # LLM 给出最终文本
                return resp.content or ""

            # 有 tool_calls:执行并把结果追加到对话历史
            # 提取 reasoning_content(DeepSeek/MiMo 多轮工具调用需回传)
            reasoning = None
            if isinstance(resp.raw, dict):
                reasoning = resp.raw.get("reasoning_content")
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=resp.content,
                    tool_calls=list(resp.tool_calls),
                    reasoning_content=reasoning,
                )
            )
            for tc in resp.tool_calls:
                result = await self._execute_tool_call(tc, ctx)
                messages.append(
                    LLMMessage(
                        role="tool",
                        content=json.dumps(result, ensure_ascii=False),
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                )
            # 继续循环:LLM 在下一轮看到工具结果

        # 工具调用轮次耗尽:强制索取最终答案(不带 tools,LLM 无法再调用工具)
        messages.append(
            LLMMessage(
                role="user",
                content=(
                    "已达到工具调用上限,请基于已有信息直接给出最终结果,"
                    "不要再调用工具。"
                ),
            )
        )
        resp = await self._llm_call(client, messages, None, agent, attempt=0)
        return resp.content or ""

    async def _execute_tool_call(
        self, tc: ToolCall, ctx: "Context"
    ) -> dict:
        """执行一次 Function Call,返回结果 dict(将 JSON 序列化为 tool 消息)。

        容错策略:
            - 工具未注册:返回 ``{"error": "工具 ... 未注册"}``,让 LLM 能据此
              调整策略,不抛异常中断整个 Step。
            - 工具执行抛异常:返回 ``{"error": "工具执行失败: ..."}``,同理。
        所有调用(含失败)记录到 ``self._tool_calls_record`` 供 trace。
        """
        record: dict[str, Any] = {
            "tool": tc.name,
            "arguments": dict(tc.arguments),
            "status": "ok",
        }
        try:
            tool = get_tool(tc.name)
        except KeyError:
            err = f"工具 {tc.name!r} 未注册"
            record["status"] = "error"
            record["error"] = err
            self._tool_calls_record.append(record)
            return {"error": err}

        try:
            # 按 tool.execution 分派:inline(默认)/ thread / process
            # 与 ToolStep.run 保持一致；使 ReportEngineTool 标 thread 在
            # Function Call 路径下也生效（对齐 §5.5）。
            # 优先用注入的 _blocking_executor，否则回落到全局单例。
            executor = self._blocking_executor
            if executor is None:
                from agentkit.runtime.blocking import get_blocking_executor
                executor = get_blocking_executor()
            result = await executor.run_tool(tool, dict(tc.arguments), ctx)
            # Tool 接口约定返回 dict;防御性处理非 dict 返回
            if not isinstance(result, dict):
                result = {"value": result}
            record["result_summary"] = self._summarize(result)
            self._tool_calls_record.append(record)
            return result
        except Exception as e:
            err = f"工具执行失败: {e}"
            record["status"] = "error"
            record["error"] = err
            self._tool_calls_record.append(record)
            return {"error": err}

    # ------------------------------------------------------------------
    # 输出契约保障链
    # ------------------------------------------------------------------
    def _make_parser(self, agent: AgentConfig) -> Parser:
        """根据 ``output_model`` / ``output_format`` 选择解析器。

        优先级:
            1. ``agent.output_model`` 非 None:用 ``PydanticParser``(严格
               schema 校验,返回 pydantic 模型实例)。
            2. ``self.output_format == "json"``:用 ``JSONParser``(JSON
               语法解析,返回 dict/list/标量)。
            3. 默认:用 ``TextParser``(纯文本,返回 strip 后的 str)。

        Args:
            agent: 已解析的 Agent 配置。

        Returns:
            Parser: 解析器实例。
        """
        if agent.output_model is not None:
            return PydanticParser(agent.output_model)
        if self.output_format == "json":
            return JSONParser()
        return TextParser()

    async def _run_output_contract(
        self,
        content: str,
        messages: list[LLMMessage],
        agent: AgentConfig,
        client: LLMClient,
        ctx: "Context",
    ) -> tuple[Any, str | None]:
        """运行输出契约保障链,返回 ``(value, error)``。

        成功时 ``error`` 为 None;失败时 ``value`` 为 None、``error`` 描述原因,
        由 :meth:`run` 按 ``on_exhausted`` 决策。

        链路:
            1. 解析器(由 :meth:`_make_parser` 选择)解析 + ``output_validator``
               业务校验。``TextParser`` 恒成功;``JSONParser`` / ``PydanticParser``
               解析失败可重试。
            2. 失败 -> 附加 ``retry_hint`` 重试,共 ``agent.retry.count`` 次。
            3. 重试耗尽 -> 用 ``fallback_model`` 再试一次(若配置)。
            4. 仍失败 -> 返回 (None, error)。
        """
        parser = self._make_parser(agent)

        def _try_parse_and_validate(text: str) -> tuple[Any, str | None]:
            result = parser.parse(text)
            if not result.ok:
                return None, result.error or "解析失败"
            if not agent.output_validator(result.value, ctx):
                return None, "业务校验未通过(output_validator 返回 False)"
            return result.value, None

        # 1. 首次解析
        value, err = _try_parse_and_validate(content)
        if err is None:
            return value, None

        # 2. 修复重试
        retry_count = agent.retry.count if agent.retry else 0
        for attempt in range(retry_count):
            _, hint = parser.parse_with_retry_hint(content)
            if not hint:
                # 解析恒成功(如 TextParser)但业务校验失败:用通用提示
                hint = f"上一次输出未通过校验: {err}。请重新输出合法结果。"
            messages.append(LLMMessage(role="assistant", content=content))
            messages.append(LLMMessage(role="user", content=hint))
            # 重试轮:attempt=attempt+1(0 是首次),流式 hook 据此重置缓冲
            resp = await self._llm_call(
                client, messages, None, agent, attempt=attempt + 1
            )
            content = resp.content or ""
            value, err = _try_parse_and_validate(content)
            if err is None:
                return value, None

        # 3. 降级模型(attempt 续接 retry_count+1,标识降级尝试)
        if agent.fallback_model:
            messages.append(
                LLMMessage(
                    role="user",
                    content="请重新输出最终结果,确保格式合法且通过校验。",
                )
            )
            resp = await self._llm_call(
                client,
                messages,
                None,
                agent,
                model=agent.fallback_model,
                attempt=retry_count + 1,
            )
            content = resp.content or ""
            value, err = _try_parse_and_validate(content)
            if err is None:
                return value, None

        # 4. 仍失败:返回错误,由 run 按 on_exhausted 决策
        return None, err

    def _apply_on_exhausted(self, agent: AgentConfig, error: str) -> Any:
        """按 ``agent.on_exhausted`` 策略处理契约链耗尽。

        - ``raise``:抛 :class:`OutputContractError`(交给 execute 的重试/钩子)。
        - ``default``:返回 ``agent.default_value``。
        - ``skip``:返回 ``None``(step 产出空值,流程继续)。
        - 未知策略:按 raise 处理(抛异常)。

        Args:
            agent: Agent 配置。
            error: 契约链失败原因。

        Returns:
            Any: ``default``/``skip`` 时的兜底值;``raise`` 时不会返回(抛异常)。
        """
        policy = agent.on_exhausted
        if policy == ExhaustedPolicy.DEFAULT:
            return agent.default_value
        if policy == ExhaustedPolicy.SKIP:
            return None
        # RAISE 或未知策略 -> 抛异常
        raise OutputContractError(
            f"LLMStep {self.id!r} 输出契约链失败(策略={policy!r}): {error}"
        )

    # ------------------------------------------------------------------
    # LLM 调用封装(调度流式 / 非流式 + 累计 token + 触发钩子)
    # ------------------------------------------------------------------
    async def _llm_call(
        self,
        client: LLMClient,
        messages: list[LLMMessage],
        tools: list[dict] | None,
        agent: AgentConfig,
        *,
        model: str | None = None,
        attempt: int = 0,
    ) -> LLMResponse:
        """单次 LLM 调用调度器,按 ``self.stream`` 选择流式或非流式。

        两条路径均返回完整 ``LLMResponse``,统一累计 token 用量并触发
        ``on_llm_call`` 钩子。流式路径额外触发 ``on_llm_stream_*`` 三段钩子。

        Args:
            client:   LLM 客户端。
            messages: 对话消息(本方法不修改,由调用方维护追加)。
            tools:    Function Call 工具 schema;None 表示不启用工具。
            agent:    Agent 配置(取 temperature / model)。
            model:    模型名覆盖(降级模型时用);None 时用 ``agent.model``。
            attempt:  第几次尝试(0=首次,1+=retry/降级)。仅流式路径使用,
                      传给 ``on_llm_stream_*`` 钩子让前端据此重置缓冲。

        Returns:
            LLMResponse: LLM 响应(流式路径从累积 chunk 重构)。
        """
        if self.stream:
            return await self._chat_stream_full(
                client, messages, tools, agent, model=model, attempt=attempt
            )
        return await self._chat(
            client, messages, tools, agent, model=model
        )

    async def _chat(
        self,
        client: LLMClient,
        messages: list[LLMMessage],
        tools: list[dict] | None,
        agent: AgentConfig,
        *,
        model: str | None = None,
    ) -> LLMResponse:
        """非流式 LLM 调用,累计 token 用量并触发 ``on_llm_call`` 钩子。

        Args:
            client:  LLM 客户端。
            messages: 对话消息(本方法不修改,由调用方维护追加)。
            tools:    Function Call 工具 schema;None 表示不启用工具。
            agent:    Agent 配置(取 temperature / model)。
            model:    模型名覆盖(降级模型时用);None 时用 ``agent.model``。

        Returns:
            LLMResponse: LLM 响应。
        """
        resp = await client.chat(
            messages=messages,
            tools=tools if tools else None,
            temperature=agent.temperature,
            model=model or agent.model,
        )
        # 累计 token 用量(供 _enrich_trace 回填 trace.token_usage)
        self._token_usage_total += int(resp.usage.total_tokens)
        # 累计 cached_tokens(T7 可观测性:会话缓存命中)
        if resp.usage and resp.usage.cached_tokens:
            self._cached_tokens_total += resp.usage.cached_tokens
        # 记录最近一次 LLM 输出(供会话保存时追加 assistant 消息)
        self._last_llm_content = resp.content or ""
        # 触发 on_llm_call 钩子(供 LoggingHooks / TokenAccountingHooks 计量)
        # execute 在调 run 前已把 hooks 写入 self._hooks;此处直接复用。
        hooks = self._hooks
        if hooks is not None:
            await hooks.on_llm_call(agent, messages, resp, resp.usage)
        return resp

    async def _chat_stream_full(
        self,
        client: LLMClient,
        messages: list[LLMMessage],
        tools: list[dict] | None,
        agent: AgentConfig,
        *,
        model: str | None = None,
        attempt: int = 0,
    ) -> LLMResponse:
        """流式 LLM 调用,累积 chunk 重构完整 ``LLMResponse``。

        流程:
            1. 触发 ``on_llm_stream_start``(前端据此重置缓冲)。
            2. 迭代 ``client.chat_stream``:
               - ``delta_content`` 累积到 ``accumulated``,触发 ``on_llm_stream_delta``。
               - 末尾 chunk 携带完整 ``tool_calls`` / ``finish_reason`` / ``usage``。
            3. 触发 ``on_llm_stream_end``。
            4. 从累积数据重构 ``LLMResponse``,累计 token,触发 ``on_llm_call``。

        流式不改变契约链语义:解析 / 重试 / 降级仍按完整文本执行。
        ``attempt`` 标识第几次尝试(retry/降级时递增),让前端区分首次与重试。

        Args:
            client:   LLM 客户端。
            messages: 对话消息。
            tools:    Function Call 工具 schema;None 表示不启用工具。
            agent:    Agent 配置。
            model:    模型名覆盖。
            attempt:  第几次尝试(0=首次,1+=retry/降级)。

        Returns:
            LLMResponse: 从流式 chunk 累积重构的完整响应。
        """
        hooks = self._hooks
        accumulated = ""
        accumulated_reasoning = ""
        tool_calls: list[ToolCall] = []
        finish_reason = ""
        usage = None

        if hooks is not None:
            await hooks.on_llm_stream_start(self, agent, attempt=attempt)

        async for chunk in client.chat_stream(
            messages=messages,
            tools=tools if tools else None,
            temperature=agent.temperature,
            model=model or agent.model,
        ):
            # 文本增量:累积并推送
            if chunk.delta_content:
                accumulated += chunk.delta_content
                if hooks is not None:
                    await hooks.on_llm_stream_delta(
                        self, agent, chunk.delta_content, accumulated,
                        attempt=attempt,
                        delta_reasoning=None,
                    )
            # 思考链增量:累积并推送(与 content 对称,框架不负责最终拼接)
            if chunk.delta_reasoning_content:
                accumulated_reasoning += chunk.delta_reasoning_content
                if hooks is not None:
                    await hooks.on_llm_stream_delta(
                        self, agent, "", accumulated,
                        attempt=attempt,
                        delta_reasoning=chunk.delta_reasoning_content,
                    )
            # 末尾 chunk:tool_calls / finish_reason / usage
            if chunk.tool_calls is not None:
                tool_calls = chunk.tool_calls
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            if chunk.usage is not None:
                usage = chunk.usage

        if hooks is not None:
            await hooks.on_llm_stream_end(self, agent, accumulated, attempt=attempt)

        # 重构完整 LLMResponse(与非流式 _chat 返回结构一致)
        if usage is None:
            # 客户端未返回 usage(如部分 provider 流式不返回 token):用零值兜底
            usage = LLMUsage()
        raw = {"reasoning_content": accumulated_reasoning} if accumulated_reasoning else None
        resp = LLMResponse(
            content=accumulated,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            raw=raw,
        )
        # 累计 token 用量 + 触发 on_llm_call(与非流式路径统一)
        self._token_usage_total += int(usage.total_tokens)
        # 累计 cached_tokens(T7 可观测性:会话缓存命中)
        if usage and usage.cached_tokens:
            self._cached_tokens_total += usage.cached_tokens
        # 记录最近一次 LLM 输出(供会话保存时追加 assistant 消息)
        self._last_llm_content = resp.content or ""
        if hooks is not None:
            await hooks.on_llm_call(agent, messages, resp, usage)
        return resp

    # ------------------------------------------------------------------
    # _enrich_trace —— 把 token 用量与工具调用链回填到 trace
    # ------------------------------------------------------------------
    def _enrich_trace(self, trace: StepTrace) -> None:
        """重写:把运行期累计的 token 用量与工具调用记录回填到 trace。

        ``execute`` 在写入 trace 前调用本方法;``run`` 期间把数据暂存到实例
        scratch(``_token_usage_total`` / ``_tool_calls_record`` /
        ``_last_input_summary``),本方法负责转移。这样 ``run`` 无需接触
        ``execute`` 私有的 trace 对象。
        """
        trace.token_usage = self._token_usage_total
