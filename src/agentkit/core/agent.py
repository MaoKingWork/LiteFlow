"""core.agent —— AgentConfig 配置对象与 Agent 注册机制。

本模块定义 AgentKit 中的智能体配置对象 ``AgentConfig``，以及配套的
``@agent`` 装饰器与 ``AgentRegistry`` 注册表。

设计要点：
    - ``AgentConfig`` 是「提示词 + 模型配置 + 输出契约 + 工具集 + Skill
      引用 + Function Call 策略 + 重试降级链 + 业务校验器」的不可变配置
      对象，作为被任意 Step 引用的纯数据载体。
    - 不可变理念：``resolve_defaults`` 通过 ``dataclasses.replace`` 返回
      填充默认值后的新实例，不修改原对象。
    - 可拓展：子类通过 ``@agent`` 装饰器注册到全局 ``AgentRegistry``，
      ``instantiate_agent`` 按名构造实例并回填注册名。
    - 模块仅依赖 ``agentkit.config`` 与 ``pydantic``，以及
      ``TYPE_CHECKING`` 下的 ``Context``，不依赖其他 agentkit 子模块，
      避免循环依赖。

公开 API：
    - ExhaustedPolicy:    重试耗尽策略常量
    - AgentConfig:        Agent 配置基类
    - agent:              装饰器，注册 AgentConfig 子类
    - AgentRegistry:      Agent 注册表
    - register_agent:     注册到全局注册表
    - get_agent:          按名取 Agent 类
    - instantiate_agent:  按名构造 Agent 实例
    - list_agents:        列出所有已注册 Agent 名
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel

from agentkit.config import RetryPolicy, default_retry_policy, get_default

if TYPE_CHECKING:
    from agentkit.core.context import Context


# ---------------------------------------------------------------------------
# 辅助：CamelCase / PascalCase 转 snake_case
# ---------------------------------------------------------------------------
def _to_snake_case(name: str) -> str:
    """将类名转换为 snake_case。

    Args:
        name: 原始类名，例如 ``DataCompressor`` / ``HTTPClient``。

    Returns:
        str: snake_case 形式，例如 ``data_compressor`` / ``http_client``。
    """
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    return s2.lower()


# ---------------------------------------------------------------------------
# ExhaustedPolicy —— 重试耗尽后的处理策略常量
# ---------------------------------------------------------------------------
class ExhaustedPolicy:
    """重试链全部耗尽后的处理策略。

    以类常量形式暴露，便于类型提示与可读性。取值为字符串，对应
    ``AgentConfig.on_exhausted`` 字段。

    Attributes:
        RAISE:   抛出异常（默认）。
        DEFAULT: 返回 ``default_value``。
        SKIP:    跳过当前 Step，返回 ``None``。
    """

    RAISE = "raise"
    DEFAULT = "default"
    SKIP = "skip"


# ---------------------------------------------------------------------------
# AgentConfig —— Agent 配置对象
# ---------------------------------------------------------------------------
@dataclass
class AgentConfig:
    """Agent 配置对象。

    封装智能体运行所需的全部静态配置：提示词、模型、输出契约、工具集、
    Skill 引用、Function Call 策略、重试降级链与业务校验器。作为不可变
    配置对象被任意 Step 引用；子类用 ``@agent`` 装饰器注册。

    不可变说明：本 dataclass 未加 ``frozen=True``，以便 ``resolve_defaults``
    与子类 ``__post_init__`` 能填充字段；但语义上调用方不应在运行期修改
    实例字段，需要变更时应通过 ``dataclasses.replace`` 产生新实例。

    Attributes:
        name:                注册名，由 ``@agent`` 装饰器设置。
        model:               LLM 模型名。
        system:              系统提示词。
        output_model:        输出契约 Pydantic Model，``None`` 表示自由文本。
        temperature:         采样温度。
        tools:               工具名引用列表（指向 ToolRegistry 中的工具）。
        skills:              Skill 名引用列表（指向 SkillRegistry 中的 Skill）。
        max_tool_iterations: Function Call 最大轮次；``0`` 表示用 config 默认值，
                             在 ``resolve_defaults`` 时填充。
        retry:               重试策略。
        fallback_model:      降级模型名，``None`` 表示不降级。
        on_exhausted:        重试耗尽后策略：``raise`` | ``default`` | ``skip``。
        default_value:       ``on_exhausted='default'`` 时返回的默认值。
    """

    name: str = ""
    model: str = "gpt-4o-mini"
    system: str = ""
    output_model: type[BaseModel] | None = None
    temperature: float = 0.2
    tools: list[str] = field(default_factory=list)
    # 依赖的 MCP server 名列表;MCPManager.connect_all 后据此把对应 server
    # 发现的工具名自动注入到 tools,使 MCP 能力对 LLM Function Call 可见。
    mcp: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    max_tool_iterations: int = 0
    retry: RetryPolicy = field(default_factory=default_retry_policy)
    fallback_model: str | None = None
    on_exhausted: str = "raise"
    default_value: Any = None

    def output_validator(self, result: Any, ctx: "Context") -> bool:
        """业务校验器：子类重写。

        在 LLM 输出解析后调用，返回 ``False`` 触发重试链。默认总返回 ``True``。

        Args:
            result: LLM 输出解析后的结果（结构由 ``output_model`` 决定）。
            ctx:    会话上下文（只读视图）。

        Returns:
            bool: 校验是否通过。
        """
        return True

    def resolve_defaults(self) -> "AgentConfig":
        """返回填充了默认值的新 AgentConfig。

        当 ``max_tool_iterations`` 为 ``0`` 时，用
        ``get_default("default_max_tool_iterations")`` 填充。不修改原对象
        （不可变理念），通过 ``dataclasses.replace`` 产生新实例。

        Returns:
            AgentConfig: 填充默认值后的新实例。
        """
        if self.max_tool_iterations != 0:
            # 已显式设置，无需填充，返回等价副本
            return dataclasses.replace(self)
        return dataclasses.replace(
            self,
            max_tool_iterations=int(get_default("default_max_tool_iterations")),
        )


# ---------------------------------------------------------------------------
# AgentRegistry —— Agent 注册表
# ---------------------------------------------------------------------------
class AgentRegistry:
    """Agent 注册表。

    维护 ``name -> AgentConfig 子类`` 映射。注册的是**类**而非实例，
    由调用方按需实例化，或通过 ``instantiate`` 一步完成。
    ``instantiate`` 会在构造实例后回填注册名到 ``inst.name``，确保实例
    携带注册名（dataclass 子类的 ``__init__`` 默认值不会自动反映装饰器
    设置的类级 ``name``，因此需显式回填）。
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[AgentConfig]] = {}

    def register(self, agent_class: type[AgentConfig]) -> None:
        """注册 AgentConfig 子类。

        Args:
            agent_class: AgentConfig 子类，须已设置非空 ``name`` 类属性。

        Raises:
            ValueError: 类缺少 ``name`` 或 ``name`` 已被注册。
        """
        name = getattr(agent_class, "name", "")
        if not name:
            raise ValueError("AgentConfig 子类缺少 name 属性，无法注册")
        if name in self._classes:
            raise ValueError(f"Agent 名 {name!r} 已注册")
        self._classes[name] = agent_class

    def get(self, name: str) -> type[AgentConfig]:
        """按名取 Agent 类。

        Args:
            name: Agent 注册名。

        Returns:
            type[AgentConfig]: 对应的 AgentConfig 子类。

        Raises:
            KeyError: 名称为空或未注册。
        """
        if name not in self._classes:
            raise KeyError(name)
        return self._classes[name]

    def has(self, name: str) -> bool:
        """判断 Agent 是否已注册。"""
        return name in self._classes

    def list(self) -> list[str]:
        """返回所有已注册 Agent 名。"""
        return list(self._classes.keys())

    def instantiate(self, name: str) -> AgentConfig:
        """按名构造 Agent 实例。

        取出注册类并实例化，随后将注册名回填到 ``inst.name``，确保实例
        携带注册名（dataclass 默认值不会自动反映装饰器设置的类级 name）。

        Args:
            name: Agent 注册名。

        Returns:
            AgentConfig: 携带注册名的实例。

        Raises:
            KeyError: 名称为空或未注册。
        """
        cls = self.get(name)
        inst = cls()
        inst.name = name
        return inst

    def clear(self) -> None:
        """清空注册表（测试用）。"""
        self._classes.clear()


