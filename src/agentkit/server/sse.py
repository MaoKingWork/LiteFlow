"""server.sse —— SSE 事件流推送 + Last-Event-ID 续传（E3）。

流程:
    1. 历史补齐:EventLog(read_id).read_from(last_event_id + 1) 逐条 yield
    2. 终态判断:历史含终态事件则结束
    3. live 订阅:订阅 EventBus,消费新事件直到终态

设计原则:
    - 不丢不重:先 subscribe 再回放历史,消费 live 时跳过已回放的 seq
    - 懒加载 sse_starlette
    - run 已结束(不在内存)时只回放历史

注意:本模块 **不用** ``from __future__ import annotations``
（Pydantic + FastAPI 局部类解析陷阱）。
"""

import json

from agentkit.runtime.event import EventLog, EventType

__all__ = ["TERMINAL_TYPES", "event_stream", "create_sse_response"]


# 终态事件类型（收到后断开 SSE）
TERMINAL_TYPES = frozenset({
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
    EventType.RUN_INTERRUPTED,
})


def _format_sse(event) -> dict:
    """格式化 RunEvent 为 SSE 事件 dict。

    ``data`` 下发完整事件(RunEvent v1 全字段,对齐 §5.1 契约):
    前端据此获得 ``step_id`` / ``attempt`` / ``ts`` 以路由 LLM 流与
    节点着色;纯增量字段,旧客户端仅读 ``payload`` 不受影响。

    Returns:
        dict: ``{"event": type, "data": json_str, "id": seq}``。
    """
    try:
        data = json.dumps(event.to_dict(), default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        data = json.dumps({"error": "event 不可序列化"})
    return {
        "event": event.type,
        "data": data,
        "id": str(event.seq),
    }


async def event_stream(run_id: str, last_event_id: int, run_manager):
    """SSE 事件流生成器。

    流程:
        1. 先 subscribe（若 run 在内存活跃）,避免 subscribe 窗口丢事件
        2. 回放历史（last_event_id + 1 起）
        3. 历史含终态 → 结束
        4. 消费 live 事件,跳过已回放的 seq,直到终态

    Args:
        run_id:        run id。
        last_event_id: 客户端最后收到的事件 seq（0 表示从头）。
        run_manager:   RunManager 实例。

    Yields:
        dict: SSE 事件 ``{event, data, id}``。
    """
    base_dir = getattr(run_manager, "_base_dir", "output/runs")

    # 先 subscribe（若 run 活跃）,确保回放期间的 live 事件不丢失
    handle = run_manager.get(run_id)
    sub = None
    if handle is not None:
        sub = await handle.event_bus.subscribe()

    try:
        # 回放历史
        log = EventLog(run_id, base_dir=base_dir)
        max_seq = last_event_id
        for event in log.read_from(last_event_id + 1):
            yield _format_sse(event)
            max_seq = event.seq
            if event.type in TERMINAL_TYPES:
                return

        # 消费 live 事件
        if sub is None:
            # run 不在内存（已结束）且历史无终态 → 结束
            return

        async for event in sub:
            # 跳过已回放的事件（subscribe 前已 publish 到 log 的事件）
            if event.seq <= max_seq:
                continue
            yield _format_sse(event)
            if event.type in TERMINAL_TYPES:
                break
    finally:
        if sub is not None:
            sub.cancel()


def create_sse_response(run_id: str, run_manager, last_event_id: int = 0):
    """创建 SSE 响应。

    懒加载 sse_starlette.EventSourceResponse。

    Args:
        run_id:        run id。
        run_manager:   RunManager 实例。
        last_event_id: 客户端最后收到的事件 seq。

    Raises:
        ImportError: 未安装 sse-starlette。
    """
    try:
        from sse_starlette import EventSourceResponse
    except ImportError as e:
        raise ImportError(
            "SSE 需要 sse-starlette: pip install agentkit[server]"
        ) from e
    return EventSourceResponse(
        event_stream(run_id, last_event_id, run_manager)
    )
