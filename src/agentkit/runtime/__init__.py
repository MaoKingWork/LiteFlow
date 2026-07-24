"""runtime —— 可视化适配运行时层。

本子包为 AgentKit 引入可视化能力的运行时支撑，与 ``core/`` 完全解耦：
仅通过 ``hooks`` 与引擎公开 API 接入，``core/`` 不 import 本包。

设计目标（对齐 ``docs/visualization-design.md``）：
    - **轻量**：零新依赖；事件、产物、执行卸载各自独立成模块
    - **稳定**：事件协议显式版本化；所有状态转换落盘；崩溃可恢复、可对账
    - **模块化**：``core/`` 不依赖 runtime；经 hooks 与注册表接入
    - **零侵入**：不挂 EventBusHooks、不调用 ArtifactStore、工具不标 execution 时
                  行为完全同现状

模块组成：
    - event:        RunEvent v1 协议 + EventBus + EventLog
    - event_hooks:  EventBusHooks（LifecycleHooks 子类，hook→事件翻译）
    - artifact:     ArtifactStore（写序协议）+ GCSweeper（孤儿对账）
    - blocking:     BlockingExecutor（thread/process 卸载）+ 全局单例
    - run_manager:  RunManager（状态机编排 + 取消编排 + 内存注册表）
    - reconciler:   Reconciler（启动对账：僵尸 run + GC + 日志完整性）

依赖方向：``runtime/`` → ``core/``（只读 hooks 契约）+ 标准库；不依赖 server。
"""
from __future__ import annotations

from agentkit.runtime.artifact import ArtifactQuotaError, ArtifactRef, ArtifactStore, GCSweeper
from agentkit.runtime.blocking import (
    BlockingExecutor,
    ExecutionMode,
    get_blocking_executor,
    set_blocking_executor,
)
from agentkit.runtime.event import (
    EventBus,
    EventLog,
    EventType,
    RunEvent,
)
from agentkit.runtime.event_hooks import EventBusHooks
from agentkit.runtime.reconciler import ReconcileResult, Reconciler
from agentkit.runtime.run_manager import RunHandle, RunManager, RunSummary

__all__ = [
    # event
    "RunEvent",
    "EventType",
    "EventBus",
    "EventLog",
    # event_hooks
    "EventBusHooks",
    # artifact
    "ArtifactRef",
    "ArtifactStore",
    "ArtifactQuotaError",
    "GCSweeper",
    # blocking
    "BlockingExecutor",
    "ExecutionMode",
    "get_blocking_executor",
    "set_blocking_executor",
    # run_manager (P1)
    "RunHandle",
    "RunSummary",
    "RunManager",
    # reconciler (P1)
    "ReconcileResult",
    "Reconciler",
]
