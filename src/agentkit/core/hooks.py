"""hooks —— 生命周期钩子模块。

定义 AgentKit 工作流与 Step 执行全生命周期的回调钩子,用于可观测性、日志、
Token 计量、追踪、测试拦截等横切关注点。

设计原则
--------
- **高度模块化**:本模块仅依赖标准库与可选的 ``opentelemetry``,不依赖任何其他
  agentkit 子模块。所有跨模块类型引用通过 ``TYPE_CHECKING`` 守卫,运行时不导入,
  从而彻底避免循环依赖。
- **可拓展**:新增 Hook 只需继承 :class:`LifecycleHooks` 并重写相关方法。
- **优雅降级**::class:`TracingHooks` 在 ``opentelemetry`` 未安装时静默 no-op。
- **全异步**:所有回调方法均为 ``async def``,即便空实现亦然,保证调用方可在
  事件循环中统一 ``await``。

接口契约
--------
:class:`LifecycleHooks` 与 :class:`ErrorAction` 的方法签名是框架内部共享契约,
:class:`~agentkit.core.workflow.Workflow` 与各 Step 在关键节点调用这些钩子。
修改签名会破坏其他模块,需谨慎。

内置 Hooks
----------
- :class:`LoggingHooks`            标准库 logging 日志
- :class:`TokenAccountingHooks`    Token 用量累计
- :class:`TracingHooks`            OpenTelemetry Span 集成(可选依赖)
- :class:`MockLLMHooks`            测试用,记录 LLM 调用
- :class:`MockToolHooks`           测试用,记录工具调用
- :class:`CompositeHooks`          聚合多个 hooks 依次调用
"""

from __future__ import annotations

import logging
from abc import ABC
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable, List, Optional

if TYPE_CHECKING:
    # 仅用于类型注解,运行时不导入,避免循环依赖。
    # 这些类型在 core.context / core.workflow / steps.base / core.agent 中定义。
    from agentkit.core.agent import AgentConfig
    from agentkit.core.context import Context
    from agentkit.core.workflow import Workflow
    from agentkit.steps.base import BaseStep, StepTrace


# ---------------------------------------------------------------------------
# ErrorAction —— on_step_error 返回值枚举
# ---------------------------------------------------------------------------
class ErrorAction(Enum):
    """``on_step_error`` 回调返回值,指示错误发生后工作流应采取的行为。

    Attributes:
        RAISE:   重新抛出异常,工作流失败。
        RETRY:   重试当前 Step(由 Workflow 处理重试计数与退避)。
        SKIP:    跳过当前 Step,继续下一步。
        DEFAULT: 用默认值填充 output,继续下一步。
    """

    RAISE = "raise"
    RETRY = "retry"
    SKIP = "skip"
    DEFAULT = "default"


