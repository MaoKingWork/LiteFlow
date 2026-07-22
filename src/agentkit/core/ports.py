"""core.ports —— 节点端口系统：类型契约、输入绑定、输出校验。

本模块为 Step 引入显式输入/输出端口，叠加在现有 ``{{var}}`` + Context 数据流
之上，为其增加"端口定义 + 类型校验 + 连线校验"的声明式契约层。不声明端口时
行为完全同现状（低下限）；声明端口即启用契约（高上限）。

核心能力：
    - Python 风格自动类型推断（不声明 ``type`` 时不校验）
    - 显式类型契约（``type`` 字符串 / JSON Schema）
    - 严格类型模式（``strict=True`` 默认，拒绝隐式转换如 ``"5"``→``5``）
    - 作用域封闭（``strict_scope=True`` 切断全局 Context 回退，消除幽灵依赖）

设计原则:
    - 高度模块化:仅依赖标准库 + 可选 pydantic,不依赖其他 agentkit 子模块,
      无循环依赖。
    - 安全:类型字符串用 ast 白名单解析器(同 ``template.py`` 安全模型),
      不调用 ``eval``。
    - 无冗余:端口名即 Context key;``output`` 是 ``outputs`` 的语法糖。

公开 API:
    - Port / InputPort / OutputPort:端口数据类
    - PortType:端口类型(封装 pydantic TypeAdapter)
    - PortBindingError / PortTypeError / UndefinedError:异常
    - PortScopeContext / ClosedScopeContext:模板解析作用域
"""

from __future__ import annotations

import ast
import types
import typing
from dataclasses import dataclass, field
from typing import Any

# 可选依赖 pydantic:用于类型校验。不可用时退化为 isinstance 降级校验。
try:  # pragma: no cover - 依赖是否存在取决于运行环境
    from pydantic import TypeAdapter as _TypeAdapter
    from pydantic import create_model as _create_model
except Exception:  # pragma: no cover
    _TypeAdapter = None  # type: ignore[assignment]
    _create_model = None  # type: ignore[assignment]


__all__ = [
    "MISSING",
    "Port",
    "InputPort",
    "OutputPort",
    "PortType",
    "PortBindingError",
    "PortTypeError",
    "UndefinedError",
    "PortScopeContext",
    "ClosedScopeContext",
]


# ---------------------------------------------------------------------------
# MISSING —— default 哨兵
# ---------------------------------------------------------------------------
class _MissingSentinel:
    """``default`` 未提供时的哨兵单例，区别于 ``None`` 这种合法默认值。"""

    _instance: "_MissingSentinel | None" = None

    def __new__(cls) -> "_MissingSentinel":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<MISSING>"


MISSING: Any = _MissingSentinel()


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class PortBindingError(Exception):
    """端口绑定错误：required 输入缺失，或 required 输出未产出。"""


class PortTypeError(Exception):
    """端口类型校验失败。"""


class UndefinedError(KeyError):
    """strict_scope 模式下引用了未声明的变量。

    继承 :class:`KeyError` 以便 ``_resolve_path`` 等现有代码透明传播。
    """


# ---------------------------------------------------------------------------
# 类型字符串 ast 解析器（白名单，安全）
# ---------------------------------------------------------------------------
# 基础类型名 → Python 类型。``any`` 映射到 object（兼容一切）。
_BASE_TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "set": set,
    "bytes": bytes,
    "any": object,
    "none": type(None),
    "null": type(None),
}


def _parse_type_string(spec: str) -> type:
    """把类型字符串解析为 Python 类型对象（ast 白名单解析器）。

    支持::

        str | int | float | bool | list | dict | any
        list[str] | list[dict] | dict[str, int]
        str | None | str | int | list[str] | None

    安全设计:仅放行 ``Name`` / ``Subscript`` / ``Tuple`` / ``BinOp(BitOr)``
    / ``Constant(None)``，禁止属性访问与任意调用。不调用 ``eval``。

    Raises:
        PortTypeError: 语法错误或包含不支持的节点。
    """
    try:
        tree = ast.parse(spec, mode="eval")
    except SyntaxError as e:
        raise PortTypeError(f"类型表达式语法错误: {spec!r}: {e}") from e
    return _eval_type_node(tree.body, spec)


