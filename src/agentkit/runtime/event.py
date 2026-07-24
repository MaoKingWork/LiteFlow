"""runtime.event —— RunEvent v1 协议 + EventBus + EventLog。

本模块实现可视化适配的运行时事件层，是前后端唯一的运行时契约。

设计要点（对齐 ``docs/visualization-design.md`` §5.1 / §5.2）：
    - **显式版本化**：``RunEvent.v`` 标识协议版本；破坏性变更升 v2，旧字段只增不改
    - **per-run 单调 seq**：``EventBus`` 单点分配，SSE ``Last-Event-ID`` 与历史回放游标
    - **背压分级**：
        * 可靠级（``run_*`` / ``step_*`` / ``tool_call`` / ``artifact_produced``）：
          不丢；日志先行，队列满时阻塞至多 ``reliable_block_timeout``，超时记 warning
          并丢弃入队（但已落日志，历史不丢）
        * 可合并级（``llm_delta``）：50ms 窗口合并；队列满时丢弃旧 delta、保留最新
    - **日志先于分发**：``EventBus.publish`` 先 ``EventLog.append``，再入内存队列
    - **per-subscriber queue**：每个订阅者独立队列，``publish`` 广播；互不干扰

EventLog 为 ``output/runs/{run_id}/events.jsonl``，单行一事件，``O_APPEND`` 写入。
它是历史回放的唯一数据源，也是崩溃后 SSE 续传的数据源。**历史 = 实时**：
前端渲染器只消费事件流；看历史 run 即顺序读 JSONL，看实时即 SSE，二者同一渲染路径。

模块化原则：
    - 仅依赖标准库与 ``agentkit.config``
    - ``EventLog`` 同步 I/O（``append`` 必须在 ``publish`` 返回前完成，保证日志先行）
    - ``EventBus`` 全异步，per-subscriber ``asyncio.Queue``
    - 不依赖 ``core/`` 任何模块（``RunEvent`` 字段对齐 ``StepTrace``，但运行时不导入）

公开 API：
    - RunEvent:        事件协议数据类
    - EventType:       事件类型常量
    - EventBus:        per-run 事件总线
    - EventLog:        JSONL 持久化
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "RunEvent",
    "EventType",
    "EventBus",
    "EventLog",
    "EVENT_PROTOCOL_VERSION",
]


# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------
EVENT_PROTOCOL_VERSION: int = 1
"""RunEvent 协议版本。破坏性变更升 v2，旧字段只增不改。"""


# ---------------------------------------------------------------------------
# EventType —— 事件类型常量
# ---------------------------------------------------------------------------
class EventType:
    """RunEvent.type 的合法取值（对齐 §5.1 事件类型全集表）。

    所有常量为 ``str``，便于直接作为 ``RunEvent.type`` 使用。

    类别：
        - 运行：``RUN_CREATED`` / ``RUN_STARTED`` / ``RUN_COMPLETED`` /
          ``RUN_FAILED`` / ``RUN_CANCELLING`` / ``RUN_CANCELLED`` / ``RUN_INTERRUPTED``
        - Step：``STEP_STARTED`` / ``STEP_FINISHED``
        - LLM 流：``LLM_STREAM_START`` / ``LLM_DELTA`` / ``LLM_STREAM_END``
        - 工具：``TOOL_CALL``
        - 产物：``ARTIFACT_PRODUCED``
    """

    # 运行级
    RUN_CREATED = "run_created"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLING = "run_cancelling"
    RUN_CANCELLED = "run_cancelled"
    RUN_INTERRUPTED = "run_interrupted"

    # Step 级
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"

    # LLM 流式
    LLM_STREAM_START = "llm_stream_start"
    LLM_DELTA = "llm_delta"
    LLM_STREAM_END = "llm_stream_end"

    # 工具
    TOOL_CALL = "tool_call"

    # 产物
    ARTIFACT_PRODUCED = "artifact_produced"


# 重新构造可靠级 / 可合并级集合（EventType 类定义完成后）
_RELIABLE_TYPES = frozenset({
    EventType.RUN_CREATED,
    EventType.RUN_STARTED,
    EventType.RUN_COMPLETED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLING,
    EventType.RUN_CANCELLED,
    EventType.RUN_INTERRUPTED,
    EventType.STEP_STARTED,
    EventType.STEP_FINISHED,
    EventType.TOOL_CALL,
    EventType.ARTIFACT_PRODUCED,
})
_MERGEABLE_TYPES = frozenset({
    EventType.LLM_DELTA,
})


# ---------------------------------------------------------------------------
# RunEvent —— 事件协议数据类
# ---------------------------------------------------------------------------
@dataclass
class RunEvent:
    """RunEvent v1 协议数据类（对齐 §5.1 事件 schema）。

    前后端唯一运行时契约。``seq`` 由 :class:`EventBus` 单点分配，保证 per-run
    单调递增；``v`` 标识协议版本。

    Attributes:
        v:        协议版本（:data:`EVENT_PROTOCOL_VERSION`）。破坏性变更升 v2。
        seq:      per-run 单调递增整数，SSE ``Last-Event-ID`` 与历史回放游标。
        run_id:   所属 run id。
        type:     事件类型，见 :class:`EventType`。
        ts:       事件时间戳（``time.time()``，秒）。
        step_id:  关联的 Step id（可选；Step / LLM / 工具事件携带）。
        attempt:  LLM 流式 attempt 序号（可选；retry/降级时前端据此重置缓冲）。
        payload:  类型相关数据；StepTrace 字段直接对齐。
    """

    v: int = EVENT_PROTOCOL_VERSION
    seq: int = 0
    run_id: str = ""
    type: str = ""
    ts: float = 0.0
    step_id: str | None = None
    attempt: int | None = None
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 序列化的 dict。

        ``None`` 字段保留（与协议一致，前端按字段存在性判断可选字段）。
        """
        return asdict(self)

    def to_jsonl(self) -> str:
        """序列化为 JSONL 单行（无缩进、``\\n`` 结尾）。

        供 :class:`EventLog` append 与读取对齐。
        """
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict) -> "RunEvent":
        """从 dict 反序列化。

        宽松处理：仅取已知字段，缺失字段使用 dataclass 默认值，便于向前兼容
        （后续新增字段时旧事件仍可加载）。
        """
        known = {"v", "seq", "run_id", "type", "ts", "step_id", "attempt", "payload"}
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)

    @classmethod
    def from_jsonl(cls, line: str) -> "RunEvent":
        """从 JSONL 单行反序列化。

        Raises:
            json.JSONDecodeError: 行不是合法 JSON。
        """
        return cls.from_dict(json.loads(line))

    def make(self, **overrides: Any) -> "RunEvent":
        """以当前事件为模板，构造一个新事件（覆盖部分字段）。

        便捷方法，供 EventBusHooks 等场景复用 run_id / step_id 等公共字段。
        """
        data = self.to_dict()
        data.update(overrides)
        return RunEvent.from_dict(data)


