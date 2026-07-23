"""core.cancel —— 协作式取消令牌。

提供 :class:`CancelToken`:基于 ``asyncio.Event`` 的轻量级协作取消原语。
Workflow 与容器型 Step 在执行边界检查令牌状态,实现 graceful 取消。

设计原则:
    - 零依赖:仅用标准库 ``asyncio``。
    - 非侵入:不修改协程内部逻辑,仅在边界检查。
    - 幂等:``trigger()`` 多次调用安全。

公开 API:
    - CancelToken: 协作式取消令牌
"""

from __future__ import annotations

import asyncio

__all__ = ["CancelToken"]


class CancelToken:
    """协作式取消令牌。

    基于 ``asyncio.Event`` 实现。触发后所有检查点的协程可感知取消信号。
    用于 Workflow 的 graceful 取消模式:在 step 边界、LoopStep 迭代间、
    ParallelStep 分支启动前检查令牌,实现"当前 step 完成后停止"。

    immediate 取消不依赖本令牌,而是通过 ``asyncio.Task.cancel()`` 注入
    :class:`asyncio.CancelledError`,由 ``Workflow._execute`` 显式捕获并落盘。

    用法::

        token = CancelToken()
        # 在另一处触发取消
        token.trigger()
        # Workflow 在 step 边界检查
        if token.is_cancelled:
            ...
    """

    def __init__(self) -> None:
        self._event: asyncio.Event = asyncio.Event()

    def trigger(self) -> None:
        """触发取消信号。幂等:多次调用安全。"""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """是否已被取消。"""
        return self._event.is_set()

    def check(self) -> None:
        """检查取消状态,已取消时抛 :class:`asyncio.CancelledError`。

        适用于需要在协程内部主动中断的场景(如紧凑长循环)。
        边界检查推荐直接用 :attr:`is_cancelled` 属性判断,避免抛异常。
        """
        if self._event.is_set():
            raise asyncio.CancelledError()
