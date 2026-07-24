"""steps.base —— Step 抽象基类、执行轨迹与类型注册表。

本模块定义 AgentKit 中所有 Step 类型的抽象基类 :class:`BaseStep`,以及执行
轨迹 :class:`StepTrace` 与类型注册表 :class:`StepRegistry`。

6 种内置 Step 类型(LLMStep / ToolStep / SkillStep / ParallelStep /
ConditionStep / LoopStep)均继承 :class:`BaseStep`,实现 ``run(ctx) -> ctx``
完成各自核心逻辑;通用执行编排(钩子触发 / 超时 / 执行级重试 / trace 记录)
集中在 :meth:`BaseStep.execute` 中,子类一般无需重写。

设计原则:
    - 高度模块化:运行时仅依赖 ``agentkit.config`` 与 ``ErrorAction`` 枚举;
      ``Context`` / ``LifecycleHooks`` 等类型仅用于注解,通过 ``TYPE_CHECKING``
      守卫,避免循环依赖。不依赖 template 或其他 step 子模块。
    - 可拓展:新增 Step 类型只需继承 :class:`BaseStep` 重写 ``run``,并用
      :func:`register_step` 装饰器注册,即可在 YAML 中按 ``type`` 引用。
    - 类型注解完整,中文 docstring 与注释。

执行级重试 vs 输出契约重试:
    :meth:`BaseStep.execute` 的重试针对 ``run`` 抛出的异常(网络/瞬时错误),
    按 :class:`RetryPolicy` 退避重试。LLMStep 等子类的"输出契约重试"(如
    解析失败重试)在 ``run`` 内部实现,不走 execute 的执行级重试。
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentkit.config import RetryPolicy, get_default
# ErrorAction 为纯枚举,运行时导入不会触发 hooks 模块的重型类加载,
# 也不会造成循环依赖(hooks 仅在 TYPE_CHECKING 下导入 steps.base)。
from agentkit.core.hooks import ErrorAction
from agentkit.core.ports import (
    MISSING,
    ClosedScopeContext,
    InputPort,
    OutputPort,
    PortBindingError,
    PortScopeContext,
    PortType,
    PortTypeError,
)

if TYPE_CHECKING:
    # 仅用于类型注解,运行时不导入,避免循环依赖。
    from agentkit.core.cancel import CancelToken
    from agentkit.core.context import Context
    from agentkit.core.hooks import LifecycleHooks
    from agentkit.llm.base import LLMClient
    from agentkit.mcp.manager import MCPManager


# ---------------------------------------------------------------------------
# _compute_backoff —— 退避时长计算
# ---------------------------------------------------------------------------
def _compute_backoff(policy: RetryPolicy | None, attempt: int) -> float:
    """根据重试策略计算退避秒数。

    Args:
        policy:  重试策略;为 None 时返回 0(不退避)。
        attempt: 刚失败的尝试序号(从 0 计),用于 exponential 倍增。

    Returns:
        float: 退避秒数。

    Notes:
        - ``exponential``: ``base_seconds * (2 ** attempt)`` —— 第 0 次失败后
          退避 ``base``,第 1 次失败后退避 ``base*2``,依此类推。
        - ``fixed`` 或未知策略: 恒为 ``base_seconds``。
    """
    if policy is None:
        return 0.0
    if policy.backoff == "exponential":
        return policy.base_seconds * (2 ** attempt)
    # fixed 或未知策略均视为 fixed
    return policy.base_seconds


# ---------------------------------------------------------------------------
# StepTrace —— 执行轨迹
# ---------------------------------------------------------------------------
@dataclass
class StepTrace:
    """Step 执行轨迹(可观测性 + 检查点)。

    每次 :meth:`BaseStep.execute` 调用产生一条 :class:`StepTrace`,经
    :meth:`Context.add_trace` 写入上下文,供可观测性查看与检查点持久化。

    Attributes:
        step_id:        对应 Step 的 ``id``。
        status:         执行结果,``success`` / ``failed`` / ``skipped``。
        duration_ms:    执行耗时(毫秒)。
        input_summary:  输入摘要(子类可填充)。
        output_summary: 输出摘要(execute 在 output 已写入时填充)。
        token_usage:    Token 用量(LLMStep 等可填充)。
        tool_calls:     本次执行内的工具调用记录(子类可填充)。
        error:          失败/跳过时的异常字符串。
        retry_count:    实际重试次数(不含首次执行)。
    """

    step_id: str
    status: str = "success"
    duration_ms: float | None = None
    input_summary: str = ""
    output_summary: str = ""
    token_usage: int | None = None
    tool_calls: list[dict] = field(default_factory=list)
    error: str | None = None
    retry_count: int = 0


# ---------------------------------------------------------------------------
# BaseStep —— Step 抽象基类
# ---------------------------------------------------------------------------
class BaseStep(ABC):
    """Step 抽象基类。

    所有 Step 类型继承本类并实现 :meth:`run`;通用执行编排集中在
    :meth:`execute`,子类一般无需重写 ``execute``。

    子类约定:
        - ``run`` 应自行把结果通过 ``ctx.set(self.output, result)`` 写入上下文,
          并返回 ``ctx``。``execute`` 不重复 set(但 SKIP/DEFAULT 兜底会 set None)。
        - ``run`` 抛出的异常(网络/瞬时错误)由 ``execute`` 按重试策略重试;
          输出契约重试(如解析失败重试)在 ``run`` 内部实现。

    Attributes:
        type:    Step 类型标识(子类覆盖),用于注册表与 YAML 引用。
        id:      Step 实例标识(用于 trace / 日志)。
        output:  输出键名;``run`` 应将结果 ``ctx.set(self.output, ...)``。
        retry:   实例级重试策略;为 None 时由 ``execute`` 的 ``retry_policy``
                 参数决定。
        timeout: 实例级超时秒数;为 None 时使用全局默认。
    """

    # 类属性:Step 类型标识。子类应覆盖(如 ``"llm"`` / ``"tool"`` 等)。
    type: str = "base"

    # MCP 管理器引用;由 Workflow 在连接完成后经 bind_mcp_manager 注入。
    # LLMStep 据此把 MCP 发现的工具名注入 agent.tools;None 表示无 MCP 能力。
    mcp_manager: "MCPManager | None" = None

    # LLM 客户端引用;由 Workflow 经 bind_llm_client 注入(优先于全局默认)。
    # LLMStep / SkillStep 据此取客户端,避免每个 Step 自行管理连接生命周期。
    # None 表示未注入,LLMStep 会回落到 agentkit.llm.get_default_client()。
    llm_client: "LLMClient | None" = None

    # 阻塞执行器引用;由 Workflow 经 bind_blocking_executor 注入(可选)。
    # ToolStep 据此分派 inline / thread / process 调用;None 表示未注入,
    # ToolStep 会回落到 agentkit.runtime.get_blocking_executor() 全局单例。
    # 详见 docs/visualization-design.md §5.5。
    _blocking_executor: Any = None

    def __init__(
        self,
        id: str = "",
        output: str | None = None,
        retry: RetryPolicy | None = None,
        timeout: float | None = None,
        *,
        inputs: list[InputPort] | None = None,
        outputs: list[OutputPort] | None = None,
        strict_scope: bool = False,
    ) -> None:
        self.id: str = id
        self.retry: RetryPolicy | None = retry
        self.timeout: float | None = timeout
        # 端口系统：output 是 outputs 的语法糖；二者不可同时声明
        self.inputs: list[InputPort] = list(inputs) if inputs else []
        self.outputs: list[OutputPort] = []
        if output and outputs:
            raise ValueError(
                f"Step {id!r} 的 output 与 outputs 不可同时声明"
            )
        if output:
            self.outputs = [OutputPort(name=output)]
        elif outputs:
            self.outputs = list(outputs)
        # strict_scope: True 时封闭输入作用域（切断全局 Context 回退）
        self.strict_scope: bool = strict_scope
        # 运行期 hooks 引用:由 execute 在调用 run 前设置,供子类(如 LLMStep)
        # 在 run 内部触发 on_llm_call / on_tool_call 等细粒度钩子。
        self._hooks: "LifecycleHooks | None" = None
        # 运行期取消令牌:由 execute 在调用 run 前设置,供容器型 Step(Loop /
        # Parallel / Condition)在内部边界检查,实现 graceful 取消。
        self._cancel_token: "CancelToken | None" = None
        # 运行期端口绑定:由 _bind_inputs 在 run 前填充,供 _render 使用。
        self._port_bindings: dict[str, Any] = {}

    @property
    def output(self) -> str | None:
        """单输出端口的 name（向后兼容）；多输出或无输出时为 None。"""
        if len(self.outputs) == 1:
            return self.outputs[0].name
        return None

    def bind_mcp_manager(self, manager: "MCPManager | None") -> None:
        """注入 MCP 管理器引用。

        由 Workflow 在 ``connect_all`` 后调用。默认仅存到 ``self.mcp_manager``;
        容器型 Step(Loop / Parallel / Condition)重写以递归传播给子 Step,
        确保任意嵌套层级的 LLMStep / SkillStep 都能拿到 MCP 工具。

        Args:
            manager: MCPManager 实例;None 表示该工作流不使用 MCP。
        """
        self.mcp_manager = manager

    def bind_llm_client(self, client: "LLMClient | None") -> None:
        """注入 LLM 客户端引用。

        由 Workflow 在执行前调用。默认仅存到 ``self.llm_client``;容器型 Step
        重写以递归传播给子 Step。LLMStep 取客户端时优先用此值,为 None 时
        回落到全局默认客户端。注入的客户端生命周期由 Workflow 统一管理
        (见 :class:`~agentkit.core.workflow.Workflow` 的 ``owns_llm_client``)。

        Args:
            client: LLMClient 实例;None 表示不注入(用全局默认)。
        """
        self.llm_client = client

    def bind_blocking_executor(self, executor: Any) -> None:
        """注入 :class:`~agentkit.runtime.blocking.BlockingExecutor` 引用(可选)。

        由 Workflow 在执行前调用(可选,P0 阶段 Workflow 不主动调用,留扩展点)。
        默认仅存到 ``self._blocking_executor``;容器型 Step 重写以递归传播给
        子 Step。ToolStep 取执行器时优先用此值,为 None 时回落到全局单例
        :func:`agentkit.runtime.blocking.get_blocking_executor`。

        未来多工作流并行隔离场景可经此方法注入 per-Workflow 的执行器实例;
        P0 阶段不注入,所有 ToolStep 共享全局单例,行为等价。

        Args:
            executor: BlockingExecutor 实例;None 表示不注入(用全局单例)。
        """
        self._blocking_executor = executor

    def iter_child_steps(self) -> list["BaseStep"]:
        """返回直接子 Step 列表。叶子 Step 返回空列表。

        组合型 Step(Loop / Parallel / Condition 等)重写此方法,
        返回其持有的子 Step。静态校验、可视化等通用遍历逻辑
        只需调用此方法递归,无需按类型 case-by-case 硬编码。
        """
        return []

    @abstractmethod
    async def run(self, ctx: "Context") -> "Context":
        """执行核心逻辑(子类实现)。

        约定流程:解析 input → 执行 → 按 ``self.output`` 写 ctx → 返回 ctx。

        Args:
            ctx: 当前上下文(只读视图,修改需 ``copy.deepcopy`` 后 ``set``)。

        Returns:
            Context: 同一上下文(执行编排不依赖返回值,但保留约定)。

        Raises:
            Exception: 任何异常将被 :meth:`execute` 捕获并按重试策略处理。
        """

    async def execute(
        self,
        ctx: "Context",
        hooks: "LifecycleHooks | None" = None,
        *,
        retry_policy: RetryPolicy | None = None,
        cancel_token: "CancelToken | None" = None,
    ) -> StepTrace:
        """通用执行编排:钩子 / 超时 / 执行级重试 / trace 记录。

        编排流程:
            1. 记录起始时间,创建 :class:`StepTrace`。
            2. 触发 ``before_step`` 钩子(若有)。
            3. 解析生效重试策略与最大尝试次数。
            4. 按尝试次数循环执行 ``run``:成功则记录并退出;失败则按退避
               策略重试,直到耗尽。
            5. 重试耗尽时调用 ``on_step_error`` 钩子决定后续行为
               (RAISE / RETRY / SKIP / DEFAULT)。
            6. 记录耗时,写入 trace 到 ctx,触发 ``after_step`` 钩子。

        Args:
            ctx:          当前上下文。
            hooks:        生命周期钩子(可选)。
            retry_policy: 调用方传入的重试策略;与 ``self.retry`` 的优先级为
                          ``self.retry`` 优先,为 None 时回落到本参数。
            cancel_token: 协作式取消令牌(可选);容器型 Step 在内部边界检查,
                          叶子 Step 仅透传不检查。

        Returns:
            StepTrace: 本次执行的轨迹。

        Raises:
            Exception: 当 ``on_step_error`` 返回 RAISE(或无 hooks 默认 RAISE)
                       且重试耗尽时,重新抛出最后一次异常。
        """
        t0 = time.perf_counter()
        trace = StepTrace(step_id=self.id)

        # 1) before_step 钩子
        if hooks:
            await hooks.before_step(self, ctx)

        # 1b) 暴露 hooks / cancel_token 给子类 run 使用
        self._hooks = hooks
        self._cancel_token = cancel_token

        # 1c) 输入端口绑定：校验来源值存在性与类型（声明端口时生效）
        self._bind_inputs(ctx)

        # 2) 解析重试策略:max_attempts = 首次执行 + 重试次数;无策略时仅执行 1 次
        policy = self.effective_retry(retry_policy)
        max_attempts = (policy.count + 1) if policy else 1

        # 3) 执行级重试循环
        last_error: BaseException | None = None
        success = False
        for attempt in range(max_attempts):
            try:
                await asyncio.wait_for(
                    self.run(ctx), timeout=self.effective_timeout()
                )
                # 成功:校验输出端口（声明端口时生效）
                self._validate_outputs(ctx)
                # 成功:记录状态与重试次数,退出循环
                trace.status = "success"
                trace.retry_count = attempt
                success = True
                break
            except asyncio.TimeoutError as e:
                # 超时单独捕获,便于后续按需区分(此处与一般异常处理一致)
                last_error = e
            except Exception as e:
                # 捕获 run 抛出的任意异常(网络/瞬时错误等)
                # 注:CancelledError 是 BaseException 子类,不会被捕获,符合预期
                last_error = e
            # 若仍有重试机会,退避后继续
            if attempt < max_attempts - 1:
                backoff = _compute_backoff(policy, attempt)
                if backoff > 0:
                    await asyncio.sleep(backoff)
                trace.retry_count += 1
                continue
            # 否则(已是最后一次尝试):循环自然结束,进入下方错误处理

        # 4) 错误处理(仅当所有尝试均失败时)
        if not success:
            assert last_error is not None  # 仅在失败时进入此分支,断言保护
            action = (
                await hooks.on_step_error(self, ctx, last_error)
                if hooks
                else ErrorAction.RAISE
            )
            if action is ErrorAction.RAISE or action is ErrorAction.RETRY:
                # RAISE: 直接抛出。
                # RETRY: 钩子要求额外重试;但为防无限循环,RETRY 也消耗 max_attempts。
                #        此处错误处理仅在常规重试耗尽后触发,故 max_attempts 必已耗尽,
                #        RETRY 等价于 RAISE —— 直接抛出异常使工作流失败。
                #        (若未来需支持"on_step_error 触发的额外重试",应在 max_attempts
                #        之外单独维护 RETRY 配额,并在此分支实现额外重试循环。)
                trace.status = "failed"
                trace.error = str(last_error)
                trace.duration_ms = (time.perf_counter() - t0) * 1000
                self._enrich_trace(trace)
                ctx.add_trace(trace)
                if hooks:
                    await hooks.after_step(self, ctx, trace)
                raise last_error
            elif action is ErrorAction.SKIP:
                # 跳过本 Step,继续下一步;单输出端口未写入时填 None 兜底
                # 多输出端口不自动填，保持失败语义清晰
                trace.status = "skipped"
                trace.error = str(last_error)
                if self.output and not ctx.has(self.output):
                    ctx.set(self.output, None)
            elif action is ErrorAction.DEFAULT:
                # 标记失败但用默认值填充单输出端口,继续下一步
                trace.status = "failed"
                if self.output:
                    ctx.set(self.output, None)

        # 5) 收尾:记录耗时、填充输出摘要、写入 trace、触发 after_step
        trace.duration_ms = (time.perf_counter() - t0) * 1000
        if self.output and ctx.has(self.output):
            trace.output_summary = self._summarize(ctx.get(self.output))
        self._enrich_trace(trace)
        ctx.add_trace(trace)
        if hooks:
            await hooks.after_step(self, ctx, trace)
        return trace

    # ---- 策略解析 ------------------------------------------------------
    def effective_retry(self, default: RetryPolicy | None = None) -> RetryPolicy | None:
        """返回生效的重试策略。

        优先使用实例级 ``self.retry``;为 None 时回落到 ``default``。

        Args:
            default: 调用方提供的回退策略(通常来自 Workflow / Agent 配置)。

        Returns:
            RetryPolicy | None: 生效策略;均为 None 时返回 None。
        """
        return self.retry if self.retry is not None else default

    def effective_timeout(self) -> float:
        """返回生效的超时秒数。

        优先使用实例级 ``self.timeout``;为 None 时使用全局默认
        ``default_step_timeout_seconds``。

        Returns:
            float: 超时秒数。
        """
        if self.timeout is not None:
            return self.timeout
        return float(get_default("default_step_timeout_seconds"))

    # ---- 工具方法 ------------------------------------------------------
    def _summarize(self, value: Any, max_len: int = 200) -> str:
        """将任意值转为截断的 repr 字符串,供 trace 摘要。

        Args:
            value:   任意对象。
            max_len: 最大字符长度,超出则截断并追加 ``"..."``。

        Returns:
            str: 截断后的 repr 字符串。
        """
        try:
            text = repr(value)
        except Exception as e:
            # repr 失败时退化为类型 + 异常信息,避免 summarization 自身抛错
            text = f"<unrepr {type(value).__name__}: {e}>"
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text

    def _enrich_trace(self, trace: StepTrace) -> None:
        """填充 trace 的扩展字段(子类可重写)。

        默认空实现。子类(如 LLMStep)在 ``run`` 期间把 token 用量、工具调用
        记录等暂存到实例属性,本方法在 ``execute`` 写入 trace 前被调用,将其
        回填到 trace,供可观测性与检查点使用。这样 ``run`` 无需直接接触
        ``execute`` 私有的 trace 对象。

        Args:
            trace: 当前执行的轨迹(已填入 status / duration_ms 等基础字段)。
        """
        # 默认无操作;子类按需重写。
        return None

    # ---- 端口系统 ------------------------------------------------------
    def _bind_inputs(self, ctx: "Context") -> None:
        """输入端口绑定：校验来源值存在性与类型，构造端口绑定字典。

        遍历 ``self.inputs``，按 ``from``（默认 ``name``）从 ctx 取值：
            - required=True 且缺失 → 抛 PortBindingError。
            - required=False 且缺失 → 用 default（未设则 None）。
            - 声明了 type → PortType.validate 校验（strict 默认 True 拒绝隐式转换）。

        绑定结果存到 ``self._port_bindings``，供 ``_render`` 使用。
        不声明 inputs 时为空字典，_render 退化为直接用 ctx。
        """
        from agentkit.core.context import to_mutable

        bindings: dict[str, Any] = {}
        for port in self.inputs:
            source = port.source
            if ctx.has(source):
                # ctx.get 返回冻结视图，转回可变类型供模板解析与类型校验
                value = to_mutable(ctx.get(source))
            else:
                if port.required:
                    raise PortBindingError(
                        f"Step {self.id!r} 的输入端口 {port.name!r} "
                        f"(来源 {source!r}) 缺失且 required=True"
                    )
                value = port.default if port.default is not MISSING else None
            if port.type is not None:
                value = port.type.validate(value, strict=port.strict)
            bindings[port.name] = value
        self._port_bindings = bindings

    def _validate_outputs(self, ctx: "Context") -> None:
        """输出端口校验：检查 required 产出与类型。

        遍历 ``self.outputs``：
            - required=True 且 ctx 无 name → 抛 PortBindingError。
            - 声明了 type → PortType.validate 校验（先 to_mutable 解冻）。
        """
        from agentkit.core.context import to_mutable

        for port in self.outputs:
            if not ctx.has(port.name):
                if port.required:
                    raise PortBindingError(
                        f"Step {self.id!r} 的输出端口 {port.name!r} "
                        f"未产出且 required=True"
                    )
                # 非 required：填 None 兜底
                ctx.set(port.name, None)
                continue
            if port.type is not None:
                # ctx.get 返回冻结视图（FrozenDict/tuple），转回可变类型再校验
                value = to_mutable(ctx.get(port.name))
                port.type.validate(value, strict=port.strict)

    def _get_render_scope(self, ctx: "Context") -> Any:
        """构造模板解析的作用域 context。

        - 无端口绑定：返回原 ctx（行为同现状）。
        - strict_scope=True：返回 ClosedScopeContext（仅允许端口变量）。
        - 否则：返回 PortScopeContext（端口优先，其余透传父 ctx）。
        """
        if not self._port_bindings:
            return ctx
        if self.strict_scope:
            return ClosedScopeContext(self._port_bindings)
        return PortScopeContext(ctx, self._port_bindings)

    def _render(self, template: Any, ctx: "Context") -> Any:
        """模板解析（对应 resolve_value）：叠加端口作用域。

        整体单 ``{{var}}`` 返回原始对象；否则返回拼接 str。
        不声明端口时退化为 ``resolve_value(template, ctx)``，行为完全同现状。
        """
        from agentkit.core.template import resolve_value

        scope = self._get_render_scope(ctx)
        return resolve_value(template, scope)

    def _render_str(self, template: str, ctx: "Context") -> str:
        """模板解析（对应 resolve_template）：始终返回 str，叠加端口作用域。

        不声明端口时退化为 ``resolve_template(template, ctx)``。
        """
        from agentkit.core.template import resolve_template

        scope = self._get_render_scope(ctx)
        return resolve_template(template, scope)

    def _emit_dict_outputs(self, ctx: "Context", result: dict) -> None:
        """多输出端口拆分：把 dict 结果按端口 name 写入 Context。

        供 ToolStep / LLMStep 在多输出场景调用。缺失的 required 端口报错，
        非 required 端口填 None。

        Args:
            ctx:     当前上下文。
            result:  工具/LLM 返回的 dict（按端口 name 取字段）。
        """
        for port in self.outputs:
            if port.name in result:
                ctx.set(port.name, result[port.name])
            elif port.required:
                raise PortBindingError(
                    f"Step {self.id!r} 多输出拆分：结果 dict 缺少字段 "
                    f"{port.name!r} 且 required=True"
                )
            else:
                ctx.set(port.name, None)


# ---------------------------------------------------------------------------
# StepRegistry —— 类型注册表
# ---------------------------------------------------------------------------
class StepRegistry:
    """Step 类型注册表。

    维护 ``step_type`` 字符串到 :class:`BaseStep` 子类的映射,供 YAML
    配置层按 ``type`` 字段反序列化为对应 Step 类。

    设计上提供独立实例(可创建多个隔离的注册表),同时通过模块级
    ``_GLOBAL_STEP_REGISTRY`` 与 :func:`register_step` / :func:`get_step_type`
    提供全局默认注册表。
    """

    def __init__(self) -> None:
        self._types: dict[str, type[BaseStep]] = {}

    def register(self, step_type: str, step_cls: type[BaseStep]) -> None:
        """注册一个 Step 类型。

        重复注册同一 ``step_type`` 会覆盖旧值(便于热重载 / 测试覆盖)。

        Args:
            step_type: 类型标识字符串(如 ``"llm"`` / ``"tool"``)。
            step_cls:  :class:`BaseStep` 子类。
        """
        self._types[step_type] = step_cls

    def get(self, step_type: str) -> type[BaseStep]:
        """按类型标识获取 Step 类。

        Args:
            step_type: 类型标识字符串。

        Returns:
            type[BaseStep]: 对应的 Step 子类。

        Raises:
            KeyError: ``step_type`` 未注册时。
        """
        if step_type not in self._types:
            raise KeyError(
                f"未注册的 Step 类型: {step_type!r}。"
                f"已注册类型: {sorted(self._types.keys())}"
            )
        return self._types[step_type]

    def has(self, step_type: str) -> bool:
        """判断某类型是否已注册。"""
        return step_type in self._types

    def list(self) -> list[str]:
        """返回所有已注册类型标识(按字母序)。"""
        return sorted(self._types.keys())

    def clear(self) -> None:
        """清空所有已注册类型。"""
        self._types.clear()


# ---------------------------------------------------------------------------
# 全局注册表与便捷 API
# ---------------------------------------------------------------------------
_GLOBAL_STEP_REGISTRY = StepRegistry()


def register_step(step_type: str):
    """类装饰器:将 Step 类注册到全局注册表。

    用法::

        @register_step("echo")
        class EchoStep(BaseStep):
            type = "echo"
            async def run(self, ctx): ...

    Args:
        step_type: 类型标识字符串(将作为 YAML 中的 ``type`` 字段值)。

    Returns:
        Callable: 类装饰器,返回原类(不包装)。
    """
    def decorator(cls: type[BaseStep]) -> type[BaseStep]:
        _GLOBAL_STEP_REGISTRY.register(step_type, cls)
        return cls
    return decorator


def get_step_type(step_type: str) -> type[BaseStep]:
    """从全局注册表按类型标识获取 Step 类。

    Args:
        step_type: 类型标识字符串。

    Returns:
        type[BaseStep]: 对应的 Step 子类。

    Raises:
        KeyError: ``step_type`` 未注册时。
    """
    return _GLOBAL_STEP_REGISTRY.get(step_type)


def walk_all_steps(steps: list[BaseStep]) -> list[BaseStep]:
    """深度优先遍历 Step 树(含所有嵌套层级)。

    Args:
        steps: 顶层 Step 列表。

    Returns:
        list[BaseStep]: 深度优先顺序的所有 Step(含自身与所有子孙)。
    """
    result: list[BaseStep] = []
    for step in steps:
        result.append(step)
        result.extend(walk_all_steps(step.iter_child_steps()))
    return result


__all__ = [
    "StepTrace",
    "BaseStep",
    "StepRegistry",
    "register_step",
    "get_step_type",
    "walk_all_steps",
]