def _eval_type_node(node: ast.AST, spec: str) -> type:
    """递归求值类型 ast 节点（白名单）。"""
    if isinstance(node, ast.Name):
        if node.id in _BASE_TYPES:
            return _BASE_TYPES[node.id]
        raise PortTypeError(
            f"未知的基础类型名: {node.id!r} (在 {spec!r} 中)。"
            f"可用: {sorted(_BASE_TYPES.keys())}"
        )
    if isinstance(node, ast.Constant):
        if node.value is None:
            return type(None)
        raise PortTypeError(f"不支持的常量: {node.value!r} (在 {spec!r} 中)")
    if isinstance(node, ast.Subscript):
        # list[str] / dict[str, int]
        container = _eval_type_node(node.value, spec)
        slice_node = node.slice
        # Python 3.8 兼容:ast.Index 已废弃但旧版可能存在
        if hasattr(ast, "Index") and isinstance(slice_node, ast.Index):  # type: ignore[attr-defined]
            slice_node = slice_node.value  # type: ignore[attr-defined]
        if isinstance(slice_node, ast.Tuple):
            args = tuple(_eval_type_node(e, spec) for e in slice_node.elts)
            return container[args] if len(args) > 1 else container[args[0]]
        arg = _eval_type_node(slice_node, spec)
        return container[arg]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        # str | None / str | int (PEP 604 在 ast 中是 BinOp(BitOr))
        left = _eval_type_node(node.left, spec)
        right = _eval_type_node(node.right, spec)
        return left | right
    raise PortTypeError(
        f"不支持的类型表达式节点: {type(node).__name__} (在 {spec!r} 中)"
    )


# ---------------------------------------------------------------------------
# JSON Schema → Python 类型（pydantic 可用时构造模型）
# ---------------------------------------------------------------------------
def _json_type_to_python(schema: dict) -> type:
    """JSON Schema type 映射为 Python 类型（简化）。"""
    json_type = schema.get("type", "string")
    mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    return mapping.get(json_type, object)