# ---------------------------------------------------------------------------
# 全局注册表与便捷函数
# ---------------------------------------------------------------------------
_GLOBAL_AGENT_REGISTRY = AgentRegistry()


def register_agent(agent_class: type[AgentConfig]) -> None:
    """将 AgentConfig 子类注册到全局注册表。"""
    _GLOBAL_AGENT_REGISTRY.register(agent_class)


def get_agent(name: str) -> type[AgentConfig]:
    """从全局注册表按名取 Agent 类。不存在抛 ``KeyError``。"""
    return _GLOBAL_AGENT_REGISTRY.get(name)


def instantiate_agent(name: str) -> AgentConfig:
    """从全局注册表按名构造 Agent 实例（回填注册名）。"""
    return _GLOBAL_AGENT_REGISTRY.instantiate(name)


def list_agents() -> list[str]:
    """返回全局注册表中所有 Agent 名。"""
    return _GLOBAL_AGENT_REGISTRY.list()


# ---------------------------------------------------------------------------
# @agent 装饰器
# ---------------------------------------------------------------------------
def agent(name: str | None = None):
    """装饰器：将 AgentConfig 子类注册到全局注册表。

    自动设置类属性 ``name``（默认类名转 snake_case），并注册到
    ``_GLOBAL_AGENT_REGISTRY``。装饰器返回类本身，不替换类、不自动加
    ``@dataclass``——子类需自行标注 ``@dataclass``（按 spec 简单做法）。

    注意：装饰器在类上设置类级 ``name``，但 dataclass 子类的 ``__init__``
    仍以字段默认值为准；实例化时通过 ``instantiate_agent`` 回填注册名
    以确保实例携带正确名称。

    Args:
        name: 注册名。``None`` 时用类名转 snake_case。

    Returns:
        装饰器函数，接收 AgentConfig 子类并返回该类本身。
    """

    def decorator(cls: type[AgentConfig]) -> type[AgentConfig]:
        resolved_name = name if name is not None else _to_snake_case(cls.__name__)
        cls.name = resolved_name
        _GLOBAL_AGENT_REGISTRY.register(cls)
        return cls

    return decorator


__all__ = [
    "ExhaustedPolicy",
    "AgentConfig",
    "agent",
    "AgentRegistry",
    "register_agent",
    "get_agent",
    "instantiate_agent",
    "list_agents",
]
