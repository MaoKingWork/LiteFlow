"""runtime.blocking —— BlockingExecutor：阻塞执行卸载 + Tool.execution 分派。

将 ``tools/db.py`` 的局部 ``asyncio.to_thread`` 先例抽象为 Tool 级声明机制
（对齐 ``docs/visualization-design.md`` §5.5）。

设计要点：
    - **Tool 级声明**：``Tool.execution = "inline" | "thread" | "process"``
      类属性，默认 ``inline``（行为同现状，零侵入）
    - **三模式分派**：
        * ``inline``（默认）：直接 ``await tool.call(params, ctx)``，行为同现状
        * ``thread``：``asyncio.to_thread`` 经共享 ``ThreadPoolExecutor``
          （大小可配 ``executor_max_workers``，默认 4）
        * ``process``：``ProcessPoolExecutor``（默认 2 worker）；
          契约：params/result 仅 JSON 可序列化；**Context 不进子进程**
    - **事件分发不进执行器**：EventBusHooks 的队列操作全在主循环；
      BlockingExecutor 不接触 EventBus
    - **全局惰性单例**：``get_blocking_executor()`` 懒加载，与
      ``get_default_client()`` / ``ToolRegistry`` 风格一致；可通过
      ``set_blocking_executor(None)`` 重置（测试用）
    - **可选注入**：``BaseStep._blocking_executor`` 字段 + ``bind_blocking_executor``
      方法（默认 no-op，容器型 Step 重写递归传播）；Workflow 不主动 bind，
      但留扩展点（未来多工作流并行隔离）

限度说明（对齐 §5.5）：
    - GIL 下 ``thread`` 对纯 Python CPU 密集仅缓解（reportlab 的 C 段会释放 GIL，
      收益真实）
    - 彻底隔离用 ``process``，代价是序列化与进程启动开销
    - 二者按工具标注共存，默认不改任何现有工具行为

模块化原则：
    - 仅依赖标准库 + :mod:`agentkit.config`（懒加载）
    - ``run_tool`` 不导入 Tool 类（duck typing：只要有 ``execution`` 属性 + ``call``）
    - process 模式 P0 不实现，留接口签名 + ``NotImplementedError``

公开 API：
    - ExecutionMode:       执行模式常量
    - BlockingExecutor:    阻塞执行卸载器
    - get_blocking_executor:  全局单例获取
    - set_blocking_executor:  全局单例设置（含 None 重置）
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from agentkit.config import get_default

if TYPE_CHECKING:
    from agentkit.core.context import Context
    from agentkit.tools.base import Tool

logger = logging.getLogger(__name__)

__all__ = [
    "ExecutionMode",
    "BlockingExecutor",
    "get_blocking_executor",
    "set_blocking_executor",
]


# ---------------------------------------------------------------------------
# ExecutionMode —— 执行模式常量
# ---------------------------------------------------------------------------
class ExecutionMode:
    """``Tool.execution`` 的合法取值（对齐 §5.5）。

    所有常量为 ``str``，便于直接作为 ``Tool.execution`` 类属性使用。

    Attributes:
        INLINE:  直接 ``await``（默认；实现必须真异步，行为同现状）。
        THREAD:  ``asyncio.to_thread`` 经共享 ``ThreadPoolExecutor``。
                 适用于同步 IO 库、reportlab/docx 渲染。
        PROCESS: ``ProcessPoolExecutor``。契约：params/result 仅 JSON 可序列化；
                 **Context 不进子进程**，工具只收声明输入。适用于纯 Python CPU 密集。
    """

    INLINE = "inline"
    THREAD = "thread"
    PROCESS = "process"


# ---------------------------------------------------------------------------
# process 模式子进程入口（模块级函数，必须可 pickle）
# ---------------------------------------------------------------------------
def _process_tool_entry(
    tool_module: str,
    tool_qualname: str,
    params_json: str,
) -> str:
    """process 模式子进程入口：导入工具 + 调用 + 返回 JSON。

    必须为模块级函数（可 pickle）。子进程内重新导入 Tool 类并实例化，
    **不传 ctx**（Context 不进子进程契约）。

    Args:
        tool_module:  Tool 类所在模块名。
        tool_qualname: Tool 类的 qualified name。
        params_json:  params 的 JSON 序列化字符串。

    Returns:
        str: result 的 JSON 序列化字符串。

    Note:
        P0 不实现 process 模式；本函数为预留契约，P1+ 实现时启用。
    """
    import importlib
    import json

    module = importlib.import_module(tool_module)
    cls = module
    for part in tool_qualname.split("."):
        cls = getattr(cls, part)
    tool_instance = cls()
    params = json.loads(params_json)

    # 子进程内无事件循环；直接调 tool.call 的同步等价物。
    # 注意：Tool.call 是 async def，子进程内需 asyncio.run。
    result = asyncio.run(tool_instance.call(params, None))  # type: ignore[arg-type]
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# BlockingExecutor —— 阻塞执行卸载器
# ---------------------------------------------------------------------------
class BlockingExecutor:
    """按 :attr:`Tool.execution` 分派工具调用（对齐 §5.5）。

    三模式分派：
        - ``inline``（默认）：直接 ``await tool.call(params, ctx)``，行为同现状
        - ``thread``：``asyncio.to_thread`` 经共享 ``ThreadPoolExecutor``
        - ``process``：``ProcessPoolExecutor``；P0 不实现，抛 :class:`NotImplementedError`

    **事件分发不进执行器**：本类不接触 EventBus；EventBusHooks 的队列操作全在
    主循环（hook 由 ToolStep 在主循环触发）。

    线程模式契约：``ctx`` 传入线程但工具应只读（不修改 ctx）。
    进程模式契约：params/result 仅 JSON 可序列化；**Context 不进子进程**
    （P0 不实现）。

    Args:
        thread_workers:  线程池大小，默认取 ``executor_max_workers``。
        process_workers: 进程池大小，默认取 ``executor_max_processes``。
                         P0 不创建进程池（process 模式未实现）。
    """

    def __init__(
        self,
        *,
        thread_workers: int | None = None,
        process_workers: int | None = None,
    ) -> None:
        self._thread_workers: int = thread_workers or int(
            get_default("executor_max_workers")
        )
        # 进程池 P0 不创建（process 模式未实现）
        self._process_workers: int = process_workers or int(
            get_default("executor_max_processes")
        )
        self._thread_pool: ThreadPoolExecutor | None = None
        self._process_pool: ProcessPoolExecutor | None = None

    # ------------------------------------------------------------------
    # 线程池惰性创建
    # ------------------------------------------------------------------
    def _get_thread_pool(self) -> ThreadPoolExecutor:
        """惰性创建线程池（首次 thread 模式调用时）。"""
        if self._thread_pool is None:
            self._thread_pool = ThreadPoolExecutor(
                max_workers=self._thread_workers,
                thread_name_prefix="agentkit-tool",
            )
        return self._thread_pool

    # ------------------------------------------------------------------
    # run_tool —— 核心分派
    # ------------------------------------------------------------------
    async def run_tool(
        self,
        tool: "Tool",
        params: dict,
        ctx: "Context",
    ) -> dict:
        """按 ``tool.execution`` 分派工具调用。

        Args:
            tool:   工具实例（duck typing：需有 ``execution`` 属性 + ``call`` 方法）。
            params: 调用参数（已解析模板）。
            ctx:    会话上下文。

        Returns:
            dict: 工具返回结果。

        Raises:
            NotImplementedError: ``execution == "process"``（P0 未实现）。
            ValueError:          ``execution`` 非法取值。
        """
        # 读取 execution 属性；duck typing，缺失视为 inline（兼容现有 Tool）
        execution: str = getattr(tool, "execution", ExecutionMode.INLINE) or ExecutionMode.INLINE

        if execution == ExecutionMode.INLINE:
            # 直接 await（行为同现状）
            return await tool.call(params, ctx)

        if execution == ExecutionMode.THREAD:
            return await self._run_thread(tool, params, ctx)

        if execution == ExecutionMode.PROCESS:
            return await self._run_process(tool, params, ctx)

        raise ValueError(
            f"工具 {getattr(tool, 'name', tool)!r} 的 execution 属性非法: {execution!r}。"
            f"合法取值: {ExecutionMode.INLINE!r} / {ExecutionMode.THREAD!r} / "
            f"{ExecutionMode.PROCESS!r}"
        )

    # ------------------------------------------------------------------
    # thread 模式
    # ------------------------------------------------------------------
    async def _run_thread(
        self,
        tool: "Tool",
        params: dict,
        ctx: "Context",
    ) -> dict:
        """thread 模式：``asyncio.to_thread`` 经共享 ``ThreadPoolExecutor``。

        契约：``ctx`` 传入线程但工具应只读（不修改 ctx）。
        实现：``tool.call`` 是 ``async def``，但 thread 模式假设底层是同步阻塞
        库（如 reportlab）；用 ``asyncio.to_thread`` 包装后，``await`` 仍需在
        子线程内执行 ``tool.call``。为简化，P0 直接用 ``run_coroutine_threadsafe``
        不适用（需要事件循环）；改用：在子线程内 ``asyncio.run(tool.call(...))``。

        实际上更简单的等价做法：``await asyncio.to_thread(self._run_sync_in_thread,
        tool, params, ctx)``，其中 ``_run_sync_in_thread`` 在子线程内
        ``asyncio.run(tool.call(params, ctx))``。
        """
        pool = self._get_thread_pool()
        loop = asyncio.get_running_loop()
        # 在子线程内运行 async tool.call（子线程新建临时事件循环）
        return await loop.run_in_executor(
            pool,
            _thread_runner,
            tool,
            params,
            ctx,
        )

    # ------------------------------------------------------------------
    # process 模式（P0 不实现）
    # ------------------------------------------------------------------
    async def _run_process(
        self,
        tool: "Tool",
        params: dict,
        ctx: "Context",
    ) -> dict:
        """process 模式：``ProcessPoolExecutor``。

        契约：params/result 仅 JSON 可序列化；**Context 不进子进程**。

        P0 不实现，抛 :class:`NotImplementedError`。P1+ 实现时：
            1. 序列化 params 为 JSON
            2. 取 Tool 类的 module + qualname
            3. 提交到 ProcessPoolExecutor，子进程内调 :func:`_process_tool_entry`
            4. 反序列化 result JSON
        """
        raise NotImplementedError(
            f"工具 {getattr(tool, 'name', tool)!r} 标记 execution='process'，"
            f"但 process 模式在 P0 阶段未实现。请改用 'inline' 或 'thread'。"
        )

    # ------------------------------------------------------------------
    # 资源清理
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭线程池 / 进程池（如有）。幂等。"""
        if self._thread_pool is not None:
            self._thread_pool.shutdown(wait=False)
            self._thread_pool = None
        if self._process_pool is not None:
            self._process_pool.shutdown(wait=False)
            self._process_pool = None