# ---------------------------------------------------------------------------
# EventLog —— JSONL 持久化
# ---------------------------------------------------------------------------
class EventLog:
    """RunEvent 的 JSONL 持久化（对齐 §5.2 / §6 存储布局）。

    路径：``{base_dir}/{run_id}/events.jsonl``（默认 ``base_dir="output/runs"``）。
    单行一事件，``O_APPEND`` 写入；是历史回放与 SSE 续传的唯一数据源。

    **日志先行契约**：``append`` 必须在 ``EventBus.publish`` 返回前完成，
    保证崩溃时已分发的事件必然落盘（实时推送缺失可由 ``Last-Event-ID`` 补齐）。

    同步 I/O：``append`` 为同步方法，调用方（``EventBus``）在异步上下文中直接调用。
    单行 ``O_APPEND`` 在 POSIX 下原子；Windows 下用 ``open("ab")`` + 手动 ``\\n``
    近似原子（行级不交错即可，配合 GCSweeper 对账）。

    Args:
        run_id:   所属 run id。
        path:     显式文件路径；``None`` 时由 ``base_dir`` + ``run_id`` 拼接。
        base_dir: 存储根目录，默认 ``output/runs``；``path`` 非 None 时忽略。
    """

    def __init__(
        self,
        run_id: str,
        *,
        path: str | None = None,
        base_dir: str = "output/runs",
    ) -> None:
        self.run_id: str = run_id
        if path is not None:
            self.path: str = path
        else:
            self.path = os.path.join(base_dir, run_id, "events.jsonl")
        # 确保父目录存在（append 前的初始化）
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def append(self, event: RunEvent) -> None:
        """追加一个事件到 JSONL。

        同步 ``O_APPEND``；调用方（``EventBus.publish``）在异步上下文中直接调用，
        单次磁盘写通常 < 1ms，不显著阻塞事件循环。

        Args:
            event: 待追加的事件；``event.seq`` 应已由 :class:`EventBus` 分配。
        """
        line = event.to_jsonl() + "\n"
        # open("ab") + 二进制写入：Windows 下比 "a" 模式更接近原子追加
        with open(self.path, "ab") as f:
            f.write(line.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())

    def read_from(self, seq: int = 0) -> Iterator[RunEvent]:
        """从指定 seq 起读取事件（历史回放 / SSE 续传）。

        逐行解析，过滤 ``event.seq < seq``。损坏行跳过并记 warning（不影响其他行）。

        Args:
            seq: 起始 seq（含）；默认 0 表示从头读。

        Yields:
            RunEvent: ``seq >= 起始值`` 的事件，按文件顺序。
        """
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = RunEvent.from_jsonl(line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    logger.warning("事件日志损坏行已跳过: %s", line[:200])
                    continue
                if event.seq >= seq:
                    yield event

    def latest_seq(self) -> int:
        """返回当前最大 seq；文件不存在或为空时返回 0。"""
        latest = 0
        if not os.path.exists(self.path):
            return 0
        # 只扫最后一行（O(1) 近似）；为简单起见全量扫描，事件日志通常不大
        for event in self.read_from():
            if event.seq > latest:
                latest = event.seq
        return latest


# ---------------------------------------------------------------------------
# _Subscription —— per-subscriber 订阅句柄
# ---------------------------------------------------------------------------
class _Subscription:
    """EventBus 订阅句柄（per-subscriber queue）。

    每个订阅者持有一个独立的 ``asyncio.Queue``；``EventBus.publish`` 广播到所有
    活跃订阅者。订阅者退出时调用 :meth:`cancel` 清理，避免内存泄漏。

    支持异步迭代（``async for event in subscription``），迭代结束自动取消订阅。
    """

    def __init__(self, queue: asyncio.Queue, cancel_fn: Any) -> None:
        self._queue: asyncio.Queue = queue
        self._cancel_fn: Any = cancel_fn
        self._closed: bool = False

    async def get(self) -> RunEvent | None:
        """取下一个事件；订阅关闭且队列空时返回 ``None``。"""
        if self._closed and self._queue.empty():
            return None
        return await self._queue.get()

    def cancel(self) -> None:
        """取消订阅（幂等）。"""
        if self._closed:
            return
        self._closed = True
        self._cancel_fn(self)

    def __aiter__(self) -> "_Subscription":
        return self

    async def __anext__(self) -> RunEvent:
        event = await self.get()
        if event is None:
            self.cancel()
            raise StopAsyncIteration
        return event


# ---------------------------------------------------------------------------
# EventBus —— per-run 事件总线
# ---------------------------------------------------------------------------
class EventBus:
    """per-run 事件总线（对齐 §5.1 / §5.2）。

    单点分配 ``seq``，背压分级，日志先于分发。per-subscriber queue 模型：
    每个订阅者独立 ``asyncio.Queue``，``publish`` 广播；互不干扰。

    **背压分级**：
        - 可靠级（``run_*`` / ``step_*`` / ``tool_call`` / ``artifact_produced``）：
          先 ``log.append``，再入队；队列满时 ``await asyncio.wait_for(queue.put,
          timeout=reliable_block_timeout)``，超时记 warning 并丢弃入队（已落日志）。
        - 可合并级（``llm_delta``）：50ms 窗口合并；队列满时丢弃旧 delta、保留最新。

    用法::

        log = EventLog(run_id)
        bus = EventBus(run_id, log=log)
        sub = await bus.subscribe()
        await bus.publish(RunEvent(type=EventType.RUN_STARTED))
        async for event in sub:
            ...

    Args:
        run_id:            所属 run id。
        log:               事件日志；``None`` 时不持久化（仅内存分发，测试用）。
        queue_size:        per-subscriber 队列容量，默认 1000。
        delta_merge_window: ``llm_delta`` 合并窗口（秒），默认 0.05（50ms）。
        reliable_block_timeout: 可靠级事件队列满时的阻塞上限（秒），默认 1.0。
    """

    def __init__(
        self,
        run_id: str,
        log: EventLog | None = None,
        *,
        queue_size: int = 1000,
        delta_merge_window: float = 0.05,
        reliable_block_timeout: float = 1.0,
    ) -> None:
        self.run_id: str = run_id
        self._log: EventLog | None = log
        self._queue_size: int = queue_size
        self._delta_merge_window: float = delta_merge_window
        self._reliable_block_timeout: float = reliable_block_timeout

        # seq 单点分配，asyncio.Lock 保护（publish 可并发触发）
        self._seq_lock: asyncio.Lock = asyncio.Lock()
        self._seq: int = 0

        # per-subscriber 队列集合
        self._subs: set[_Subscription] = set()
        self._subs_lock: asyncio.Lock = asyncio.Lock()

        # llm_delta 合并状态：上次 delta 时间戳 + 待 flush 的合并事件
        self._delta_last_ts: float = 0.0
        self._delta_pending: RunEvent | None = None
        self._delta_flush_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # seq 分配
    # ------------------------------------------------------------------
    async def next_seq(self) -> int:
        """分配下一个单调递增 seq（per-run）。"""
        async with self._seq_lock:
            self._seq += 1
            return self._seq

    # ------------------------------------------------------------------
    # publish —— 核心分发
    # ------------------------------------------------------------------
    async def publish(self, event: RunEvent) -> None:
        """发布一个事件。

        流程（严格顺序，对齐 §5.2）：
            1. 填充 ``run_id`` / ``ts`` / ``seq``（seq 单点分配）。
            2. ``log.append(event)``（日志先行；log 为 None 时跳过）。
            3. 按事件类型分级入队：
               - 可靠级：广播到所有订阅者队列；队列满时阻塞至多
                 ``reliable_block_timeout``，超时记 warning 并丢弃入队（已落日志）。
               - 可合并级（``llm_delta``）：50ms 窗口合并；队列满时丢弃旧 delta、
                 保留最新。

        Args:
            event: 待发布事件；``run_id`` / ``ts`` / ``seq`` 缺失时由本方法填充。
        """
        # 1. 填充公共字段
        if not event.run_id:
            event.run_id = self.run_id
        if not event.ts:
            event.ts = time.time()
        if not event.seq:
            event.seq = await self.next_seq()

        # 2. 日志先行
        if self._log is not None:
            try:
                self._log.append(event)
            except Exception as exc:
                # 日志写入失败：记 error 但不阻塞分发（实时推送仍可继续）
                # 历史回放会缺失此事件，但 GCSweeper / 对账可兜底
                logger.error("事件日志 append 失败（已忽略，继续分发）: %r", exc)

        # 3. 分级入队
        if event.type in _MERGEABLE_TYPES:
            await self._publish_mergeable(event)
        else:
            await self._publish_reliable(event)

    async def _publish_reliable(self, event: RunEvent) -> None:
        """可靠级分发：广播到所有订阅者；队列满时阻塞至多 timeout，超时丢弃入队。"""
        async with self._subs_lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                await asyncio.wait_for(
                    sub._queue.put(event),
                    timeout=self._reliable_block_timeout,
                )
            except asyncio.TimeoutError:
                # 队列满且超时：记 warning，丢弃此订阅者的本次入队（已落日志）
                logger.warning(
                    "事件 %s seq=%d 分发超时（订阅者队列满），已丢弃入队",
                    event.type, event.seq,
                )

    async def _publish_mergeable(self, event: RunEvent) -> None:
        """可合并级分发（llm_delta）：50ms 窗口合并；队列满时丢弃旧 delta、保留最新。

        策略（对齐 §5.1 背压分级表）：
            - 收到 delta 后不立即分发，存为 ``_delta_pending``。
            - 若距上次分发 > ``delta_merge_window``，立即 flush（合并当前 pending）。
            - 否则启动 / 重设一个 flush 任务，窗口结束后 flush。
            - 队列满时丢弃旧 delta、保留最新（不阻塞生产者）。
        """
        # 合并：累加 delta 文本
        if self._delta_pending is not None and self._delta_pending.step_id == event.step_id:
            # 同 step 内合并 delta 文本
            old_delta = self._delta_pending.payload.get("delta", "")
            new_delta = event.payload.get("delta", "")
            self._delta_pending.payload["delta"] = old_delta + new_delta
            self._delta_pending.payload["accumulated_len"] = (
                event.payload.get("accumulated_len",
                                  self._delta_pending.payload.get("accumulated_len", 0))
            )
            self._delta_pending.ts = event.ts
        else:
            self._delta_pending = event

        now = event.ts
        if now - self._delta_last_ts >= self._delta_merge_window:
            # 窗口已过：立即 flush
            await self._flush_delta()
        else:
            # 窗口内：重设 flush 任务
            if self._delta_flush_task is not None:
                self._delta_flush_task.cancel()
            self._delta_flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        """窗口结束后 flush pending delta。"""
        try:
            await asyncio.sleep(self._delta_merge_window)
            await self._flush_delta()
        except asyncio.CancelledError:
            # 被 _publish_mergeable 取消重设：正常
            pass

    async def _flush_delta(self) -> None:
        """flush 当前 pending delta 到所有订阅者（队列满时丢弃旧、保留最新）。"""
        if self._delta_pending is None:
            return
        event = self._delta_pending
        self._delta_pending = None
        self._delta_last_ts = time.time()
        if self._delta_flush_task is not None:
            self._delta_flush_task.cancel()
            self._delta_flush_task = None

        async with self._subs_lock:
            subs = list(self._subs)
        for sub in subs:
            # 队列满时丢弃旧 delta、保留最新（非阻塞）
            if sub._queue.full():
                try:
                    sub._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                sub._queue.put_nowait(event)
            except asyncio.QueueFull:
                # 极端情况：丢弃后仍满（并发），记 warning
                logger.debug("llm_delta 合并后仍队列满，丢弃本次 delta")

    # ------------------------------------------------------------------
    # subscribe —— per-subscriber queue
    # ------------------------------------------------------------------
    async def subscribe(self) -> _Subscription:
        """订阅事件流。

        返回 :class:`_Subscription`，支持 ``async for event in sub`` 迭代。
        每个订阅者独立 ``asyncio.Queue``，互不干扰。订阅者退出时应调用
        ``sub.cancel()`` 或迭代结束自动取消。

        Returns:
            _Subscription: 订阅句柄。
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        sub = _Subscription(queue, cancel_fn=self._remove_sub)
        async with self._subs_lock:
            self._subs.add(sub)
        return sub

    def _remove_sub(self, sub: _Subscription) -> None:
        """从订阅者集合移除（由 _Subscription.cancel 回调）。"""
        # 不加锁：set.remove 在 CPython 下 GIL 保护，且 subscribe 也用锁；
        # 这里调用量小，简单起见用同步 remove
        self._subs.discard(sub)

    # ------------------------------------------------------------------
    # 便捷发布方法
    # ------------------------------------------------------------------
    async def publish_run_created(self, workflow_name: str = "") -> None:
        """便捷发布 ``run_created`` 事件。"""
        await self.publish(RunEvent(
            type=EventType.RUN_CREATED,
            payload={"workflow_name": workflow_name},
        ))

    async def publish_run_lifecycle(
        self, status: str, error: str | None = None, **extra: Any
    ) -> None:
        """便捷发布运行级事件（``run_started`` / ``run_completed`` 等）。

        Args:
            status: 终态状态字符串，映射到对应事件类型。
            error:  失败时的错误信息。
            **extra: 额外 payload 字段。
        """
        type_map = {
            "running": EventType.RUN_STARTED,
            "completed": EventType.RUN_COMPLETED,
            "failed": EventType.RUN_FAILED,
            "cancelling": EventType.RUN_CANCELLING,
            "cancelled": EventType.RUN_CANCELLED,
            "interrupted": EventType.RUN_INTERRUPTED,
        }
        event_type = type_map.get(status, EventType.RUN_COMPLETED)
        payload: dict[str, Any] = {"status": status}
        if error is not None:
            payload["error"] = error
        payload.update(extra)
        await self.publish(RunEvent(type=event_type, payload=payload))

    # ------------------------------------------------------------------
    # 资源清理
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """关闭总线：取消所有订阅者，flush 待合并 delta。"""
        if self._delta_flush_task is not None:
            self._delta_flush_task.cancel()
            self._delta_flush_task = None
        async with self._subs_lock:
            subs = list(self._subs)
            self._subs.clear()
        # 给订阅者发 None 哨兵？_Subscription.get 在 closed+empty 时返回 None，
        # 但 close 时订阅者可能正阻塞在 get；这里不主动 enqueue None（避免与
        # 协议事件混淆），订阅者应在 cancel 后退出迭代。
        for sub in subs:
            sub.cancel()
