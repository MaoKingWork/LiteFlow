"""core.checkpoint —— 断点续传检查点存储。

本模块实现 AgentKit 的 ``CheckpointStore``:Workflow 失败后可从检查点恢复,
跳过已完成的 Step,从失败处重新执行。检查点存储以下信息:

    - 已完成 Step 列表(``completed_steps``):resume 时跳过这些 Step。
    - Context 小对象快照(``context_snapshot``):``Context.snapshot()`` 结果,
      小对象完整记录、大对象仅记 ``{type, size, md5, summary}`` 指针。
    - Workflow 元信息(``run_id`` / ``workflow_name`` / ``status`` /
      ``started_at`` / ``updated_at`` / ``error``)。

设计原则
--------
- **高度模块化**:本模块仅依赖标准库与可选的 ``redis``,不依赖任何其他
  agentkit 子模块(``Context`` 仅作为类型在 docstring 中提及,运行时不导入,
  避免 core 子包内循环依赖)。``RedisCheckpointStore`` 的 ``redis`` 依赖延迟
  到 ``__init__`` 内导入,未安装时抛带清晰提示的 :class:`ImportError`。
- **可拓展**:新增后端(如 SQL / Mongo)只需继承 :class:`CheckpointStore`
  并实现四个抽象方法。
- **优化**:仅存小对象快照 + 大对象指针(由 ``Context.snapshot()`` 保证),
  避免检查点膨胀。
- **全异步接口**:所有 store 方法均为 ``async def``,即便后端是同步的(本地
  文件、同步 redis)也用 ``async def`` 包装,便于上层 Workflow 在事件循环中
  无差别 ``await``,也便于后续切换到真正的异步后端(``redis.asyncio``)。

接口契约
--------
:class:`Checkpoint` / :class:`CheckpointStore` 的签名是框架内部共享契约,
:class:`~agentkit.core.workflow.Workflow` 在执行前后调用 ``save`` /
``load`` / ``delete`` / ``list_runs``。修改签名会破坏其他模块,需谨慎。

公开 API:
    - Checkpoint:             单次运行检查点数据类
    - CheckpointStore:        存储抽象接口
    - LocalCheckpointStore:   本地文件系统实现
    - RedisCheckpointStore:   Redis 实现(可选依赖)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields
from typing import Any


# ---------------------------------------------------------------------------
# RunStatus —— 运行状态常量
# ---------------------------------------------------------------------------
class RunStatus:
    """运行状态常量(:attr:`Checkpoint.status` 的合法取值)。

    ``Checkpoint.status`` 为自由 ``str``,此类仅提供常量避免魔法字符串。
    新增状态(``cancelling`` / ``cancelled`` / ``interrupted``)对旧检查点
    无影响——:meth:`Checkpoint.from_dict` 宽松解析,旧代码遇到新状态值不会报错。

    状态机::

        running ──→ completed
          │   └──→ failed
          │   └──→ cancelling ──→ cancelled     (engine 层设置)
          └──（进程重启）──→ interrupted          (Reconciler 设置, P1)

    引擎层仅设置 ``running`` / ``completed`` / ``failed`` / ``cancelled``;
    ``cancelling`` 与 ``interrupted`` 由 runtime/server 层(P1)管理。
    """

    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Checkpoint —— 单次工作流运行的检查点
# ---------------------------------------------------------------------------
@dataclass
class Checkpoint:
    """单个工作流运行的检查点。

    承载一次 Workflow 运行的可恢复状态:运行 id、Workflow 名称、当前状态、
    已完成 Step 列表、Context 快照、起止时间与错误信息。供
    :class:`CheckpointStore` 持久化,失败后从中恢复执行(resume 时跳过
    ``completed_steps`` 中的 Step,从失败处重新执行)。

    Attributes:
        run_id:           唯一运行 id。
        workflow_name:    所属 Workflow 名称,用于按 Workflow 过滤列出 run。
        status:           运行状态,见 :class:`RunStatus`(``running`` / ``completed``
                          / ``failed`` / ``cancelled`` / ``cancelling`` / ``interrupted``)。
        completed_steps:  已完成 step id 的顺序列表(resume 时跳过这些 Step)。
        context_snapshot: ``Context.snapshot()`` 结果(小对象快照 + 大对象指针)。
        started_at:       运行开始时间戳(``time.time()``)。
        updated_at:       最近一次更新时间戳(``save`` 前由调用方更新)。
        error:            失败时的错误信息,成功 / 运行中时为 ``None``。
    """

    run_id: str
    workflow_name: str
    status: str = "running"
    completed_steps: list[str] = field(default_factory=list)
    context_snapshot: dict = field(default_factory=dict)
    started_at: float = 0.0
    updated_at: float = 0.0
    error: str | None = None

    @classmethod
    def new(cls, workflow_name: str, run_id: str | None = None) -> "Checkpoint":
        """创建一个新的 ``running`` 状态检查点。

        Args:
            workflow_name: 所属 Workflow 名称。
            run_id:        自定义运行 id;``None`` 时自动生成
                           ``run_<uuid hex 前 12 位>``。

        Returns:
            Checkpoint: ``status="running"`` 的新检查点,``started_at`` 与
            ``updated_at`` 均设为当前时间戳,``completed_steps`` 为空,
            ``error`` 为 ``None``。
        """
        if run_id is None:
            run_id = f"run_{uuid.uuid4().hex[:12]}"
        now = time.time()
        return cls(
            run_id=run_id,
            workflow_name=workflow_name,
            status="running",
            completed_steps=[],
            context_snapshot={},
            started_at=now,
            updated_at=now,
            error=None,
        )

    def to_dict(self) -> dict:
        """序列化为可 JSON 序列化的 dict。

        使用 :func:`dataclasses.asdict` 递归转换;字段均为基本类型 / dict / list,
        可直接 ``json.dumps``。

        Returns:
            dict: 检查点的字典表示,与 :meth:`from_dict` 互为逆运算。
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        """从 dict 反序列化为 :class:`Checkpoint`。

        宽松处理:仅取已知字段,缺失字段使用 dataclass 默认值,便于向前兼容
        (后续新增字段时旧检查点仍可加载,反之旧代码加载新检查点也不会因
        多余字段报错)。

        Args:
            d: ``to_dict`` 产出的 dict(或任意含部分字段的 dict)。

        Returns:
            Checkpoint: 重建后的检查点。
        """
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# CheckpointStore —— 抽象接口
# ---------------------------------------------------------------------------
class CheckpointStore(ABC):
    """检查点存储抽象接口。

    所有方法均为 ``async``,即便后端是同步的(如本地文件系统、同步 redis),
    也统一用 ``async def`` 包装。这样上层 Workflow 可在事件循环中无差别
    ``await``,也便于后续切换到真正的异步后端(如 ``redis.asyncio``)而无需
    改动调用方代码。

    子类需实现: :meth:`save` / :meth:`load` / :meth:`delete` / :meth:`list_runs`。
    """

    @abstractmethod
    async def save(self, checkpoint: Checkpoint) -> None:
        """持久化(或更新)一个检查点。

        Args:
            checkpoint: 待保存的检查点;若 ``run_id`` 已存在则覆盖。
        """

    @abstractmethod
    async def load(self, run_id: str) -> Checkpoint | None:
        """加载检查点。

        Args:
            run_id: 运行 id。

        Returns:
            Checkpoint | None: 检查点;不存在时返回 ``None``。后端损坏(如
            JSON 解析失败)时也应返回 ``None`` 而非抛异常,保证 resume 健壮性。
        """

    @abstractmethod
    async def delete(self, run_id: str) -> None:
        """删除检查点。

        Args:
            run_id: 运行 id。

        Note:
            不存在的 ``run_id`` 不应报错(no-op),便于幂等清理。
        """

    @abstractmethod
    async def list_runs(self, workflow_name: str | None = None) -> list[str]:
        """列出 run_id。

        Args:
            workflow_name: 可选,按 Workflow 名称过滤;``None`` 时返回全部 run_id。

        Returns:
            list[str]: run_id 列表(顺序不保证,由后端决定)。
        """


