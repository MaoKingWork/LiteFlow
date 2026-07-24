"""tools.base —— Tool 工具统一接口与注册机制。

本模块定义 AgentKit 中所有工具的统一抽象基类 ``Tool``，以及配套的
``@tool`` 装饰器与 ``ToolRegistry`` 注册表。

设计要点：
    - 三种语义角色（Source / Action / Sink）共享同一接口，``role`` 字段
      仅用于可观测性与编排提示，不参与接口分派。
    - ``Tool`` 为抽象基类，子类必须实现 ``call``；可选实现 ``param_model``
      属性以声明参数 Pydantic Model，用于静态校验与 JSON Schema 生成。
    - ``schema`` 属性自动生成 Function Call 用的 JSON Schema，优先使用
      ``param_model``，否则返回空 object 占位 schema。
    - 通过 ``@tool`` 装饰器或 ``register`` 函数将工具注册到全局注册表，
      支持「实例或类」两种形式（类会在注册时实例化）。
    - 模块仅依赖 ``pydantic`` 与 ``TYPE_CHECKING`` 下的 ``Context``，不依赖
      其他 agentkit 子模块，避免循环依赖。

公开 API：
    - Tool:            工具抽象基类
    - tool:            装饰器，注册 Tool 子类到全局注册表
    - ToolRegistry:    工具注册表
    - register:        注册工具到全局注册表
    - get_tool:        按名取工具实例
    - list_tools:      列出所有已注册工具名
"""

from __future__ import annotations

import inspect
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from agentkit.core.context import Context


# ---------------------------------------------------------------------------
# 辅助：CamelCase / PascalCase 转 snake_case
# ---------------------------------------------------------------------------
# 处理连续大写（如 HTTPClient -> http_client）与大小写边界（如 MyTool -> my_tool）。
# 两条替换规则：
#   1. 任意字符后跟「大写+小写」组合，在边界插入下划线：HTTPClient -> HTTP_Client
#   2. 小写/数字后跟大写，在边界插入下划线：MyTool -> My_Tool
# 最后整体小写。
def _to_snake_case(name: str) -> str:
    """将类名转换为 snake_case。

    Args:
        name: 原始类名，例如 ``HTTPClient`` / ``MyTool`` / ``DB``。

    Returns:
        str: snake_case 形式，例如 ``http_client`` / ``my_tool`` / ``db``。
    """
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    return s2.lower()


# ---------------------------------------------------------------------------
# 辅助：可调用的 schema dict
# ---------------------------------------------------------------------------
class _CallableSchemaDict(dict):
    """可调用的 schema dict，同时支持 ``.schema``（property 访问）与
    ``.schema()``（调用）两种用法。

    spec 中 ``schema`` 声明为 ``@property`` 返回 dict，而部分调用方使用
    ``.schema()`` 形式访问。本类在 dict 基础上增加 ``__call__`` 返回自身，
    使两种访问形式都得到 dict 结果，兼容两类调用方。它本质仍是 dict，
    ``isinstance(x, dict)``、``json.dumps``、``==`` 比较等行为均与普通 dict 一致。
    """

    def __call__(self) -> dict:
        return self


# ---------------------------------------------------------------------------
# Tool 抽象基类
# ---------------------------------------------------------------------------
class Tool(ABC):
    """工具统一接口。

    Source / Action / Sink 三种语义角色共享此接口。``role`` 字段仅用于
    可观测性与编排提示，不影响接口分派；子类按需在类上声明 ``role`` 即可。

    类属性：
        role:        工具语义角色，``source`` | ``action`` | ``sink``。
        name:        注册名，由 ``@tool`` 装饰器或子类显式设置。
        description: 供 LLM Function Call 理解用途的自然语言描述。
        execution:   执行模式，``inline`` | ``thread`` | ``process``。
                     默认 ``inline``（直接 ``await``，行为同现状，零侵入）。
                     ``thread`` 经共享 ``ThreadPoolExecutor`` 卸载到子线程，
                     适用于同步阻塞库（reportlab / python-docx / markdown2）；
                     ``process`` 经 ``ProcessPoolExecutor`` 卸载到子进程，
                     契约：params/result 仅 JSON 可序列化、Context 不进子进程。
                     详见 :class:`agentkit.runtime.blocking.BlockingExecutor`。
    """

    role: str = "action"
    name: str = ""
    description: str = ""
    execution: str = "inline"

    @abstractmethod
    async def call(self, params: dict, ctx: "Context") -> dict:
        """执行工具，返回结果 dict。

        Args:
            params: 调用参数，已由调用方（如 Function Call 调度器）解析为 dict。
            ctx:    会话上下文，**只读**。如需修改上下文必须通过 ``ctx.set``，
                    避免直接篡改内部状态。

        Returns:
            dict: 工具执行结果，结构由具体工具自行约定。
        """

    @property
    def param_model(self) -> type[BaseModel] | None:
        """可选：声明参数 Pydantic Model，用于静态校验与 schema 生成。

        默认返回 ``None``，表示不提供参数模型。子类可重写为 property
        返回一个 ``BaseModel`` 子类。
        """
        return None

    @property
    def schema(self) -> dict:
        """自动生成 Function Call 用的 JSON Schema。

        优先使用 ``param_model`` 通过 ``model_json_schema()`` 生成参数 schema；
        若 ``param_model`` 为 ``None`` 或生成失败，则返回空 object 占位 schema。

        返回值为 ``_CallableSchemaDict``（dict 子类），既支持 ``.schema``
        属性访问，也支持 ``.schema()`` 调用访问，兼容不同调用方。

        Returns:
            dict: 形如 ``{"name": ..., "description": ..., "parameters": <jsonschema>}``
            的结构，可直接作为 LLM Function Call 的工具描述。
        """
        params_schema: dict[str, Any]
        model_cls = self.param_model
        if model_cls is None:
            params_schema = {"type": "object", "properties": {}}
        else:
            try:
                params_schema = model_cls.model_json_schema()
            except Exception:
                # 降级：model_json_schema 失败时返回空 object schema，保证可用
                params_schema = {"type": "object", "properties": {}}
        return _CallableSchemaDict(
            name=self.name,
            description=self.description,
            parameters=params_schema,
        )


