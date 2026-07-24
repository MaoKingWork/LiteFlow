"""core.context —— 不可变上下文与引用污染根治。

本模块实现 AgentKit 的 ``Context``：智能体运行期间承载变量、消息与大对象
摘要的「不可变写入 + 只读读取」容器。核心目标是根治引用污染——杜绝 Step
之间通过共享可变对象产生隐式耦合。

不可变策略：
    - 小对象（<= ``large_object_threshold``）：``set`` 时经 ``_deep_freeze``
      递归冻结为不可变结构（dict→``FrozenDict``，list/tuple→``tuple``，
      set→``frozenset``，任意对象→``ReadOnlyProxy``）。``get`` 直接返回该
      冻结结构，零拷贝且天然只读。
    - 大对象（> 阈值）：``set`` 时包装为 ``LargeRef``，仅持有原始引用 +
      摘要/size/md5，``get`` 返回 ``ReadOnlyProxy`` 只读代理，避免全量拷贝。
    - 任何对返回视图的变更操作（``__setitem__`` / ``__setattr__`` /
      ``pop`` / ``append`` 等）均抛 ``RuntimeError``。Step 需修改数据须
      ``copy.deepcopy(ctx.get(key))`` 后 ``ctx.set(key, modified)``。
    - ``evict`` 显式释放大对象内存，仅保留摘要；``snapshot`` 序列化时大对象
      只记 ``{type,size,md5,summary}``，供检查点持久化。

模块化：仅依赖 ``agentkit.config``（与可选的第三方 ``pydantic``），不依赖
其他 agentkit 子模块，避免循环依赖。

线程安全说明：本模块未加锁，单线程内同步访问安全；跨线程动态读写需外部加锁。

公开 API：
    - Context:       不可变上下文容器
    - FrozenDict:    不可变字典视图（collections.abc.Mapping 子类）
    - ReadOnlyProxy: 任意对象的只读代理
    - LargeRef:      大对象引用 + 摘要
    - to_mutable:    递归解冻 Context 数据为可变 JSON 友好结构
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import sys
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from agentkit.config import get_default

# 可选依赖 pydantic：存在时将 BaseModel 实例视为只读直接返回，避免包装
try:  # pragma: no cover - 依赖是否存在取决于运行环境
    from pydantic import BaseModel as _BaseModel
except Exception:  # pragma: no cover
    _BaseModel = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 不可变异常文案与统一拦截器
# ---------------------------------------------------------------------------
_IMMUTABLE_MSG = "Context 数据不可变,请 copy.deepcopy 后 set 新值"


def _raise_immutable(self: Any, *args: Any, **kwargs: Any) -> Any:
    """所有变更操作的统一拦截器：抛 ``RuntimeError``。"""
    raise RuntimeError(_IMMUTABLE_MSG)


# ---------------------------------------------------------------------------
# _MISSING —— get() 默认值哨兵
# ---------------------------------------------------------------------------
class _MissingSentinel:
    """``get`` 未提供 default 时的哨兵单例，区别于 ``None`` 这种合法默认值。"""

    _instance: "_MissingSentinel | None" = None

    def __new__(cls) -> "_MissingSentinel":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<MISSING>"


_MISSING: Any = _MissingSentinel()


# ---------------------------------------------------------------------------
# FrozenDict —— 不可变字典视图（collections.abc.Mapping 子类）
# ---------------------------------------------------------------------------
class FrozenDict(Mapping):
    """不可变字典视图（``collections.abc.Mapping`` 子类）。

    包装一个普通 dict（其 value 已由 ``_deep_freeze`` 递归冻结），对外暴露
    只读 ``Mapping`` 接口。继承 ``Mapping`` 使 ``isinstance(fd, Mapping)``
    为真，可与 jsonschema / requests.json= / ORM 等检查 ``Mapping`` 的第三方
    库无缝集成。

    所有变更操作（``__setitem__`` / ``__delitem__`` / ``pop`` / ``clear`` /
    ``update`` / ``setdefault`` 等）抛 ``RuntimeError``。

    ``copy.deepcopy`` 会返回一个**可变** plain dict，以便 Step 修改后通过
    ``Context.set`` 写回（此时会被再次冻结）。
    """

    __slots__ = ("_data",)

    def __init__(self, data: Any) -> None:
        # value 已冻结；这里仅做浅拷贝避免持有外部 dict 引用
        object.__setattr__(self, "_data", dict(data))

    # ---- Mapping 抽象方法 ----
    def __getitem__(self, key: Any) -> Any:
        return self._data[key]

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Any:
        return iter(self._data)

    # ---- 性能优化：O(1) 查找，优于 Mapping 默认的 try/except 实现 ----
    def __contains__(self, key: object) -> bool:
        return key in self._data

    def get(self, key: Any, default: Any = None) -> Any:
        return self._data.get(key, default)

    # ---- 变更操作：统一拦截 ----
    __setitem__ = _raise_immutable  # type: ignore[assignment]
    __delitem__ = _raise_immutable  # type: ignore[assignment]
    __setattr__ = _raise_immutable  # type: ignore[assignment]
    __delattr__ = _raise_immutable  # type: ignore[assignment]
    pop = _raise_immutable  # type: ignore[assignment]
    popitem = _raise_immutable  # type: ignore[assignment]
    clear = _raise_immutable  # type: ignore[assignment]
    update = _raise_immutable  # type: ignore[assignment]
    setdefault = _raise_immutable  # type: ignore[assignment]

    # ---- 双下方法 ----
    def __repr__(self) -> str:
        return f"FrozenDict({self._data!r})"

    def __eq__(self, other: object) -> bool:
        # 直接比较底层 dict，避免 Mapping 默认 __eq__ 的 dict() 重建开销
        if isinstance(other, FrozenDict):
            return self._data == other._data
        if isinstance(other, Mapping):
            return self._data == dict(other)
        return NotImplemented

    # 定义 __eq__ 后 Python 会将 __hash__ 置为 None（不可哈希），与 Mapping 一致
    __hash__ = None  # type: ignore[assignment]

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[Any, Any]:
        # 返回可变 dict 副本：Step 可就地修改后 set 回 Context
        return {
            copy.deepcopy(k, memo): copy.deepcopy(v, memo)
            for k, v in self._data.items()
        }


# ---------------------------------------------------------------------------
# ReadOnlyProxy —— 任意对象的只读代理
# ---------------------------------------------------------------------------
class ReadOnlyProxy:
    """任意对象的只读代理。

    包装目标对象，代理属性读取（``__getattr__``）与 item 读取（``__getitem__``），
    拦截一切变更操作（``__setattr__`` / ``__setitem__`` / ``__delattr__`` /
    ``__delitem__`` / ``pop`` / ``append`` / ``extend`` / ``clear`` / ``update``
    等）抛 ``RuntimeError``。

    设计权衡：``__getitem__`` 返回的子对象经 ``_deep_freeze`` 冻结，保证读取
    链路同样不可变。大对象以引用 + 代理方式存储以避免全量拷贝，仅在访问具体
    子项时按需冻结该子项。``copy.deepcopy(proxy)`` 返回目标对象的可变深拷贝，
    便于 Step 修改后写回。
    """

    __slots__ = ("_target",)

    def __init__(self, target: Any) -> None:
        object.__setattr__(self, "_target", target)

    # ---- 读取代理 ----
    def __getattr__(self, name: str) -> Any:
        # _target 在 __slots__ 中，正常查找即可命中，不会进入 __getattr__；
        # 此处加守卫防止 _target 未设置时递归调用 __getattr__ 自身。
        if name == "_target":
            raise AttributeError(name)
        return getattr(self._target, name)

    def __getitem__(self, key: Any) -> Any:
        # 子对象按需冻结，保证读取链路不可变
        return _deep_freeze(self._target[key])

    def __iter__(self) -> Any:
        return iter(self._target)

    def __len__(self) -> int:
        return len(self._target)

    def __contains__(self, item: object) -> bool:
        return item in self._target

    def __repr__(self) -> str:
        return f"ReadOnlyProxy({self._target!r})"

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        # 解包代理：返回目标对象的可变深拷贝，供 Step 修改后 set 回去
        return copy.deepcopy(self._target, memo)

    # ---- 变更操作：统一拦截 ----
    __setattr__ = _raise_immutable  # type: ignore[assignment]
    __delattr__ = _raise_immutable  # type: ignore[assignment]
    __setitem__ = _raise_immutable  # type: ignore[assignment]
    __delitem__ = _raise_immutable  # type: ignore[assignment]
    pop = _raise_immutable  # type: ignore[assignment]
    append = _raise_immutable  # type: ignore[assignment]
    extend = _raise_immutable  # type: ignore[assignment]
    insert = _raise_immutable  # type: ignore[assignment]
    remove = _raise_immutable  # type: ignore[assignment]
    clear = _raise_immutable  # type: ignore[assignment]
    update = _raise_immutable  # type: ignore[assignment]
    popitem = _raise_immutable  # type: ignore[assignment]
    sort = _raise_immutable  # type: ignore[assignment]
    reverse = _raise_immutable  # type: ignore[assignment]
    setdefault = _raise_immutable  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# LargeRef —— 大对象引用 + 摘要
# ---------------------------------------------------------------------------
class LargeRef:
    """大对象引用 + 摘要。

    持有原始大对象引用，避免在 set/get 时全量拷贝；同时预先计算 size / md5 /
    summary，供快照与 ``evict`` 使用。``evict`` 后原始引用置 ``None``，仅保留
    摘要信息。
    """

    __slots__ = ("_obj", "_size", "_md5", "_summary", "_type", "_evicted")

    def __init__(self, obj: Any) -> None:
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_size", _sizeof(obj))
        object.__setattr__(self, "_type", type(obj).__name__)
        # md5 基于 repr，便于跨进程对齐；repr 失败时退化为占位摘要
        try:
            rep = repr(obj)
            digest = hashlib.md5(rep.encode("utf-8", errors="replace")).hexdigest()
        except Exception:
            rep = "<unrepr-obj>"
            digest = hashlib.md5(b"<unrepr-obj>").hexdigest()
        object.__setattr__(self, "_md5", digest)
        summary_len = int(get_default("context_snapshot_big_object_summary_len"))
        object.__setattr__(self, "_summary", rep[:summary_len])
        object.__setattr__(self, "_evicted", False)

    @property
    def proxy(self) -> ReadOnlyProxy | None:
        """返回原始对象的只读代理；已 evict 时返回 ``None``。"""
        if self._evicted:
            return None
        return ReadOnlyProxy(self._obj)

    def evict(self) -> None:
        """释放原始对象内存，仅保留摘要。"""
        object.__setattr__(self, "_obj", None)
        object.__setattr__(self, "_evicted", True)

    @property
    def size(self) -> int:
        return self._size

    @property
    def md5(self) -> str:
        return self._md5

    @property
    def summary(self) -> str:
        return self._summary

    @property
    def type_name(self) -> str:
        return self._type

    @property
    def evicted(self) -> bool:
        return self._evicted

    # 大对象引用本身也不应被外部改写
    __setattr__ = _raise_immutable  # type: ignore[assignment]
    __delattr__ = _raise_immutable  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 辅助函数：_sizeof / _deep_freeze / _to_jsonable / _trace_to_dict
# ---------------------------------------------------------------------------
def _sizeof(obj: Any) -> int:
    """递归估算对象总占用字节。

    对 dict/list/tuple/set/frozenset 递归累加元素大小；对 str/bytes，
    ``sys.getsizeof`` 已含内容，无需递归；其他类型直接用 ``sys.getsizeof``。
    """
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(_sizeof(k) + _sizeof(v) for k, v in obj.items())
    elif isinstance(obj, (list, tuple, set, frozenset)):
        size += sum(_sizeof(item) for item in obj)
    # str/bytes/其他标量：sys.getsizeof 已足够
    return size


def _deep_freeze(obj: Any) -> Any:
    """递归冻结对象为不可变结构。

    - dict → ``FrozenDict``（递归冻结 value；key 假定已不可变）
    - list/tuple → ``tuple``（递归冻结每个元素）
    - set → ``frozenset``（递归冻结每个元素）
    - int/float/str/bytes/bool/None → 原样返回
    - 已是 ``FrozenDict`` / ``frozenset`` / ``MappingProxyType`` → 原样返回
    - pydantic ``BaseModel`` 实例 → 原样返回（字段多不可变）
    - 其他对象 → 包装 ``ReadOnlyProxy``
    """
    if obj is None:
        return None
    # 标量（bool 是 int 子类，一并匹配，原样返回）
    if isinstance(obj, (int, float, str, bytes)):
        return obj
    # 已冻结容器：直接返回
    if isinstance(obj, (FrozenDict, frozenset)):
        return obj
    if isinstance(obj, MappingProxyType):
        return obj
    # dict → FrozenDict
    if isinstance(obj, dict):
        return FrozenDict({k: _deep_freeze(v) for k, v in obj.items()})
    # list/tuple → tuple
    if isinstance(obj, (list, tuple)):
        return tuple(_deep_freeze(v) for v in obj)
    # set → frozenset
    if isinstance(obj, set):
        return frozenset(_deep_freeze(v) for v in obj)
    # pydantic BaseModel：字段多不可变，视为只读直接返回
    if _BaseModel is not None and isinstance(obj, _BaseModel):
        return obj
    # 其他对象：包装只读代理
    return ReadOnlyProxy(obj)


def _json_key(k: Any) -> Any:
    """dict key 转 JSON 友好形式（str/int/float/bool/None 原样，其余转 str）。"""
    if k is None or isinstance(k, (str, int, float, bool)):
        return k
    return str(k)


def _to_jsonable(obj: Any) -> Any:
    """递归转为 JSON 友好结构（dict/list/str/number/bool/None）。

    遇到不可直接序列化的对象（set/bytes/自定义类型等）：
    - set/frozenset → ``{"_type": "set", "_items": [...]}``
    - bytes → ``{"_type": "bytes", "_data": "..."}``
    - 其他 → ``{"_type": type(obj).__name__, "_repr": repr(obj)[:200]}``
    """
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return {"_type": "bytes", "_data": obj.decode("utf-8", errors="replace")}
    # FrozenDict / dict / MappingProxyType 统一为 Mapping 处理
    if isinstance(obj, Mapping):
        return {_json_key(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return {"_type": "set", "_items": [_to_jsonable(v) for v in obj]}
    if isinstance(obj, ReadOnlyProxy):
        # 解包代理后继续转换
        return _to_jsonable(obj._target)
    # 兜底：不可序列化对象记类型与截断 repr
    return {"_type": type(obj).__name__, "_repr": repr(obj)[:200]}


def _from_jsonable(obj: Any) -> Any:
    """递归还原 :func:`_to_jsonable` 产生的类型标记，恢复原始类型。

    与 :func:`_to_jsonable` 互为逆运算，用于 :meth:`Context.restore` 时将
    JSON 友好结构还原为 Python 原生类型，避免跨 resume 后类型漂移。

    还原规则：
        - ``{"_type": "set", "_items": [...]}`` → ``set``
        - ``{"_type": "bytes", "_data": "..."}`` → ``bytes``
        - ``{"_type": <其他>, "_repr": "..."}`` → 保留为 dict（不可恢复）
        - 普通 dict / list → 递归处理
        - 标量（int/float/str/bool/None）→ 原样返回

    注意：若用户数据恰好含有 ``_type`` + ``_items`` / ``_data`` 键名组合，
    会被误判为类型标记还原。此为 ``_to_jsonable`` 约定的已知限制，实际
    场景极少冲突。
    """
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float, str)):
        return obj
    if isinstance(obj, dict):
        _type = obj.get("_type")
        if _type == "set" and "_items" in obj:
            return set(_from_jsonable(v) for v in obj["_items"])
        if _type == "bytes" and "_data" in obj:
            return obj["_data"].encode("utf-8", errors="replace")
        if _type is not None and "_repr" in obj:
            # 不可恢复的自定义类型，保留为 dict
            return obj
        # 普通 dict：递归处理 value
        return {k: _from_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_jsonable(v) for v in obj]
    return obj


def to_mutable(obj: Any) -> Any:
    """递归解冻 Context 数据为可变 JSON 友好结构。

    把 ``Context.get()`` 返回的只读视图（``FrozenDict`` / ``tuple`` /
    ``frozenset`` / ``ReadOnlyProxy``）递归转为标准可变 Python 结构，
    便于传给第三方库（jsonschema / ORM / ``requests.json=`` 等）。

    转换规则：
        - ``FrozenDict`` / ``dict`` / ``MappingProxyType`` → ``dict``（递归 value）
        - ``tuple`` / ``list`` → ``list``（递归元素；tuple→list 恢复 JSON array 语义）
        - ``frozenset`` / ``set`` → ``list``（JSON 无 set 类型）
        - ``ReadOnlyProxy`` → 解包目标后递归
        - 其他标量（int/str/bytes/对象）原样返回

    与 ``copy.deepcopy`` 的区别：``deepcopy(FrozenDict)`` 返回 dict 但
    ``tuple`` 仍是 tuple；``to_mutable`` 则把 tuple 转为 list，更贴合 JSON
    互操作场景。

    Args:
        obj: 任意值（通常是 ``Context.get()`` 的返回）。

    Returns:
        Any: 可变 Python 原生结构。
    """
    if isinstance(obj, Mapping):
        return {to_mutable(k): to_mutable(v) for k, v in obj.items()}
    if isinstance(obj, ReadOnlyProxy):
        return to_mutable(obj._target)
    if isinstance(obj, (list, tuple)):
        return [to_mutable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [to_mutable(v) for v in obj]
    return obj


def _trace_to_dict(trace: Any) -> Any:
    """将一条 StepTrace 序列化为 JSON 友好结构。

    dataclass 实例用 ``dataclasses.asdict`` 递归转 dict；否则退化为 ``repr``。
    """
    if dataclasses.is_dataclass(trace) and not isinstance(trace, type):
        try:
            return dataclasses.asdict(trace)
        except Exception:
            return repr(trace)
    return repr(trace)


# ---------------------------------------------------------------------------
# Context —— 不可变上下文容器
# ---------------------------------------------------------------------------
class Context:
    """不可变上下文容器。

    承载智能体运行期间的变量、消息与大对象摘要。对外提供「不可变写入 +
    只读读取」语义：写入时小对象递归冻结、大对象存 ``LargeRef`` 引用；
    读取始终返回只读视图，变更抛 ``RuntimeError``。

    典型用法::

        ctx.set("data", {"x": [1, 2, 3]})
        v = ctx.get("data")          # 只读视图
        m = copy.deepcopy(v)         # Step 需修改必须先深拷贝
        m["x"] = 99
        ctx.set("data", m)           # 再写回（重新冻结）
    """

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._traces: list[Any] = []

    # ---- 写入 ----
    def set(self, key: str, value: Any) -> None:
        """写入：小对象递归冻结存储；大对象（>阈值）存 ``LargeRef`` 引用。

        Args:
            key:   键名。
            value: 任意对象。小对象经 ``_deep_freeze`` 冻结后存储；大于
                   ``large_object_threshold`` 的对象包装为 ``LargeRef``，
                   仅持引用 + 摘要，避免拷贝。
        """
        threshold = int(get_default("large_object_threshold"))
        if _sizeof(value) > threshold:
            self._store[key] = LargeRef(value)
        else:
            self._store[key] = _deep_freeze(value)

    # ---- 读取 ----
    def get(self, key: str, default: Any = _MISSING) -> Any:
        """读取：始终返回只读视图。

        - 小对象：返回已冻结的结构（``FrozenDict`` / ``tuple`` / ``frozenset``
          / ``ReadOnlyProxy`` 等）。
        - 大对象（未 evict）：返回 ``LargeRef.proxy``（``ReadOnlyProxy``）。
        - 大对象（已 evict）：返回含 ``summary`` 的占位 ``FrozenDict``。
        - 缺失 key：未给 ``default`` 抛 ``KeyError``；给了则返回 ``default``。

        Args:
            key:     键名。
            default: 缺失时的默认值；未提供则缺失时抛 ``KeyError``。

        Returns:
            Any: 只读视图或默认值。

        Raises:
            KeyError: ``key`` 不存在且未提供 ``default``。
        """
        if key not in self._store:
            if default is _MISSING:
                raise KeyError(key)
            return default
        stored = self._store[key]
        if isinstance(stored, LargeRef):
            if stored.evicted:
                # 已 evict：返回含摘要的占位（冻结视图）
                return FrozenDict(
                    {
                        "_evicted": True,
                        "type": stored.type_name,
                        "size": stored.size,
                        "md5": stored.md5,
                        "summary": stored.summary,
                    }
                )
            return stored.proxy
        return stored

    def has(self, key: str) -> bool:
        """判断 key 是否存在。"""
        return key in self._store

    def keys(self) -> list[str]:
        """返回所有 key 的列表副本。"""
        return list(self._store.keys())

    # ---- 大对象回收 ----
    def evict(self, key: str) -> None:
        """显式释放大对象内存，仅保留摘要。对小对象无操作。

        Args:
            key: 键名。若对应值为 ``LargeRef``，则置空原始引用并标记 evicted；
                 否则 no-op。
        """
        stored = self._store.get(key)
        if isinstance(stored, LargeRef):
            stored.evict()

    # ---- 追踪 ----
    def add_trace(self, trace: Any) -> None:
        """追加一条 StepTrace。

        ``trace`` 类型在 ``steps.base`` 中定义，此处用 ``Any`` 避免循环依赖。

        Args:
            trace: ``StepTrace`` 实例（或任意可序列化对象）。
        """
        self._traces.append(trace)

    def get_traces(self) -> list[Any]:
        """返回所有 StepTrace 的只读副本（新 list，不影响内部状态）。"""
        return list(self._traces)

    # ---- 快照 / 恢复 ----
    def snapshot(self) -> dict:
        """序列化供检查点持久化。

        - 小对象：递归转为 JSON 友好结构。
        - 大对象（含已 evict）：只记
          ``{"_evicted_big": True, "type":..., "size":..., "md5":..., "summary":...}``。
        - traces：dataclass 用 ``asdict``，否则 ``repr``。

        Returns:
            dict: ``{"store": {...}, "meta": [...traces...]}``。
        """
        store_snap: dict[str, Any] = {}
        for key, val in self._store.items():
            if isinstance(val, LargeRef):
                store_snap[key] = {
                    "_evicted_big": True,
                    "type": val.type_name,
                    "size": val.size,
                    "md5": val.md5,
                    "summary": val.summary,
                }
            else:
                store_snap[key] = _to_jsonable(val)
        # meta（StepTrace）经 _trace_to_dict 转 dict 后，仍可能含 set/frozenset
        # 等不可 JSON 序列化的值（如 tool_calls.arguments 中的 set），需再经
        # _to_jsonable 清理，否则 json.dumps 崩溃（LITE-001）。
        meta = [_to_jsonable(_trace_to_dict(t)) for t in self._traces]
        return {"store": store_snap, "meta": meta}

    @classmethod
    def restore(cls, snapshot: dict) -> "Context":
        """从快照重建 Context（用于 resume）。

        - 小对象：直接 ``set``（再次冻结）。
        - 大对象（``_evicted_big`` 标记）：恢复为占位
          ``{"restored_from_checkpoint": True, "summary": ...}``。
        - meta：直接装回 traces。

        Args:
            snapshot: ``snapshot()`` 产生的 dict。

        Returns:
            Context: 重建后的上下文。
        """
        ctx = cls()
        store = snapshot.get("store", {}) or {}
        for key, val in store.items():
            if isinstance(val, dict) and val.get("_evicted_big") is True:
                ctx.set(
                    key,
                    {
                        "restored_from_checkpoint": True,
                        "summary": val.get("summary", ""),
                    },
                )
            else:
                # _from_jsonable 还原 _to_jsonable 产生的类型标记
                # （set/frozenset/bytes），避免跨 resume 后类型漂移（LITE-001）。
                ctx.set(key, _from_jsonable(val))
        ctx._traces = [_from_jsonable(t) for t in (snapshot.get("meta", []) or [])]
        return ctx


__all__ = ["Context", "FrozenDict", "ReadOnlyProxy", "LargeRef", "to_mutable"]
