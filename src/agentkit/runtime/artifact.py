"""runtime.artifact —— ArtifactStore 写序协议 + GCSweeper 孤儿对账。

本模块实现可视化适配的产物层，把 Step 产出的报告 / 文件等大对象独立落盘，
事件流只携带引用（``artifact_produced`` 事件 payload = :class:`ArtifactRef` 字段）。

设计要点（对齐 ``docs/visualization-design.md`` §5.6 / §6 存储布局）：
    - **动机**：Context 大对象仅留 200 字符摘要，产物全文不可见；且事件流不能
      携带大产物（背压）。产物独立落盘、事件只带引用
    - **五步写序**（严格顺序）：
        1. 写 ``{artifact_id}.tmp``
        2. ``flush + fsync + close``
        3. ``os.replace(.tmp → {artifact_id})`` —— 同文件系统内原子
        4. ``append artifact_produced 事件 → events.jsonl``
        5. 入内存队列 → SSE 分发
    - **崩溃窗口兜底**：
        * 1–3 之间崩溃 → ``.tmp`` 残留，GCSweeper 直接删
        * 3–4 之间崩溃 → 完整文件 + 无事件（孤儿），GCSweeper 宽限 24h 后删
        * 4–5 之间崩溃 → 文件 + 日志 + 推送缺失，SSE ``Last-Event-ID`` 从日志补齐
    - **保证**：客户端收到 ``artifact_produced`` 时，文件必然完整存在
      （rename 先于 publish）——"URI 指向不存在文件"在协议上被消除

目录布局（对齐 §6）::

    output/runs/{run_id}/artifacts/{step_id}/{artifact_id}

模块化原则：
    - 仅依赖标准库 + :mod:`agentkit.runtime.event`
    - :class:`ArtifactStore.save` 内部调 :meth:`EventBus.publish`，由 EventBus
      保证"日志先于分发"（``EventLog.append`` 先于入队）
    - :class:`GCSweeper` 对账数据源 = 事件日志（``artifact_produced`` 记录全集）

公开 API：
    - ArtifactRef:        产物引用数据类
    - ArtifactStore:      写序协议实现
    - ArtifactQuotaError: 配额超限异常
    - GCSweeper:          孤儿文件对账
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from agentkit.runtime.event import EventBus, EventType, RunEvent

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "ArtifactQuotaError",
    "GCSweeper",
]


# ---------------------------------------------------------------------------
# ArtifactQuotaError —— 配额超限异常
# ---------------------------------------------------------------------------
class ArtifactQuotaError(Exception):
    """产物配额超限时抛出。

    由 :meth:`ArtifactStore.save` 在以下情况抛出（写盘前检查，不发事件）：
        - 单 artifact 大小超过 ``max_size``
        - 单 run 产物总量超过 ``max_total``
    """


# ---------------------------------------------------------------------------
# ArtifactRef —— 产物引用数据类
# ---------------------------------------------------------------------------
@dataclass
class ArtifactRef:
    """产物引用（对齐 §5.6 ``artifact_produced`` 事件 payload）。

    事件流只携带本 dataclass 的字段；客户端据此下载产物，可选 ``md5`` 校验。

    Attributes:
        id:            artifact_id（默认 ``art_<uuid hex 前 12 位>``）。
        run_id:        所属 run id。
        step_id:       产出该 artifact 的 Step id。
        uri:           相对路径 ``output/runs/{run_id}/artifacts/{step_id}/{id}``。
        content_type:  MIME 类型，如 ``text/markdown`` / ``application/pdf``。
        size:          字节大小。
        md5:           内容 MD5 校验（hex）。
        summary:       200 字符摘要（供前端列表展示，不下载全文）。
        ts:            产出时间戳（``time.time()``）。
    """

    id: str = ""
    run_id: str = ""
    step_id: str = ""
    uri: str = ""
    content_type: str = "application/octet-stream"
    size: int = 0
    md5: str = ""
    summary: str = ""
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 序列化的 dict（作为事件 payload）。"""
        return asdict(self)