# ---------------------------------------------------------------------------
# ToolRegistry —— 工具注册表
# ---------------------------------------------------------------------------
class ToolRegistry:
    """工具注册表。

    维护 ``name -> Tool 实例`` 映射。``register`` 接受实例或类（类会在
    注册时实例化），重名抛 ``ValueError``。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool_instance_or_class: Tool | type[Tool]) -> None:
        """注册工具实例或类。

        若传入类，则实例化后存储；若传入实例，则直接存储。重名抛 ``ValueError``。

        Args:
            tool_instance_or_class: Tool 实例或 Tool 子类。

        Raises:
            ValueError: 工具缺少 ``name`` 或 ``name`` 已被注册。
        """
        if inspect.isclass(tool_instance_or_class):
            instance: Tool = tool_instance_or_class()  # type: ignore[abstract]
        else:
            instance = tool_instance_or_class
        name = instance.name
        if not name:
            raise ValueError("工具缺少 name 属性，无法注册")
        if name in self._tools:
            raise ValueError(f"工具名 {name!r} 已注册")
        self._tools[name] = instance

    def get(self, name: str) -> Tool:
        """按名取工具实例。

        Args:
            name: 工具注册名。

        Returns:
            Tool: 对应的工具实例。

        Raises:
            KeyError: 名称为空或未注册。
        """
        if name not in self._tools:
            raise KeyError(name)
        return self._tools[name]

    def has(self, name: str) -> bool:
        """判断工具是否已注册。"""
        return name in self._tools

    def list(self) -> list[str]:
        """返回所有已注册工具名。"""
        return list(self._tools.keys())

    def clear(self) -> None:
        """清空注册表（测试用）。"""
        self._tools.clear()


# ---------------------------------------------------------------------------
# 全局注册表与便捷函数
# ---------------------------------------------------------------------------
_GLOBAL_REGISTRY = ToolRegistry()


def register(tool_instance_or_class: Tool | type[Tool]) -> None:
    """将工具注册到全局注册表。等价于 ``_GLOBAL_REGISTRY.register(...)``。"""
    _GLOBAL_REGISTRY.register(tool_instance_or_class)


def get_tool(name: str) -> Tool:
    """从全局注册表按名取工具实例。不存在抛 ``KeyError``。"""
    return _GLOBAL_REGISTRY.get(name)


def list_tools() -> list[str]:
    """返回全局注册表中所有工具名。"""
    return _GLOBAL_REGISTRY.list()


# ---------------------------------------------------------------------------
# @tool 装饰器
# ---------------------------------------------------------------------------
def tool(name: str | None = None, role: str = "action"):
    """装饰器：将 Tool 子类注册到全局注册表。

    自动设置类属性 ``name`` 与 ``role``，并注册到 ``_GLOBAL_REGISTRY``。
    装饰器返回类本身（不替换类），便于链式使用与类型推断。

    ``name`` 解析优先级：
        1. 装饰器显式传入的 ``name`` 参数；
        2. 类上已设置的非空 ``name`` 属性；
        3. 类名转 snake_case。

    Args:
        name: 注册名。``None`` 时按上述优先级解析默认名。
        role: 语义角色，``source`` | ``action`` | ``sink``，默认 ``action``。

    Returns:
        装饰器函数，接收 Tool 子类并返回该类本身。
    """

    def decorator(cls: type[Tool]) -> type[Tool]:
        # 解析注册名：显式参数 > 类上非空 name > 类名 snake_case
        if name is not None:
            resolved_name = name
        else:
            class_level_name = getattr(cls, "name", "")
            resolved_name = (
                class_level_name
                if class_level_name
                else _to_snake_case(cls.__name__)
            )
        cls.name = resolved_name
        cls.role = role
        _GLOBAL_REGISTRY.register(cls)
        return cls

    return decorator


__all__ = [
    "Tool",
    "tool",
    "ToolRegistry",
    "register",
    "get_tool",
    "list_tools",
]
