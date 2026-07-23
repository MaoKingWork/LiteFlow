"""runtime.event_hooks —— EventBusHooks：hook→事件翻译。

:class:`EventBusHooks` 是 :class:`~agentkit.core.hooks.LifecycleHooks` 的子类，
把现有 hook 回调一对一翻译为 :class:`~agentkit.runtime.event.RunEvent`，经
:class:`~agentkit.runtime.event.EventBus` 分发。

设计要点（对齐 ``docs/visualization-design.md`` §5.2）：
    - **执行引擎零改动**：经
      ``Workflow(hooks=CompositeHooks([..., EventBusHooks(bus, run_id)]))`` 接入
    - **日志先于分发**：``EventBus.publish`` 内部先 ``EventLog.append`` 再入队；
      本 Hook 只调 ``publish``，不直接写日志
    - **零侵入**：不挂本 Hook 时 hooks 链不变；挂上后除事件分发外无副作用
    - **字段对齐**：``step_finished`` payload 直接对齐 :class:`StepTrace` 字段
      （status / duration_ms / token_usage / error / retry_count / output_summary /
      tool_calls），前端无需二次映射

事件类型映射（对齐 §5.1 事件类型全集表）：

    +-------------------------+---------------------+------------------------------+
    | Hook 方法               | 翻译为事件          | payload 关键字段             |
    +=========================+=====================+==============================+
    | before_workflow         | run_started         | {workflow_name}              |
    +-------------------------+---------------------+------------------------------+
    | after_workflow          | run_completed /     | {status, error}              |
    |                         | run_failed          |                              |
    +-------------------------+---------------------+------------------------------+
    | before_step             | step_started        | {step_type}                  |
    +-------------------------+---------------------+------------------------------+
    | after_step              | step_finished       | StepTrace 字段全集           |
    +-------------------------+---------------------+------------------------------+
    | on_llm_stream_start     | llm_stream_start    | {agent_name, model}          |
    +-------------------------+---------------------+------------------------------+
    | on_llm_stream_delta     | llm_delta           | {delta, accumulated_len,     |
    |                         |                     |  delta_reasoning}            |
    +-------------------------+---------------------+------------------------------+
    | on_llm_stream_end       | llm_stream_end      | {full_len}                   |
    +-------------------------+---------------------+------------------------------+
    | on_tool_call            | tool_call           | {name, params_summary,       |
    |                         |                     |  result_summary, status}     |
    +-------------------------+---------------------+------------------------------+
    | on_mcp_call             | tool_call           | 同上 + mcp_server            |
    +-------------------------+---------------------+------------------------------+

**不在 hooks 路径的事件**：
    - ``run_created``：由 RunManager（P1）在调度前发
    - ``run_cancelling`` / ``run_cancelled``：由 RunManager 在 cancel() 时发
    - ``run_interrupted``：由 Reconciler（P1）发

P0 阶段 EventLog 已落盘，P1 的 RunManager / Reconciler 可直接复用 EventBus 与
EventLog API，无需改动本模块。

公开 API：
    - EventBusHooks: LifecycleHooks 子类
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentkit.core.hooks import ErrorAction, LifecycleHooks
from agentkit.runtime.event import EventBus, EventType, RunEvent

if TYPE_CHECKING:
    from agentkit.core.agent import AgentConfig
    from agentkit.core.context import Context
    from agentkit.core.workflow import Workflow
    from agentkit.steps.base import BaseStep, StepTrace

__all__ = ["EventBusHooks"]


# ---------------------------------------------------------------------------
# 辅助：摘要工具（与 BaseStep._summarize 对齐，独立实现避免循环依赖）
# ---------------------------------------------------------------------------
def _summarize(value: Any, max_len: int = 200) -> str:
    """将任意值转为截断的 repr 字符串，供事件 payload 摘要。

    与 :meth:`BaseStep._summarize` 行为一致，独立实现以避免 runtime → steps 循环依赖。
    """
    try:
        text = repr(value)
    except Exception as e:
        text = f"<unrepr {type(value).__name__}: {e}>"
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


# ---------------------------------------------------------------------------
# EventBusHooks —— hook→事件翻译
# ---------------------------------------------------------------------------
class EventBusHooks(LifecycleHooks):
    """把生命周期 hook 一对一翻译为 :class:`RunEvent`，经 :class:`EventBus` 分发。

    :class:`~agentkit.core.workflow.Workflow` 经
    ``Workflow(hooks=CompositeHooks([..., EventBusHooks(bus, run_id)]))`` 接入。
    执行引擎零改动；本 Hook 除事件分发外无副作用（不读 Context 内部、不修改 trace）。

    **零侵入**：不挂本 Hook 时 hooks 链不变，行为完全同现状。

    Args:
        bus:    目标事件总线。
        run_id: 所属 run id（用于填充 ``RunEvent.run_id``；bus.run_id 一致时冗余）。
    """

    def __init__(self, bus: EventBus, run_id: str = "") -> None:
        self._bus: EventBus = bus
        self._run_id: str = run_id or bus.run_id
        # 运行级状态追踪：用于 after_workflow 判断 completed vs failed
        self._workflow_failed: bool = False
        self._workflow_error: str | None = None

    # ------------------------------------------------------------------
    # 运行级
    # ------------------------------------------------------------------
    async def before_workflow(self, wf: "Workflow", ctx: "Context") -> None:
        """工作流开始 → ``run_started`` 事件。"""
        await self._bus.publish(RunEvent(
            run_id=self._run_id,
            type=EventType.RUN_STARTED,
            payload={"workflow_name": getattr(wf, "name", "")},
        ))

    async def after_workflow(
        self, wf: "Workflow", ctx: "Context", result: "Context"
    ) -> None:
        """工作流结束 → ``run_completed`` 或 ``run_failed`` 事件。

        依据 :attr:`_workflow_failed` 标志判断；标志由 :meth:`on_step_error`
        在 RAISE 策略时设置。``result`` 为最终 Context，此处只读其状态字段。
        """
        if self._workflow_failed:
            await self._bus.publish(RunEvent(
                run_id=self._run_id,
                type=EventType.RUN_FAILED,
                payload={
                    "status": "failed",
                    "error": self._workflow_error or "unknown error",
                },
            ))
        else:
            await self._bus.publish(RunEvent(
                run_id=self._run_id,
                type=EventType.RUN_COMPLETED,
                payload={"status": "completed"},
            ))

    # ------------------------------------------------------------------
    # Step 级
    # ------------------------------------------------------------------
    async def before_step(self, step: "BaseStep", ctx: "Context") -> None:
        """Step 开始 → ``step_started`` 事件。"""
        await self._bus.publish(RunEvent(
            run_id=self._run_id,
            type=EventType.STEP_STARTED,
            step_id=getattr(step, "id", None) or getattr(step, "type", None),
            payload={"step_type": getattr(step, "type", "")},
        ))

    async def after_step(
        self, step: "BaseStep", ctx: "Context", trace: "StepTrace"
    ) -> None:
        """Step 结束 → ``step_finished`` 事件，payload 对齐 :class:`StepTrace` 字段。"""
        step_id = getattr(step, "id", None) or getattr(step, "type", None)
        # StepTrace 字段全集对齐（前端无需二次映射）
        payload: dict[str, Any] = {
            "step_type": getattr(step, "type", ""),
            "status": getattr(trace, "status", ""),
            "duration_ms": getattr(trace, "duration_ms", None),
            "token_usage": getattr(trace, "token_usage", None),
            "error": getattr(trace, "error", None),
            "retry_count": getattr(trace, "retry_count", 0),
            "input_summary": getattr(trace, "input_summary", ""),
            "output_summary": getattr(trace, "output_summary", ""),
            "tool_calls": getattr(trace, "tool_calls", []) or [],
        }
        await self._bus.publish(RunEvent(
            run_id=self._run_id,
            type=EventType.STEP_FINISHED,
            step_id=step_id,
            payload=payload,
        ))

    async def on_step_error(
        self, step: "BaseStep", ctx: "Context", error: BaseException
    ) -> ErrorAction:
        """Step 异常 → 记录失败标志（不直接发事件，由 after_step 的 failed 状态承载）。

        返回 :attr:`ErrorAction.RAISE` 保持默认语义（不干预重试决策）。
        :meth:`after_workflow` 据本方法设置的标志发 ``run_failed``。
        """
        self._workflow_failed = True
        self._workflow_error = f"{type(error).__name__}: {error}"
        return ErrorAction.RAISE

    # ------------------------------------------------------------------
    # LLM 流式
    # ------------------------------------------------------------------
    async def on_llm_stream_start(
        self,
        step: "BaseStep",
        agent: "AgentConfig",
        *,
        attempt: int = 0,
    ) -> None:
        """流式开始 → ``llm_stream_start`` 事件（前端据此重置缓冲）。"""
        await self._bus.publish(RunEvent(
            run_id=self._run_id,
            type=EventType.LLM_STREAM_START,
            step_id=getattr(step, "id", None) or getattr(step, "type", None),
            attempt=attempt,
            payload={
                "agent_name": getattr(agent, "name", ""),
                "model": getattr(agent, "model", ""),
            },
        ))

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
        """流式增量 → ``llm_delta`` 事件（可合并级，50ms 窗口合并）。

        payload 不带 ``accumulated`` 全文，只带 ``accumulated_len``，防背压。
        """
        await self._bus.publish(RunEvent(
            run_id=self._run_id,
            type=EventType.LLM_DELTA,
            step_id=getattr(step, "id", None) or getattr(step, "type", None),
            attempt=attempt,
            payload={
                "delta": delta,
                "accumulated_len": len(accumulated),
                "delta_reasoning": delta_reasoning,
            },
        ))

    async def on_llm_stream_end(
        self,
        step: "BaseStep",
        agent: "AgentConfig",
        full_content: str,
        *,
        attempt: int = 0,
    ) -> None:
        """流式结束 → ``llm_stream_end`` 事件。"""
        await self._bus.publish(RunEvent(
            run_id=self._run_id,
            type=EventType.LLM_STREAM_END,
            step_id=getattr(step, "id", None) or getattr(step, "type", None),
            attempt=attempt,
            payload={"full_len": len(full_content)},
        ))

    # ------------------------------------------------------------------
    # 工具调用
    # ------------------------------------------------------------------
    async def on_tool_call(
        self, tool: Any, params: dict, result: Any
    ) -> None:
        """工具调用完成 → ``tool_call`` 事件（params/result 仅记摘要）。"""
        name = getattr(tool, "name", str(tool))
        # status 推断：result 含 error 字段视为失败
        status = "error" if isinstance(result, dict) and "error" in result else "ok"
        await self._bus.publish(RunEvent(
            run_id=self._run_id,
            type=EventType.TOOL_CALL,
            payload={
                "name": name,
                "params_summary": _summarize(params),
                "result_summary": _summarize(result),
                "status": status,
            },
        ))

    async def on_mcp_call(
        self, server: str, tool: str, params: dict, result: Any
    ) -> None:
        """MCP 工具调用完成 → ``tool_call`` 事件（payload 加 ``mcp_server``）。"""
        status = "error" if isinstance(result, dict) and "error" in result else "ok"
        await self._bus.publish(RunEvent(
            run_id=self._run_id,
            type=EventType.TOOL_CALL,
            payload={
                "name": tool,
                "mcp_server": server,
                "params_summary": _summarize(params),
                "result_summary": _summarize(result),
                "status": status,
            },
        ))