# ---------------------------------------------------------------------------
# 辅助：摘要生成
# ---------------------------------------------------------------------------
def _make_summary(content: bytes, max_len: int = 200) -> str:
    """从产物内容生成 200 字符摘要。

    尝试 utf-8 解码后取前 ``max_len`` 字符；解码失败退化为 ``<binary {size} bytes>``。
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary {len(content)} bytes>"
    text = text.strip()
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


# ---------------------------------------------------------------------------
# ArtifactStore —— 写序协议实现
# ---------------------------------------------------------------------------
class ArtifactStore:
    """产物存储 + 五步写序协议（对齐 §5.6）。

    生产者写序（严格顺序，保证"客户端收到事件时文件必然完整存在"）::

        1. 写 {artifact_id}.tmp
        2. flush + fsync + close
        3. os.replace(.tmp → {artifact_id})   # 同文件系统内原子
        4. EventBus.publish(ARTIFACT_PRODUCED) # 内部先 append EventLog 再入队
        5. （EventBus 内部分发到订阅者）

    步骤 4 由 :class:`EventBus` 保证"日志先于分发"——``EventLog.append`` 在
    ``publish`` 返回前完成，``artifact_produced`` 事件必然落盘后才入内存队列。

    配额：``max_size`` 单 artifact 上限、``max_total`` 单 run 总量上限；
    超限抛 :class:`ArtifactQuotaError`，不写盘不发事件。

    Args:
        run_id:       所属 run id。
        event_bus:    目标事件总线；``None`` 时不发事件（仅落盘，测试用）。
        base_dir:     存储根目录，默认 ``output/runs``。
        max_size:     单 artifact 字节上限；``None`` 不限制。
        max_total:    单 run 产物总量字节上限；``None`` 不限制。
    """

    def __init__(
        self,
        run_id: str,
        *,
        event_bus: EventBus | None = None,
        base_dir: str = "output/runs",
        max_size: int | None = None,
        max_total: int | None = None,
    ) -> None:
        self.run_id: str = run_id
        self._bus: EventBus | None = event_bus
        self._base_dir: str = base_dir
        self._max_size: int | None = max_size
        self._max_total: int | None = max_total
        # 已产出 artifact 的累计字节（配额检查）
        self._total_bytes: int = 0
        # 已产出 artifact id 集合（防重复 + 便于对账）
        self._artifact_ids: set[str] = set()
        # 引用缓存（id → ArtifactRef），list_artifacts 用
        self._refs_by_id: dict[str, ArtifactRef] = {}

    # ------------------------------------------------------------------
    # 路径计算
    # ------------------------------------------------------------------
    def _artifact_dir(self, step_id: str) -> str:
        """产物目录：``{base_dir}/{run_id}/artifacts/{step_id}``。"""
        return os.path.join(self._base_dir, self.run_id, "artifacts", step_id)

    def _artifact_path(self, step_id: str, artifact_id: str) -> str:
        """产物最终路径（rename 后）。"""
        return os.path.join(self._artifact_dir(step_id), artifact_id)

    def _tmp_path(self, step_id: str, artifact_id: str) -> str:
        """临时路径（rename 前）。"""
        return os.path.join(self._artifact_dir(step_id), f"{artifact_id}.tmp")

    # ------------------------------------------------------------------
    # save —— 五步写序
    # ------------------------------------------------------------------
    async def save(
        self,
        step_id: str,
        content: bytes | str,
        *,
        content_type: str = "application/octet-stream",
        summary: str = "",
        artifact_id: str | None = None,
    ) -> ArtifactRef:
        """执行五步写序，返回 :class:`ArtifactRef` 并发布 ``artifact_produced`` 事件。

        严格顺序（对齐 §5.6）：
            1. 写 ``{artifact_id}.tmp``
            2. ``flush + fsync + close``
            3. ``os.replace(.tmp → {artifact_id})`` —— 同文件系统内原子
            4. ``EventBus.publish(ARTIFACT_PRODUCED)`` —— 内部先 append 日志再入队
            5. （EventBus 内部分发）

        保证：客户端收到 ``artifact_produced`` 时，文件必然完整存在
        （rename 先于 publish）。

        Args:
            step_id:       产出 Step 的 id。
            content:       产物内容（``bytes`` 或 ``str``；``str`` 按 utf-8 编码）。
            content_type:  MIME 类型。
            summary:       200 字符摘要（供前端列表展示）。
            artifact_id:   自定义 id；``None`` 时自动生成 ``art_<uuid hex 前 12 位>``。

        Returns:
            ArtifactRef: 产物引用（含 size / md5 / uri）。

        Raises:
            ArtifactQuotaError: 单 artifact 超过 ``max_size`` 或 run 总量超过
                                ``max_total``（写盘前检查，不发事件）。
        """
        # 编码
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
        else:
            content_bytes = content
        size = len(content_bytes)

        # 配额检查（写盘前）
        if self._max_size is not None and size > self._max_size:
            raise ArtifactQuotaError(
                f"产物大小 {size} 超过单 artifact 上限 {self._max_size}"
            )
        if self._max_total is not None and self._total_bytes + size > self._max_total:
            raise ArtifactQuotaError(
                f"产物总量 {self._total_bytes + size} 超过 run 上限 {self._max_total}"
            )

        # 分配 artifact_id
        artifact_id = artifact_id or f"art_{uuid.uuid4().hex[:12]}"
        if artifact_id in self._artifact_ids:
            raise ValueError(f"artifact_id {artifact_id!r} 已存在（同 run 内不可重复）")
        artifact_dir = self._artifact_dir(step_id)
        tmp_path = self._tmp_path(step_id, artifact_id)
        final_path = self._artifact_path(step_id, artifact_id)

        # 步骤 1-2: 写 .tmp + flush + fsync + close
        os.makedirs(artifact_dir, exist_ok=True)
        with open(tmp_path, "wb") as f:
            f.write(content_bytes)
            f.flush()
            os.fsync(f.fileno())

        # 步骤 3: 原子 rename（同文件系统内）
        os.replace(tmp_path, final_path)

        # 计算 md5 + 摘要
        md5 = hashlib.md5(content_bytes).hexdigest()
        if not summary:
            summary = _make_summary(content_bytes)

        # 构造 ArtifactRef
        ref = ArtifactRef(
            id=artifact_id,
            run_id=self.run_id,
            step_id=step_id,
            uri=final_path.replace("\\", "/"),  # 跨平台路径统一
            content_type=content_type,
            size=size,
            md5=md5,
            summary=summary,
            ts=time.time(),
        )

        # 更新配额计数 + 引用缓存
        self._total_bytes += size
        self._artifact_ids.add(artifact_id)
        self._refs_by_id[artifact_id] = ref

        # 步骤 4-5: publish 事件（EventBus 内部保证日志先于分发）
        if self._bus is not None:
            await self._bus.publish(RunEvent(
                run_id=self.run_id,
                type=EventType.ARTIFACT_PRODUCED,
                step_id=step_id,
                payload=ref.to_dict(),
            ))

        return ref

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def read(self, ref: ArtifactRef) -> bytes:
        """读取产物内容（按 :attr:`ArtifactRef.uri`）。

        Args:
            ref: 产物引用。

        Returns:
            bytes: 产物字节内容。

        Raises:
            FileNotFoundError: 文件不存在（理论上不应发生，因 rename 先于 publish）。
        """
        with open(ref.uri, "rb") as f:
            return f.read()

    def list_artifacts(self) -> list[ArtifactRef]:
        """列出本 run 已产出的所有 artifact 引用（按 ts 升序）。

        依据内存 ``_refs_by_id`` 缓存（:meth:`save` 时填充）。
        """
        return sorted(self._refs_by_id.values(), key=lambda r: r.ts)


# ---------------------------------------------------------------------------
# GCSweeper —— 孤儿文件对账
# ---------------------------------------------------------------------------
class GCSweeper:
    """产物孤儿文件对账（对齐 §5.6 GCSweeper）。

    P0 仅实现同步 :meth:`sweep_once`；定时调度（启动 + 每 6 小时）留给 P1
    server lifecycle 集成。

    删除规则：
        1. 所有 ``*.tmp`` 残留 → 立即删（崩溃窗口 1–3 的兜底）
        2. 扫描所有 ``{artifact_id}`` 文件，与 ``events.jsonl`` 中
           ``artifact_produced`` 事件 payload 的 ``id`` 集合对账；
           未被引用且 ``mtime`` 超过 ``orphan_grace_seconds`` → 删（孤儿兜底）

    不删 checkpoint 引用：checkpoint 不持有 artifact 引用，事件日志是唯一对账源。

    Args:
        base_dir:             存储根目录，默认 ``output/runs``。
        orphan_grace_seconds: 孤儿文件宽限期（秒），默认 24h。
    """

    def __init__(
        self,
        *,
        base_dir: str = "output/runs",
        orphan_grace_seconds: float = 24 * 3600,
    ) -> None:
        self._base_dir = base_dir
        self._grace = orphan_grace_seconds

    def sweep_once(self) -> dict[str, int]:
        """执行一次扫描清理，返回统计。

        Returns:
            dict: ``{"deleted_tmp": N, "deleted_orphan": N, "skipped": N}``。
        """
        stats = {"deleted_tmp": 0, "deleted_orphan": 0, "skipped": 0}
        if not os.path.isdir(self._base_dir):
            return stats

        # 1. 扫描所有 .tmp 残留（任何 run / step 目录下）
        for root, _dirs, files in os.walk(self._base_dir):
            for name in files:
                if not name.endswith(".tmp"):
                    continue
                tmp_path = os.path.join(root, name)
                try:
                    os.remove(tmp_path)
                    stats["deleted_tmp"] += 1
                except OSError as exc:
                    logger.warning("删除 .tmp 残留失败 %s: %r", tmp_path, exc)
                    stats["skipped"] += 1

        # 2. 扫描孤儿文件（events.jsonl 中未引用且超宽限期）
        referenced = self._collect_referenced_ids()
        now = time.time()
        for root, _dirs, files in os.walk(self._base_dir):
            for name in files:
                if name == "events.jsonl":
                    continue
                if name.endswith(".tmp"):
                    continue  # 已在步骤 1 处理
                # artifact 文件名 = artifact_id（无后缀）
                artifact_id = name
                if artifact_id in referenced:
                    continue
                path = os.path.join(root, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if now - mtime < self._grace:
                    continue  # 宽限期内
                try:
                    os.remove(path)
                    stats["deleted_orphan"] += 1
                except OSError as exc:
                    logger.warning("删除孤儿文件失败 %s: %r", path, exc)
                    stats["skipped"] += 1

        return stats

    def _collect_referenced_ids(self) -> set[str]:
        """扫描所有 ``events.jsonl``，收集被 ``artifact_produced`` 引用的 artifact id。

        遍历 ``{base_dir}/{run_id}/events.jsonl``，解析 ``ARTIFACT_PRODUCED`` 事件
        的 payload.id。损坏行跳过。
        """
        referenced: set[str] = set()
        if not os.path.isdir(self._base_dir):
            return referenced
        for run_id in os.listdir(self._base_dir):
            log_path = os.path.join(self._base_dir, run_id, "events.jsonl")
            if not os.path.isfile(log_path):
                continue
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if data.get("type") != EventType.ARTIFACT_PRODUCED:
                            continue
                        payload = data.get("payload", {}) or {}
                        art_id = payload.get("id")
                        if art_id:
                            referenced.add(art_id)
            except OSError as exc:
                logger.warning("读取事件日志失败 %s: %r", log_path, exc)
        return referenced