# ---------------------------------------------------------------------------
# 模块级 _thread_runner —— 子线程入口（模块级便于 pickle / 测试）
# ---------------------------------------------------------------------------
def _thread_runner(tool: "Tool", params: dict, ctx: "Context") -> dict:
    """子线程入口：在子线程内运行 ``asyncio.run(tool.call(params, ctx))``。

    必须为模块级函数（可被 ``run_in_executor`` 直接调用）。

    契约：``ctx`` 传入子线程但工具应只读（不修改 ctx）。子线程内新建临时事件
    循环运行 ``tool.call`` 协程；循环退出后销毁，不影响主循环。
    """
    return asyncio.run(tool.call(params, ctx))


# ---------------------------------------------------------------------------
# 全局惰性单例
# ---------------------------------------------------------------------------
_GLOBAL_EXECUTOR: BlockingExecutor | None = None


def get_blocking_executor() -> BlockingExecutor:
    """获取全局 :class:`BlockingExecutor` 单例（惰性创建）。

    首次调用时按 ``executor_max_workers`` / ``executor_max_processes`` 配置创建。
    与 :func:`agentkit.llm.get_default_client` / :class:`ToolRegistry` 风格一致。

    Returns:
        BlockingExecutor: 全局单例。
    """
    global _GLOBAL_EXECUTOR
    if _GLOBAL_EXECUTOR is None:
        _GLOBAL_EXECUTOR = BlockingExecutor()
    return _GLOBAL_EXECUTOR


def set_blocking_executor(executor: BlockingExecutor | None) -> None:
    """设置 / 重置全局 :class:`BlockingExecutor` 单例。

    Args:
        executor: 新的执行器实例；``None`` 表示重置（下次 ``get_blocking_executor``
                  重新创建）。测试用：在 fixture 中隔离执行器。
    """
    global _GLOBAL_EXECUTOR
    _GLOBAL_EXECUTOR = executor