# ---------------------------------------------------------------------------
# LifecycleHooks —— 抽象基类
# ---------------------------------------------------------------------------
class LifecycleHooks(ABC):
    """生命周期钩子抽象基类。

    所有方法默认空实现(空 ``async def``),子类按需重写感兴趣的回调。
    :class:`~agentkit.core.workflow.Workflow` 与各 Step 在执行前后调用这些钩子,
    子类可借此实现日志、计量、追踪、测试拦截等横切逻辑。

    所有方法均为协程,调用方必须 ``await``。默认实现直接返回,不产生任何副作用。
    """

    async def before_workflow(self, wf: "Workflow", ctx: "Context") -> None:
        """工作流开始前调用(在首个 Step 执行前)。"""
        ...

    async def after_workflow(
        self, wf: "Workflow", ctx: "Context", result: "Context"
    ) -> None:
        """工作流结束后调用(所有 Step 完成或异常终止后)。

        Args:
            result: 工作流最终输出的 Context。
        """
        ...

    async def before_step(self, step: "BaseStep", ctx: "Context") -> None:
        """单个 Step 开始执行前调用。"""
        ...

    async def after_step(
        self, step: "BaseStep", ctx: "Context", trace: "StepTrace"
    ) -> None:
        """单个 Step 执行完成后调用(无论成功或失败)。

        Args:
            trace: 本次执行的轨迹(含 status / duration_ms 等)。
        """
        ...

    async def on_step_error(
        self, step: "BaseStep", ctx: "Context", error: BaseException
    ) -> ErrorAction:
        """Step 抛出异常时调用,返回后续行为策略。

        默认返回 :attr:`ErrorAction.RAISE`,即重新抛出异常使工作流失败。
        子类可重写以实现重试 / 跳过 / 默认值填充等容错策略。

        Args:
            error: Step 抛出的异常实例。

        Returns:
            ErrorAction: 指示工作流后续行为的枚举值。
        """
        return ErrorAction.RAISE

    async def on_llm_call(
        self,
        agent: "AgentConfig",
        prompt: Any,
        response: Any,
        usage: Any,
    ) -> None:
        """LLM 调用完成后调用,用于 Token 计量、日志、Mock 拦截等。

        Args:
            agent:    发起调用的 AgentConfig。
            prompt:   发送给 LLM 的 prompt(具体形态由 LLMStep 决定)。
            response: LLM 返回的响应。
            usage:    Token 用量,形如 ``{prompt_tokens, completion_tokens,
                      total_tokens}`` 的 dict 或具备同名属性的对象。
        """
        ...

    # ------------------------------------------------------------------
    # 流式输出回调
    # ------------------------------------------------------------------
    # 三个方法构成"流式生命周期",调用契约:
    #   on_llm_stream_start → on_llm_stream_delta(多次) → on_llm_stream_end
    # 每次 LLM 流式调用(含 retry / 降级模型)都触发完整一轮。
    # ``attempt`` 参数标识第几次尝试(0=首次, 1+=retry/降级),前端据此
    # 重置缓冲,避免拼接上一次失败的废文本。
    async def on_llm_stream_start(
        self,
        step: "BaseStep",
        agent: "AgentConfig",
        *,
        attempt: int = 0,
    ) -> None:
        """单次流式 LLM 调用开始(含 retry / 降级模型)。

        前端消费者应据此**重置缓冲**——上一次 attempt 的文本视为废文本。
        典型实现:清空 SSE 队列、重置终端行、记录"重新生成中"状态。

        Args:
            step:    发起流式调用的 Step。
            agent:   发起调用的 AgentConfig。
            attempt: 第几次尝试。0=首次调用,1+=解析失败后的重试或降级模型。
        """
        ...

    async def on_llm_stream_delta(
        self,
        step: "BaseStep",
        agent: "AgentConfig",
        delta: str,
        accumulated: str,
        *,
        attempt: int = 0,
        delta_reasoning: str | None = None,
    ) -> None:
        """流式文本片段。

        Args:
            step:            发起流式调用的 Step。
            agent:           发起调用的 AgentConfig。
            delta:           本次增量文本。
            accumulated:     本次 attempt 内的累计文本(retry 时已重置)。
            attempt:         第几次尝试(同 on_llm_stream_start)。
            delta_reasoning: 思考链增量(与 delta 同构,框架只透传不累积)。
                             ``None`` 表示本片段无思考链。
        """
        ...

    async def on_llm_stream_end(
        self,
        step: "BaseStep",
        agent: "AgentConfig",
        full_content: str,
        *,
        attempt: int = 0,
    ) -> None:
        """单次流式 LLM 调用结束。

        Args:
            step:         发起流式调用的 Step。
            agent:        发起调用的 AgentConfig。
            full_content: 本次 attempt 的完整文本(等同末尾 accumulated)。
            attempt:      第几次尝试(同 on_llm_stream_start)。
        """
        ...

    async def on_tool_call(
        self, tool: Any, params: dict, result: Any
    ) -> None:
        """工具调用完成后调用。

        Args:
            tool:   被调用的工具对象(应具备 ``name`` 属性)。
            params: 调用参数。
            result: 工具返回结果。
        """
        ...

    async def on_mcp_call(
        self, server: str, tool: str, params: dict, result: Any
    ) -> None:
        """MCP(模型上下文协议)工具调用完成后调用。

        Args:
            server: MCP server 名称。
            tool:   工具名。
            params: 调用参数。
            result: 返回结果。
        """
        ...


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _usage_value(usage: Any, key: str, default: int = 0) -> int:
    """从 usage 中取某项 token 数,兼容 dict 与对象两种形态。

    Args:
        usage: ``{prompt_tokens, completion_tokens, total_tokens}`` 形态的 dict,
               或具备同名属性的对象;为 None 时返回 default。
        key:   字段名。
        default: 缺失时的默认值。

    Returns:
        int: 对应字段的整数值;无法解析为 int 时返回 default。
    """
    if usage is None:
        return default
    if isinstance(usage, dict):
        value = usage.get(key, default)
    else:
        value = getattr(usage, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# LoggingHooks —— 标准库日志
# ---------------------------------------------------------------------------
class LoggingHooks(LifecycleHooks):
    """使用标准库 ``logging`` 记录关键生命周期事件。

    在工作流 / Step 开始结束、LLM / 工具调用、Step 异常等节点输出日志,
    便于运行时观察与排障。

    Args:
        logger: 可选的 ``logging.Logger``;默认使用 ``logging.getLogger("agentkit")``。
    """

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.logger: logging.Logger = logger or logging.getLogger("agentkit")

    async def before_workflow(self, wf: "Workflow", ctx: "Context") -> None:
        self.logger.info("工作流 %s 开始", getattr(wf, "name", wf))

    async def after_workflow(
        self, wf: "Workflow", ctx: "Context", result: "Context"
    ) -> None:
        self.logger.info("工作流 %s 完成", getattr(wf, "name", wf))

    async def before_step(self, step: "BaseStep", ctx: "Context") -> None:
        self.logger.info("Step %s 开始", getattr(step, "id", step))

    async def after_step(
        self, step: "BaseStep", ctx: "Context", trace: "StepTrace"
    ) -> None:
        self.logger.info(
            "Step %s 完成 status=%s duration=%sms",
            getattr(step, "id", step),
            getattr(trace, "status", None),
            getattr(trace, "duration_ms", None),
        )

    async def on_step_error(
        self, step: "BaseStep", ctx: "Context", error: BaseException
    ) -> ErrorAction:
        self.logger.error("Step %s 失败: %s", getattr(step, "id", step), error)
        return ErrorAction.RAISE

    async def on_tool_call(
        self, tool: Any, params: dict, result: Any
    ) -> None:
        self.logger.debug("Tool %s 调用", getattr(tool, "name", tool))

    async def on_llm_call(
        self,
        agent: "AgentConfig",
        prompt: Any,
        response: Any,
        usage: Any,
    ) -> None:
        self.logger.debug(
            "LLM 调用 agent=%s model=%s",
            getattr(agent, "name", agent),
            getattr(agent, "model", None),
        )


# ---------------------------------------------------------------------------
# TokenAccountingHooks —— Token 用量累计
# ---------------------------------------------------------------------------
class TokenAccountingHooks(LifecycleHooks):
    """累计 LLM Token 用量。

    在 :meth:`on_llm_call` 中累加 ``usage`` 携带的 prompt / completion / total
    token 数,提供只读属性与 :meth:`report` 汇总。工作流结束时日志输出总量。

    usage 兼容两种形态:
        - dict: ``{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}``
        - 对象: 具备同名属性的对象(如 pydantic model / dataclass)。
    """

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger: logging.Logger = logger or logging.getLogger("agentkit")
        self._prompt_tokens: int = 0
        self._completion_tokens: int = 0
        self._total_tokens: int = 0

    # -- 只读属性 ---------------------------------------------------------
    @property
    def prompt_tokens(self) -> int:
        """累计的 prompt token 数。"""
        return self._prompt_tokens

    @property
    def completion_tokens(self) -> int:
        """累计的 completion token 数。"""
        return self._completion_tokens

    @property
    def total_tokens(self) -> int:
        """累计的 total token 数。

        若 usage 未提供 total_tokens,则回退为 prompt + completion 之和。
        """
        return self._total_tokens

    # -- 报告 -------------------------------------------------------------
    def report(self) -> dict:
        """返回 token 用量汇总。

        Returns:
            dict: ``{"prompt_tokens", "completion_tokens", "total_tokens"}``。
        """
        return {
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self._total_tokens,
        }

    # -- 钩子 -------------------------------------------------------------
    async def on_llm_call(
        self,
        agent: "AgentConfig",
        prompt: Any,
        response: Any,
        usage: Any,
    ) -> None:
        prompt_t = _usage_value(usage, "prompt_tokens")
        completion_t = _usage_value(usage, "completion_tokens")
        # total_tokens 可能在 usage 中缺失,此时回退为本次 prompt + completion 之和。
        total_t = _usage_value(
            usage, "total_tokens", default=prompt_t + completion_t
        )
        self._prompt_tokens += prompt_t
        self._completion_tokens += completion_t
        self._total_tokens += total_t

    async def after_workflow(
        self, wf: "Workflow", ctx: "Context", result: "Context"
    ) -> None:
        self._logger.info(
            "工作流 %s 总 token 数: %s", getattr(wf, "name", wf), self._total_tokens
        )


# ---------------------------------------------------------------------------
# TracingHooks —— OpenTelemetry Span 集成(可选依赖)
# ---------------------------------------------------------------------------
class TracingHooks(LifecycleHooks):
    """将工作流与 Step 执行映射为 OpenTelemetry Span。

    ``opentelemetry`` 为**可选依赖**:未安装时所有方法静默 no-op,不会抛异常,
    也不会影响工作流执行。

    Args:
        tracer: 可选的 opentelemetry Tracer。为 None 时所有方法 no-op。
                可通过 ``opentelemetry.trace.get_tracer("agentkit")`` 获取。
    """

    def __init__(self, tracer: Any = None) -> None:
        self._tracer: Any = tracer
        # opentelemetry 模块缓存(惰性导入,避免硬依赖)。
        self._otel_checked: bool = False
        self._otel_trace: Any = None
        # 顶层 workflow span;同一时刻通常只有一个工作流在执行。
        self._workflow_span: Any = None
        # 每个 Step 一个 span,按 id(step) 索引,支持并发/嵌套 Step。
        self._step_spans: dict[int, Any] = {}

    # -- 内部:惰性导入 opentelemetry ------------------------------------
    def _ensure_otel(self) -> Any:
        """惰性导入 opentelemetry.trace 模块,缓存结果。

        Returns:
            opentelemetry.trace 模块;未安装时返回 None。
        """
        if not self._otel_checked:
            try:
                from opentelemetry import trace as _trace  # type: ignore[import-not-found]
            except ImportError:
                _trace = None
            self._otel_trace = _trace
            self._otel_checked = True
        return self._otel_trace

    def _active(self) -> bool:
        """是否启用追踪:tracer 已提供且 opentelemetry 可用。"""
        return self._tracer is not None and self._ensure_otel() is not None

    # -- 工作流级 ---------------------------------------------------------
    async def before_workflow(self, wf: "Workflow", ctx: "Context") -> None:
        if not self._active():
            return
        try:
            name = getattr(wf, "name", "workflow")
            self._workflow_span = self._tracer.start_as_current_span(
                f"workflow.{name}"
            )
            # enter 上下文以激活 span;after_workflow 时 __exit__ 退出。
            self._workflow_span.__enter__()
        except Exception:
            # 任何追踪侧异常都不应影响工作流执行。
            self._workflow_span = None

    async def after_workflow(
        self, wf: "Workflow", ctx: "Context", result: "Context"
    ) -> None:
        if self._workflow_span is None:
            return
        try:
            self._workflow_span.__exit__(None, None, None)
        except Exception:
            pass
        finally:
            self._workflow_span = None

    # -- Step 级 ----------------------------------------------------------
    async def before_step(self, step: "BaseStep", ctx: "Context") -> None:
        if not self._active():
            return
        try:
            step_id = getattr(step, "id", "step")
            span = self._tracer.start_as_current_span(f"step.{step_id}")
            span.__enter__()
            self._step_spans[id(step)] = span
        except Exception:
            # 失败时不记录 span,after_step 会跳过。
            self._step_spans.pop(id(step), None)

    async def after_step(
        self, step: "BaseStep", ctx: "Context", trace: "StepTrace"
    ) -> None:
        span = self._step_spans.pop(id(step), None)
        if span is None:
            return
        duration = getattr(trace, "duration_ms", None)
        status = getattr(trace, "status", None)
        try:
            # 设置 span 属性:duration / status。
            if duration is not None:
                try:
                    span.set_attribute("step.duration_ms", duration)
                except Exception:
                    pass
            if status is not None:
                try:
                    span.set_attribute("step.status", str(status))
                except Exception:
                    pass
            # 若为失败态则标记 SPAN_STATUS_ERROR,否则 OK。
            try:
                from opentelemetry.trace import (  # type: ignore[import-not-found]
                    Status,
                    StatusCode,
                )
                status_str = str(status).lower() if status is not None else ""
                if status_str in ("error", "failed", "failure", "fail"):
                    span.set_status(Status(StatusCode.ERROR, status_str))
                else:
                    span.set_status(Status(StatusCode.OK))
            except Exception:
                pass
            span.__exit__(None, None, None)
        except Exception:
            try:
                span.__exit__(None, None, None)
            except Exception:
                pass

    async def on_step_error(
        self, step: "BaseStep", ctx: "Context", error: BaseException
    ) -> ErrorAction:
        span = self._step_spans.get(id(step))
        if span is not None:
            try:
                span.record_exception(error)
            except Exception:
                pass
        return ErrorAction.RAISE


# ---------------------------------------------------------------------------
# MockLLMHooks —— 测试用,记录 LLM 调用
# ---------------------------------------------------------------------------
class MockLLMHooks(LifecycleHooks):
    """测试用 Hook,记录所有 LLM 调用以便断言。

    本 Hook **仅做观测记录**,不会替换真实 LLM 调用——真正的 Mock 由
    ``MockClient`` 等机制完成。``responses`` 队列在每次调用时消费一个元素,
    可用于校验调用顺序与次数(若调用次数多于预设 responses,超出部分不再消费)。

    Args:
        responses: 预设的 LLM 响应序列,每次 ``on_llm_call`` 从头部消费一个
                   (仅作为调用计数/顺序校验,不替换真实响应)。
        record:    外部传入的列表,用于记录每次调用的详细信息。若为 None 则
                   内部新建一个空列表。
    """

    def __init__(
        self,
        responses: Optional[List[Any]] = None,
        record: Optional[List[Any]] = None,
    ) -> None:
        # 复制 responses 避免外部 mutate 影响;为 None 时空列表。
        self._responses: List[Any] = list(responses) if responses is not None else []
        self._record: List[Any] = record if record is not None else []

    @property
    def calls(self) -> List[Any]:
        """返回已记录的 LLM 调用列表(每项为一个 dict)。

        返回内部列表的引用,便于测试直接断言其长度与内容。
        """
        return self._record

    @property
    def remaining_responses(self) -> int:
        """尚未消费的预设响应数,可用于断言"是否所有预期调用都已发生"。"""
        return len(self._responses)

    async def on_llm_call(
        self,
        agent: "AgentConfig",
        prompt: Any,
        response: Any,
        usage: Any,
    ) -> None:
        # 记录调用详情,供测试断言。
        self._record.append(
            {
                "agent": agent,
                "prompt": prompt,
                "response": response,
                "usage": usage,
            }
        )
        # 消费一个预设响应(若有);仅作为调用计数/顺序校验,不替换真实 response。
        if self._responses:
            self._responses.pop(0)


# ---------------------------------------------------------------------------
# MockToolHooks —— 测试用,记录工具调用
# ---------------------------------------------------------------------------
class MockToolHooks(LifecycleHooks):
    """测试用 Hook,记录所有工具调用以便断言,并标记被 mock 的工具。

    本 Hook **仅做观测记录**,不会替换真实工具结果——真正替换工具结果由
    Step 配置侧 mock 完成。当工具名命中 ``overrides`` 时,记录中标记 ``mocked=True``,
    便于测试断言"该工具调用本应被 mock"。

    Args:
        overrides: 工具名 → 预设结果的映射;命中时记录中标记 mocked。
        record:    外部传入的列表;为 None 则内部新建。
    """

    def __init__(
        self,
        overrides: Optional[dict[str, Any]] = None,
        record: Optional[List[Any]] = None,
    ) -> None:
        self._overrides: dict[str, Any] = (
            dict(overrides) if overrides is not None else {}
        )
        self._record: List[Any] = record if record is not None else []

    @property
    def calls(self) -> List[Any]:
        """返回已记录的工具调用列表(每项为一个 dict)。"""
        return self._record

    async def on_tool_call(
        self, tool: Any, params: dict, result: Any
    ) -> None:
        name = getattr(tool, "name", None)
        mocked = name in self._overrides
        self._record.append(
            {
                "tool": tool,
                "name": name,
                "params": params,
                "result": result,
                "mocked": mocked,
            }
        )


# ---------------------------------------------------------------------------
# CompositeHooks —— 聚合多个 hooks
# ---------------------------------------------------------------------------
class CompositeHooks(LifecycleHooks):
    """聚合多个 :class:`LifecycleHooks`,依次调用每个 hook 的同名方法。

    让 Workflow 只需持有一个 hooks 对象即可分发到任意数量的具体 hook。
    所有回调按构造时传入的顺序依次 ``await``。

    ``on_step_error`` 的特殊语义:收集所有 hook 返回的 :class:`ErrorAction`,
    返回**第一个非 RAISE** 的值;若全部为 RAISE,则返回 RAISE。
    这意味着任意一个 hook 请求 RETRY/SKIP/DEFAULT,Composite 即采纳该策略;
    仅当所有 hook 都同意 RAISE 时才向上抛异常。

    Args:
        hooks: 待聚合的 LifecycleHooks 可迭代对象。
    """

    def __init__(self, hooks: Iterable[LifecycleHooks]) -> None:
        self.hooks: List[LifecycleHooks] = list(hooks)

    async def before_workflow(self, wf: "Workflow", ctx: "Context") -> None:
        for h in self.hooks:
            await h.before_workflow(wf, ctx)

    async def after_workflow(
        self, wf: "Workflow", ctx: "Context", result: "Context"
    ) -> None:
        for h in self.hooks:
            await h.after_workflow(wf, ctx, result)

    async def before_step(self, step: "BaseStep", ctx: "Context") -> None:
        for h in self.hooks:
            await h.before_step(step, ctx)

    async def after_step(
        self, step: "BaseStep", ctx: "Context", trace: "StepTrace"
    ) -> None:
        for h in self.hooks:
            await h.after_step(step, ctx, trace)

    async def on_step_error(
        self, step: "BaseStep", ctx: "Context", error: BaseException
    ) -> ErrorAction:
        # 所有 hook 都要被通知(便于日志/记录),但返回第一个非 RAISE 的策略。
        action = ErrorAction.RAISE
        for h in self.hooks:
            a = await h.on_step_error(step, ctx, error)
            if action is ErrorAction.RAISE and a is not ErrorAction.RAISE:
                action = a
        return action

    async def on_llm_call(
        self,
        agent: "AgentConfig",
        prompt: Any,
        response: Any,
        usage: Any,
    ) -> None:
        for h in self.hooks:
            await h.on_llm_call(agent, prompt, response, usage)

    async def on_llm_stream_start(
        self,
        step: "BaseStep",
        agent: "AgentConfig",
        *,
        attempt: int = 0,
    ) -> None:
        for h in self.hooks:
            await h.on_llm_stream_start(step, agent, attempt=attempt)

    async def on_llm_stream_delta(
        self,
        step: "BaseStep",
        agent: "AgentConfig",
        delta: str,
        accumulated: str,
        *,
        attempt: int = 0,
        delta_reasoning: str | None = None,
    ) -> None:
        for h in self.hooks:
            await h.on_llm_stream_delta(
                step, agent, delta, accumulated, attempt=attempt,
                delta_reasoning=delta_reasoning,
            )

    async def on_llm_stream_end(
        self,
        step: "BaseStep",
        agent: "AgentConfig",
        full_content: str,
        *,
        attempt: int = 0,
    ) -> None:
        for h in self.hooks:
            await h.on_llm_stream_end(step, agent, full_content, attempt=attempt)

    async def on_tool_call(
        self, tool: Any, params: dict, result: Any
    ) -> None:
        for h in self.hooks:
            await h.on_tool_call(tool, params, result)

    async def on_mcp_call(
        self, server: str, tool: str, params: dict, result: Any
    ) -> None:
        for h in self.hooks:
            await h.on_mcp_call(server, tool, params, result)


__all__ = [
    "ErrorAction",
    "LifecycleHooks",
    "LoggingHooks",
    "TokenAccountingHooks",
    "TracingHooks",
    "MockLLMHooks",
    "MockToolHooks",
    "CompositeHooks",
]