# ---------------------------------------------------------------------------
# LocalCheckpointStore —— 本地文件系统实现
# ---------------------------------------------------------------------------
class LocalCheckpointStore(CheckpointStore):
    """本地文件系统检查点存储。

    每个 run 存为 ``{base_dir}/{run_id}.json``。适用于单机开发与测试场景。
    写入采用「写临时文件 + 原子替换」避免写一半导致文件损坏。

    Args:
        base_dir: 存储目录,默认 ``.agentkit_checkpoints``;``save`` 时自动创建。
    """

    def __init__(self, base_dir: str = ".agentkit_checkpoints") -> None:
        self.base_dir = base_dir

    def _path(self, run_id: str) -> str:
        """计算 run_id 对应的文件路径。"""
        return os.path.join(self.base_dir, f"{run_id}.json")

    async def save(self, checkpoint: Checkpoint) -> None:
        """将检查点写入 ``{base_dir}/{run_id}.json``。

        自动创建目录;采用先写 ``.tmp`` 再 ``os.replace`` 原子替换,避免并发
        或中断时产生半截损坏文件。
        """
        os.makedirs(self.base_dir, exist_ok=True)
        path = self._path(checkpoint.run_id)
        data = json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)

    async def load(self, run_id: str) -> Checkpoint | None:
        """从文件加载检查点;文件不存在或 JSON 损坏时返回 ``None``。"""
        path = self._path(run_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            return Checkpoint.from_dict(data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            # 文件不可读 / JSON 损坏 / 结构异常:静默返回 None,保证 resume 健壮
            return None

    async def delete(self, run_id: str) -> None:
        """删除检查点文件;不存在不报错。"""
        path = self._path(run_id)
        try:
            os.remove(path)
        except FileNotFoundError:
            # 幂等:不存在视为已删除
            pass

    async def list_runs(self, workflow_name: str | None = None) -> list[str]:
        """扫描 ``base_dir`` 下 ``*.json``,返回 run_id 列表。

        Args:
            workflow_name: ``None`` 时返回全部 run_id;否则读每个文件取
            ``workflow_name`` 字段匹配(损坏文件跳过)。
        """
        if not os.path.isdir(self.base_dir):
            return []
        runs: list[str] = []
        for name in os.listdir(self.base_dir):
            if not name.endswith(".json"):
                continue
            run_id = name[:-5]  # 去 .json 后缀
            if workflow_name is None:
                runs.append(run_id)
                continue
            # 按 workflow_name 过滤:读文件取 workflow_name 字段
            try:
                with open(self._path(run_id), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("workflow_name") == workflow_name:
                    runs.append(run_id)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                # 损坏文件跳过,不影响其他 run 列举
                continue
        return runs


# ---------------------------------------------------------------------------
# RedisCheckpointStore —— Redis 实现(可选依赖)
# ---------------------------------------------------------------------------
class RedisCheckpointStore(CheckpointStore):
    """Redis 检查点存储(可选依赖 ``redis``)。

    适合多进程 / 多机器共享检查点的场景。``redis`` 包延迟导入:仅在实例化时
    才 ``import redis``,未安装抛 :class:`ImportError` 带清晰安装提示。

    简化实现:所有方法为 ``async def`` 但内部用同步 ``redis.Redis`` 调用
    (足以覆盖单机 / 测试场景)。完整异步实现可改用 ``redis.asyncio.Redis``
    将同步调用替换为 ``await``,接口签名无需变动。

    Args:
        redis_client: 已构造的同步 ``redis.Redis`` 实例;提供时优先使用,
                      忽略 host/port/db。便于测试注入 fakeredis 或复用连接池。
        host:   Redis 主机,``redis_client`` 为 ``None`` 时用于自建客户端。
        port:   Redis 端口。
        db:     Redis db 编号。
        prefix: key 前缀,默认 ``agentkit:checkpoint:``;完整 key 为
                ``{prefix}{run_id}``。
    """

    def __init__(
        self,
        redis_client: Any | None = None,
        *,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        prefix: str = "agentkit:checkpoint:",
    ) -> None:
        # 延迟导入 redis:仅在真正需要时才要求安装,避免硬依赖。
        # 完整异步实现可将此处换为 ``import redis.asyncio as redis`` 并把方法体
        # 内调用改为 ``await``。
        try:
            import redis  # noqa: F401  仅用于触发 ImportError
        except ImportError as e:  # pragma: no cover - 依赖是否安装取决于环境
            raise ImportError(
                "RedisCheckpointStore 需要 redis 包: pip install agentkit[redis]"
            ) from e

        if redis_client is not None:
            self._redis = redis_client
        else:
            self._redis = redis.Redis(host=host, port=port, db=db)
        self.prefix = prefix

    def _key(self, run_id: str) -> str:
        """计算 run_id 对应的 Redis key。"""
        return f"{self.prefix}{run_id}"

    async def save(self, checkpoint: Checkpoint) -> None:
        """将检查点 JSON 写入 ``{prefix}{run_id}``。"""
        payload = json.dumps(checkpoint.to_dict(), ensure_ascii=False)
        self._redis.set(self._key(checkpoint.run_id), payload)

    async def load(self, run_id: str) -> Checkpoint | None:
        """从 Redis 读取检查点;key 不存在或值损坏时返回 ``None``。"""
        raw = self._redis.get(self._key(run_id))
        if raw is None:
            return None
        # redis-py 默认返回 bytes,解码后解析
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            return Checkpoint.from_dict(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            # 值损坏:静默返回 None,保证 resume 健壮
            return None

    async def delete(self, run_id: str) -> None:
        """删除 ``{prefix}{run_id}`` key;不存在不报错(Redis DELETE 本身幂等)。"""
        self._redis.delete(self._key(run_id))

    async def list_runs(self, workflow_name: str | None = None) -> list[str]:
        """扫描 ``{prefix}*`` 列出 run_id;可选按 workflow_name 过滤。

        使用 ``scan_iter`` 增量扫描,避免 ``KEYS`` 阻塞 Redis 的大 keyspace。
        按 workflow_name 过滤时需 load 每个 key 取 ``workflow_name`` 字段。
        """
        runs: list[str] = []
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            if isinstance(key, bytes):
                key = key.decode("utf-8")
            run_id = key[len(self.prefix):]
            if workflow_name is None:
                runs.append(run_id)
                continue
            # 按 workflow_name 过滤:load 每个取 workflow_name 字段
            cp = await self.load(run_id)
            if cp is not None and cp.workflow_name == workflow_name:
                runs.append(run_id)
        return runs


__all__ = [
    "RunStatus",
    "Checkpoint",
    "CheckpointStore",
    "LocalCheckpointStore",
    "RedisCheckpointStore",
]
