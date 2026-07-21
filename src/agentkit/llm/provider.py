"""llm.provider —— LLM 提供商配置层与预设注册表。

本模块是 AgentKit 多 LLM 提供商支持的核心。通过 ``LLMProvider`` 数据类
统一描述一个提供商(base_url / api_key / 默认模型 / 特性开关),DeepSeek
作为内置预设注册,其他 OpenAI 兼容提供商通过 ``CustomProvider`` 配置。

设计要点:
    - ``PRESET_PROVIDERS``:内置预设注册表,DeepSeek 为首个预设。
    - ``LLMProvider``:不可变配置对象(frozen dataclass),描述一个提供商。
    - ``ProviderRegistry``:全局注册表,支持运行时动态注册自定义提供商。
    - ``resolve_provider``:按名解析提供商(预设优先,自定义次之)。
    - ``create_client``:根据提供商创建对应 LLMClient 实例。
    - DeepSeek 特有特性(thinking 模式 / JSON Output / reasoning_effort)
      由 ``DeepSeekOptions`` 承载,在创建 ``DeepSeekClient`` 时传入。

兼容性:
    - 所有提供商必须兼容 OpenAI Chat Completions API 规范。
    - 自定义提供商仅需提供 base_url + api_key + model,即可复用 ``OpenAIClient``。
    - DeepSeek 通过 ``DeepSeekClient`` 继承 ``OpenAIClient``,叠加深度优化。

公开 API:
    - LLMProvider:        提供商配置数据类
    - DeepSeekOptions:    DeepSeek 深度优化选项
    - ProviderRegistry:   提供商注册表
    - register_provider:  注册自定义提供商
    - get_provider:       按名获取提供商
    - list_providers:     列出所有提供商名
    - resolve_provider:   解析提供商(名 → 配置)
    - create_client:      根据提供商创建客户端
    - PRESET_PROVIDERS:   内置预设 dict
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentkit.llm.base import LLMClient

__all__ = [
    "LLMProvider",
    "DeepSeekOptions",
    "ProviderRegistry",
    "register_provider",
    "get_provider",
    "list_providers",
    "resolve_provider",
    "create_client",
    "PRESET_PROVIDERS",
]


# ---------------------------------------------------------------------------
# DeepSeekOptions —— DeepSeek 深度优化选项
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DeepSeekOptions:
    """DeepSeek 深度优化选项。

    封装 DeepSeek API 特有参数,在创建 ``DeepSeekClient`` 时传入。

    Attributes:
        thinking:        思考模式开关:``"enabled"`` | ``"disabled"`` | ``None``。
                         ``None`` 表示不传该参数(用 API 默认值)。
        reasoning_effort: 思考强度:``"high"`` | ``"max"`` | ``None``。
                         仅在 thinking=enabled 时生效。``None`` 表示不传。
        json_output:     是否强制 JSON Output(response_format=json_object)。
                         为 True 时每次请求自动注入 response_format。
    """

    thinking: str | None = None
    reasoning_effort: str | None = None
    json_output: bool = False


# ---------------------------------------------------------------------------
# LLMProvider —— 提供商配置
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LLMProvider:
    """LLM 提供商配置。

    描述一个 LLM 提供商的连接信息与默认参数。所有提供商必须兼容 OpenAI
    Chat Completions API 规范(``POST {base_url}/chat/completions``)。

    Attributes:
        name:        提供商标识(如 ``"deepseek"`` / ``"deepseek-flash"`` / 自定义名)。
        base_url:    API 根地址(如 ``"https://api.deepseek.com"``)。
        api_key:     API Key;``None`` 时从环境变量读取(见 ``api_key_env``)。
        api_key_env: API Key 环境变量名;``api_key`` 为 None 时从此读取。
        model:       默认模型名(如 ``"deepseek-v4-pro"``)。
        provider_type: 提供商类型:``"deepseek"`` | ``"openai"``。
                      ``"deepseek"`` 创建 ``DeepSeekClient``(深度优化);
                      ``"openai"`` 创建 ``OpenAIClient``(通用兼容)。
        options:     提供商特有选项(如 ``DeepSeekOptions``);通用提供商为 None。
    """

    name: str
    base_url: str
    api_key: str | None = None
    api_key_env: str = ""
    model: str = ""
    provider_type: str = "openai"
    options: Any = None  # DeepSeekOptions | None

    def resolve_api_key(self) -> str | None:
        """解析生效的 API Key。

        优先用显式 ``api_key``;为 None 时从 ``api_key_env`` 环境变量读取。

        Returns:
            str | None: API Key,或 None(未配置)。
        """
        if self.api_key is not None:
            return self.api_key
        if self.api_key_env:
            return os.getenv(self.api_key_env)
        return None


# ---------------------------------------------------------------------------
# 内置预设提供商
# ---------------------------------------------------------------------------
# DeepSeek 两个模型作为内置预设:
#   - "deepseek":      Pro 模型,默认开启思考模式(reasoning_effort=high),
#                       适合复杂推理任务。
#   - "deepseek-flash": Flash 模型,默认非思考模式(更快更便宜),
#                       适合简单任务与高并发场景;可按需开启思考。
# 两者共享同一 base_url 与 api_key,仅 model 与默认 options 不同。
# 其他 OpenAI 兼容提供商通过 YAML providers 段或 register_provider 注册,
# 需提供 base_url + api_key + model。
PRESET_PROVIDERS: dict[str, LLMProvider] = {
    "deepseek": LLMProvider(
        name="deepseek",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        model="deepseek-v4-pro",
        provider_type="deepseek",
        options=DeepSeekOptions(
            thinking="enabled",
            reasoning_effort="high",
            json_output=False,
        ),
    ),
    "deepseek-flash": LLMProvider(
        name="deepseek-flash",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        model="deepseek-v4-flash",
        provider_type="deepseek",
        options=DeepSeekOptions(
            thinking="disabled",
            reasoning_effort=None,
            json_output=False,
        ),
    ),
}


# ---------------------------------------------------------------------------
# ProviderRegistry —— 提供商注册表
# ---------------------------------------------------------------------------
class ProviderRegistry:
    """LLM 提供商注册表。

    维护 ``name -> LLMProvider`` 映射。初始化时加载内置预设,支持运行时
    动态注册自定义提供商。

    用法::

        from agentkit.llm.provider import register_provider, LLMProvider

        register_provider(LLMProvider(
            name="my_local",
            base_url="http://localhost:8000/v1",
            api_key="sk-local",
            model="llama-3-70b",
        ))
    """

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = dict(PRESET_PROVIDERS)

    def register(self, provider: LLMProvider) -> None:
        """注册或覆盖一个提供商。

        Args:
            provider: 提供商配置。``name`` 必须非空。
        """
        if not provider.name:
            raise ValueError("LLMProvider.name 不能为空")
        self._providers[provider.name] = provider

    def get(self, name: str) -> LLMProvider:
        """按名获取提供商。

        Args:
            name: 提供商名(预设名或自定义名)。

        Returns:
            LLMProvider: 提供商配置。

        Raises:
            KeyError: 提供商未注册。
        """
        if name not in self._providers:
            available = sorted(self._providers.keys())
            raise KeyError(
                f"未注册的 LLM 提供商: {name!r}。可用: {available}"
            )
        return self._providers[name]

    def has(self, name: str) -> bool:
        """判断提供商是否已注册。"""
        return name in self._providers

    def list(self) -> list[str]:
        """返回所有已注册提供商名。"""
        return list(self._providers.keys())

    def clear(self) -> None:
        """清空自定义注册,恢复内置预设(测试用)。"""
        self._providers = dict(PRESET_PROVIDERS)


# ---------------------------------------------------------------------------
# 全局注册表与便捷函数
# ---------------------------------------------------------------------------
_GLOBAL_PROVIDER_REGISTRY = ProviderRegistry()


def register_provider(provider: LLMProvider) -> None:
    """注册自定义提供商到全局注册表。"""
    _GLOBAL_PROVIDER_REGISTRY.register(provider)


def get_provider(name: str) -> LLMProvider:
    """从全局注册表按名获取提供商。"""
    return _GLOBAL_PROVIDER_REGISTRY.get(name)


def list_providers() -> list[str]:
    """返回所有已注册提供商名。"""
    return _GLOBAL_PROVIDER_REGISTRY.list()


def resolve_provider(
    name: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> LLMProvider:
    """解析提供商配置。

    优先级:
        1. ``name`` 非空:从注册表取预设,再用显式参数覆盖。
        2. ``name`` 为空但 ``base_url`` 非空:构造临时自定义提供商。
        3. 均为空:返回全局默认提供商(读 config ``default_llm_provider``)。

    Args:
        name:     提供商名(预设或自定义注册名)。
        base_url: 覆盖 base_url。
        api_key:  覆盖 api_key。
        model:    覆盖默认模型。

    Returns:
        LLMProvider: 解析后的提供商配置。
    """
    import dataclasses as _dc

    # 1. 按名取预设
    if name:
        provider = _GLOBAL_PROVIDER_REGISTRY.get(name)
        # 用显式参数覆盖
        overrides: dict[str, Any] = {}
        if base_url is not None:
            overrides["base_url"] = base_url
        if api_key is not None:
            overrides["api_key"] = api_key
        if model is not None:
            overrides["model"] = model
        if overrides:
            provider = _dc.replace(provider, **overrides)
        return provider

    # 2. 有 base_url 但无 name:构造临时提供商
    if base_url:
        return LLMProvider(
            name="custom",
            base_url=base_url,
            api_key=api_key,
            model=model or "",
            provider_type="openai",
        )

    # 3. 均为空:用全局默认提供商名
    from agentkit.config import get_default

    default_name = str(get_default("default_llm_provider"))
    return _GLOBAL_PROVIDER_REGISTRY.get(default_name)


def create_client(provider: LLMProvider | str | None = None) -> "LLMClient":
    """根据提供商配置创建 LLM 客户端。

    Args:
        provider: ``LLMProvider`` 实例 / 提供商名 / None。
                  None 时用全局默认提供商。

    Returns:
        LLMClient: 对应的客户端实例(DeepSeek → DeepSeekClient,
                   其他 → OpenAIClient)。

    Raises:
        ValueError: provider_type 未知。
    """
    # 解析为 LLMProvider
    if provider is None:
        provider = resolve_provider(None)
    elif isinstance(provider, str):
        provider = resolve_provider(provider)

    # 按 provider_type 创建客户端
    if provider.provider_type == "deepseek":
        from agentkit.llm.deepseek import DeepSeekClient

        return DeepSeekClient(
            api_key=provider.resolve_api_key(),
            base_url=provider.base_url,
            model=provider.model,
            options=provider.options or DeepSeekOptions(),
        )
    else:
        # 通用 OpenAI 兼容客户端
        from agentkit.llm.openai import OpenAIClient

        return OpenAIClient(
            api_key=provider.resolve_api_key(),
            base_url=provider.base_url,
            model=provider.model,
        )