def _schema_to_type(schema: dict) -> type:
    """把 JSON Schema 转为 Python 类型（pydantic 可用时构造 BaseModel）。

    简化实现:仅处理一层 properties;嵌套 object 退化为 dict。
    pydantic 不可用时返回 ``object``（不校验，降级）。
    """
    if _create_model is None:
        return object
    if schema.get("type") != "object":
        # 非对象 schema:直接用 type 映射
        return _json_type_to_python(schema)
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        py_type = _json_type_to_python(prop_schema)
        if prop_name in required:
            fields[prop_name] = (py_type, ...)
        else:
            fields[prop_name] = (py_type | None, None)
    return _create_model("PortSchemaModel", **fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PortType —— 端口类型
# ---------------------------------------------------------------------------
class PortType:
    """端口类型：封装 pydantic TypeAdapter，支持字符串/Schema/类型对象。

    Attributes:
        _py_type:  解析后的 Python 类型对象。
        _spec_str: 原始类型规格字符串（供 repr 与错误信息）。
        _adapter:  pydantic TypeAdapter（不可用时为 None）。
    """

    def __init__(self, py_type: type, spec_str: str | None = None) -> None:
        self._py_type = py_type
        self._spec_str = spec_str or getattr(py_type, "__name__", repr(py_type))
        self._adapter: Any = None
        if _TypeAdapter is not None:
            try:
                self._adapter = _TypeAdapter(py_type)
            except Exception:
                # 某些复杂类型可能无法构造 adapter，降级为 isinstance
                self._adapter = None

    @classmethod
    def parse(cls, spec: Any) -> "PortType | None":
        """从规格构造 PortType。

        Args:
            spec: 类型规格。可为:
                - ``None``:返回 None（不校验，自动推断）。
                - ``str``:类型字符串（如 ``"list[str]"``），用 ast 解析。
                - ``type``:Python 类型对象（如 ``list[str]``）。
                - ``dict``:JSON Schema，用 pydantic 构造模型。
                - ``PortType``:原样返回。

        Returns:
            PortType | None: 解析后的端口类型;``spec`` 为 None 时返回 None。

        Raises:
            PortTypeError: 类型字符串解析失败。
        """
        if spec is None:
            return None
        if isinstance(spec, PortType):
            return spec
        if isinstance(spec, type):
            return cls(spec, spec.__name__)
        if isinstance(spec, str):
            spec = spec.strip()
            py_type = _parse_type_string(spec)
            return cls(py_type, spec)
        if isinstance(spec, dict):
            # JSON Schema
            py_type = _schema_to_type(spec)
            return cls(py_type, "schema")
        raise PortTypeError(f"无法解析的类型规格: {spec!r}")

    def validate(self, value: Any, *, strict: bool = True) -> Any:
        """校验值是否匹配类型。

        Args:
            value:  待校验的值。
            strict: ``True``（默认）严格模式，拒绝隐式转换（``"5"`` 声明 ``int``
                    → 报错）;``False`` 允许 pydantic 宽容转换。

        Returns:
            Any: 校验通过的值（strict 模式下原样返回;宽松模式可能被转换）。

        Raises:
            PortTypeError: 类型不匹配。
        """
        if self._py_type is object:
            # any 类型：不校验
            return value
        if self._adapter is not None:
            try:
                return self._adapter.validate_python(value, strict=strict)
            except Exception as e:
                raise PortTypeError(
                    f"类型校验失败(期望 {self._spec_str}, strict={strict}): "
                    f"实际类型 {type(value).__name__}, 值摘要={_value_summary(value)}。{e}"
                ) from e
        # pydantic 不可用：降级为 isinstance 检查
        if _isinstance_loose(value, self._py_type):
            return value
        raise PortTypeError(
            f"类型校验失败(期望 {self._spec_str}): "
            f"实际类型 {type(value).__name__}, 值摘要={_value_summary(value)}"
        )

    def is_compatible(self, other: "PortType | None") -> bool:
        """判断本类型是否兼容另一类型（用于静态校验的 warning）。

        规则:
            - ``other`` 为 None(未声明):兼容(运行时再校验)。
            - 任一方是 ``object``(any):兼容。
            - 完全相同:兼容。
            - 联合类型:other 的所有成员都是 self 某成员的子类则兼容。
            - 子类关系:other 是 self 的子类则兼容。
            - 否则:不兼容。
        """
        if other is None:
            return True
        if self._py_type is object or other._py_type is object:
            return True
        if self._py_type == other._py_type:
            return True
        # 联合类型成员分解
        self_members = _union_members(self._py_type)
        other_members = _union_members(other._py_type)
        for o in other_members:
            if not any(
                isinstance(s, type) and isinstance(o, type) and issubclass(o, s)
                for s in self_members
            ):
                return False
        return True

    @property
    def spec_str(self) -> str:
        return self._spec_str

    def __repr__(self) -> str:
        return f"PortType({self._spec_str})"


def _union_members(t: type) -> set[type]:
    """分解联合类型的成员集合。"""
    if isinstance(t, types.UnionType):
        result: set[type] = set()
        for arg in typing.get_args(t):
            result |= _union_members(arg)
        return result
    return {t}


def _isinstance_loose(value: Any, py_type: type) -> bool:
    """降级 isinstance 检查（pydantic 不可用时）。

    对联合类型检查任一成员匹配;对参数化容器只检查外层类型。
    """
    members = _union_members(py_type)
    return any(isinstance(value, m) for m in members if isinstance(m, type))


def _value_summary(value: Any, max_len: int = 100) -> str:
    """值摘要（截断 repr），供错误信息。"""
    try:
        text = repr(value)
    except Exception:
        text = f"<{type(value).__name__}>"
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


# ---------------------------------------------------------------------------
# Port —— 端口数据类
# ---------------------------------------------------------------------------
@dataclass
class Port:
    """端口基类。

    Attributes:
        name:        端口名 = 变量名 = Context key。
        type:        类型契约(PortType 或可解析规格)。None = 不校验。
        required:    输入:是否必须有来源值;输出:是否必须产出。默认 True。
        strict:      类型校验是否严格。True(默认)拒绝隐式转换。
        default:     required=False 时的默认值(仅输入端口)。
        description: 文档说明。
    """

    name: str
    type: PortType | None = None
    required: bool = True
    strict: bool = True
    default: Any = MISSING
    description: str = ""


@dataclass
class InputPort(Port):
    """输入端口。

    Attributes:
        from_: 来源 Context key;None 时等于 name。
    """

    from_: str | None = None

    @property
    def source(self) -> str:
        """返回实际来源 key（from_ 为 None 时等于 name）。"""
        return self.from_ if self.from_ is not None else self.name


@dataclass
class OutputPort(Port):
    """输出端口。name 即写入的 Context key。"""


# ---------------------------------------------------------------------------
# 作用域 Context —— 模板解析时叠加端口绑定
# ---------------------------------------------------------------------------
class PortScopeContext:
    """端口作用域上下文：在父 Context 上叠加端口绑定。

    供 ``BaseStep._render`` 使用：``get`` 优先返回端口绑定值（已校验类型、
    已按 ``from`` 取源），其余透传父 Context。实现 Context 的 ``get`` 契约
    （缺失抛 KeyError），使其可被 ``_resolve_path`` / ``resolve_template``
    透明使用（duck typing）。

    非封闭模式（``strict_scope=False``）：未在端口声明的变量回退到父 Context。
    """

    __slots__ = ("_parent", "_bindings")

    def __init__(self, parent: Any, bindings: dict[str, Any]) -> None:
        self._parent = parent
        self._bindings = bindings

    def get(self, key: str, default: Any = MISSING) -> Any:
        if key in self._bindings:
            return self._bindings[key]
        if default is MISSING:
            return self._parent.get(key)
        return self._parent.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._bindings or self._parent.has(key)


class ClosedScopeContext:
    """封闭作用域上下文：仅允许端口绑定中的变量（strict_scope=True）。

    引用未声明的变量直接抛 :class:`UndefinedError`，切断对全局 Context 的
    回退，消除"幽灵依赖"。
    """

    __slots__ = ("_bindings",)

    def __init__(self, bindings: dict[str, Any]) -> None:
        self._bindings = bindings

    def get(self, key: str, default: Any = MISSING) -> Any:
        if key in self._bindings:
            return self._bindings[key]
        # 封闭模式：未声明的变量直接报错（不回退全局 Context）
        if default is not MISSING:
            return default
        raise UndefinedError(
            f"strict_scope 模式下引用了未声明的变量: {key!r}。"
            f"已声明端口: {list(self._bindings.keys())}"
        )

    def has(self, key: str) -> bool:
        return key in self._bindings
