"""image.provider —— 图片生成提供商配置层与预设注册表。

本模块是 AgentKit 多图片生成提供商支持的核心。通过 ``ImageProvider`` 数据类
统一描述一个提供商(base_url / api_key / 默认模型 / provider_type)，MiniMax
与 AIMIXHUB / StepFun 作为内置预设注册，其他 OpenAI 兼容提供商通过
``register_image_provider`` 配置。

设计要点（与 ``llm/provider.py`` 完全对称）：
    - ``PRESET_IMAGE_PROVIDERS``: 内置预设注册表（minimax / aihubmix / stepfun）。
    - ``ImageProvider``: 不可变配置对象(frozen dataclass)，描述一个提供商。
      ``__post_init__`` 校验 base_url 安全性 + api_key_env 合法性 +
      provider_type 已注册。
    - ``ImageProviderRegistry``: 全局注册表，支持运行时动态注册自定义提供商。
    - ``resolve_image_provider``: 按名解析提供商；无 name 时用全局默认。
    - ``create_image_client``: 根据提供商创建对应 ``ImageClient`` 实例。
    - v3 改进：用 ``_CLIENT_REGISTRY`` dict 替代 if/else 硬编码路由，
      新增服务商只需 ``register_image_client(type, cls)`` 一行注册。

安全改进（v2/v3）：
    - ``base_url`` 经 ``validate_url`` 校验（scheme + 非私网 + 非元数据接口）
    - ``api_key_env`` 经 ``validate_api_key_env`` 正则校验（防密钥泄露）
    - ``provider_type`` 经 ``_get_registered_provider_types`` 校验（防拼写错误）

公开 API:
    - ImageProvider:              提供商配置数据类
    - ImageProviderRegistry:      提供商注册表
    - register_image_provider:    注册自定义提供商
    - register_image_client:      注册 provider_type → ImageClient 映射
    - get_image_provider:         按名获取提供商
    - list_image_providers:       列出所有提供商名
    - resolve_image_provider:     解析提供商(名 → 配置)
    - create_image_client:        根据提供商创建客户端
    - PRESET_IMAGE_PROVIDERS:     内置预设 dict
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentkit.image.base import ImageClient

__all__ = [
    "ImageProvider",
    "ImageProviderRegistry",
    "register_image_provider",
    "register_image_client",
    "get_image_provider",
    "list_image_providers",
    "resolve_image_provider",
    "create_image_client",
    "PRESET_IMAGE_PROVIDERS",
]


# ---------------------------------------------------------------------------
# 客户端注册表（dict 替代 if/else 硬编码，v3 改进）
# ---------------------------------------------------------------------------
# provider_type → ImageClient 子类的映射。新增服务商只需调用
# register_image_client(type, cls) 一行注册，无需修改工厂函数。
_CLIENT_REGISTRY: dict[str, type["ImageClient"]] = {}


def register_image_client(
    provider_type: str,
    client_cls: type["ImageClient"],
) -> None:
    """注册 ``provider_type`` → ``ImageClient`` 子类的映射。

    扩展新服务商时调用此函数即可，无需修改 ``create_image_client``。

    Args:
        provider_type: 提供商类型标识（如 ``"openai"`` / ``"minimax"``）。
        client_cls:    ``ImageClient`` 子类。构造函数需接受
                       ``api_key`` / ``base_url`` / ``model`` 关键字参数。

    Raises:
        ValueError: ``provider_type`` 已注册（防止意外覆盖）。
    """
    if provider_type in _CLIENT_REGISTRY:
        raise ValueError(
            f"provider_type={provider_type!r} 已注册为 "
            f"{_CLIENT_REGISTRY[provider_type].__name__}"
        )
    _CLIENT_REGISTRY[provider_type] = client_cls


def _get_registered_provider_types() -> set[str]:
    """返回当前已注册的所有 provider_type（供 ImageProvider 校验）。"""
    return set(_CLIENT_REGISTRY.keys())


# ---------------------------------------------------------------------------
# ImageProvider —— 提供商配置（含安全校验）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ImageProvider:
    """图片生成提供商配置。

    描述一个图片生成提供商的连接信息与默认参数。

    ``__post_init__`` 安全校验：
        - ``base_url`` 经 ``validate_url`` 校验（scheme + 非私网 + 非元数据接口）
        - ``api_key_env`` 经 ``validate_api_key_env`` 校验（防密钥名注入）
        - ``provider_type`` 必须为已注册类型（防拼写错误被静默兜底）

    Attributes:
        name:          提供商标识（如 ``"minimax"`` / ``"aihubmix"`` / ``"stepfun"``）。
        base_url:      API 根地址（经 SSRF 校验）。
        api_key:       API Key；None 时从环境变量读取。
        api_key_env:   API Key 环境变量名（经名称合法性校验）。
        model:         默认模型名。
        provider_type: 提供商类型：``"openai"`` | ``"minimax"``（需为已注册类型）。
    """

    name: str
    base_url: str
    api_key: str | None = None
    api_key_env: str = ""
    model: str = ""
    provider_type: str = "openai"

    def __post_init__(self) -> None:
        """frozen dataclass 的校验入口。

        在构造时校验配置合法性，确保不安全的配置在注册时即报错，
        而非延迟到运行时以晦涩的错误暴露。
        """
        if not self.name:
            raise ValueError("ImageProvider.name 不能为空")
        # 校验 provider_type 必须是已注册的类型
        # 防止拼写错误（如 "openia"）被静默当 OpenAI 处理
        valid_types = _get_registered_provider_types()
        if self.provider_type not in valid_types:
            raise ValueError(
                f"ImageProvider.provider_type={self.provider_type!r} 不合法，"
                f"当前已注册类型: {valid_types}"
            )
        # 校验 base_url：预设的公网 API 不应指向私网
        # resolve_dns=False：配置期不做 DNS 解析，避免网络请求；
        # 请求期由 _http.post_json / _SSRFSafeTransport 做完整 DNS 校验
        from agentkit.image._http import validate_url

        validate_url(self.base_url, allow_private=False, resolve_dns=False)
        # 校验 api_key_env 名称合法性（非空时）
        if self.api_key_env:
            from agentkit.image._http import validate_api_key_env

            validate_api_key_env(self.api_key_env)

    def resolve_api_key(self) -> str | None:
        """解析生效的 API Key。

        优先用显式 ``api_key``；为 None 时从 ``api_key_env`` 环境变量读取。
        环境变量名已在 ``__post_init__`` 中校验合法性，此处直接读取。

        Returns:
            str | None: API Key，或 None（未配置）。
        """
        if self.api_key is not None:
            return self.api_key
        if self.api_key_env:
            return os.getenv(self.api_key_env)
        return None


# ---------------------------------------------------------------------------
# 内置客户端注册（在定义预设提供商之前完成，供 __post_init__ 校验）
# ---------------------------------------------------------------------------
from agentkit.image.minimax import MiniMaxImageClient  # noqa: E402
from agentkit.image.openai import OpenAIImageClient  # noqa: E402

register_image_client("openai", OpenAIImageClient)
register_image_client("minimax", MiniMaxImageClient)


# ---------------------------------------------------------------------------
# 内置预设提供商
# ---------------------------------------------------------------------------
# 三个内置预设，覆盖主要服务商：
#   - "minimax":   MiniMax 原生 API（provider_type="minimax"）
#   - "aihubmix":  AIMIXHUB OpenAI 兼容 API（provider_type="openai"）
#   - "stepfun":   StepFun OpenAI 兼容 API（provider_type="openai"）
PRESET_IMAGE_PROVIDERS: dict[str, ImageProvider] = {
    "minimax": ImageProvider(
        name="minimax",
        base_url="https://api.minimaxi.com",
        api_key_env="MINIMAX_API_KEY",
        model="image-01",
        provider_type="minimax",
    ),
    "aihubmix": ImageProvider(
        name="aihubmix",
        base_url="https://aihubmix.com/v1",
        api_key_env="AIHUBMIX_API_KEY",
        model="gpt-image-1",
        provider_type="openai",
    ),
    "stepfun": ImageProvider(
        name="stepfun",
        base_url="https://api.stepfun.com/v1",
        api_key_env="STEPFUN_API_KEY",
        model="step-1x-medium",
        provider_type="openai",
    ),
}


# ---------------------------------------------------------------------------
# ImageProviderRegistry —— 提供商注册表
# ---------------------------------------------------------------------------
class ImageProviderRegistry:
    """图片生成提供商注册表。

    维护 ``name -> ImageProvider`` 映射。初始化时加载内置预设，支持运行时
    动态注册自定义提供商。

    用法::

        from agentkit.image.provider import (
            register_image_provider, ImageProvider,
        )

        register_image_provider(ImageProvider(
            name="my_custom",
            base_url="https://api.my-custom-provider.com/v1",
            api_key_env="MY_CUSTOM_API_KEY",
            model="my-image-model",
            provider_type="openai",
        ))
    """

    def __init__(self) -> None:
        self._providers: dict[str, ImageProvider] = dict(PRESET_IMAGE_PROVIDERS)

    def register(self, provider: ImageProvider) -> None:
        """注册或覆盖一个提供商。

        Args:
            provider: 提供商配置。``name`` 必须非空。
        """
        if not provider.name:
            raise ValueError("ImageProvider.name 不能为空")
        self._providers[provider.name] = provider

    def get(self, name: str) -> ImageProvider:
        """按名获取提供商。

        Args:
            name: 提供商名（预设名或自定义名）。

        Returns:
            ImageProvider: 提供商配置。

        Raises:
            KeyError: 提供商未注册。
        """
        if name not in self._providers:
            available = sorted(self._providers.keys())
            raise KeyError(
                f"未注册的图片生成提供商: {name!r}。可用: {available}"
            )
        return self._providers[name]

    def has(self, name: str) -> bool:
        """判断提供商是否已注册。"""
        return name in self._providers

    def list(self) -> list[str]:
        """返回所有已注册提供商名。"""
        return list(self._providers.keys())

    def clear(self) -> None:
        """清空自定义注册，恢复内置预设（测试用）。"""
        self._providers = dict(PRESET_IMAGE_PROVIDERS)


# ---------------------------------------------------------------------------
# 全局注册表与便捷函数
# ---------------------------------------------------------------------------
_GLOBAL_PROVIDER_REGISTRY = ImageProviderRegistry()


def register_image_provider(provider: ImageProvider) -> None:
    """注册自定义图片生成提供商到全局注册表。"""
    _GLOBAL_PROVIDER_REGISTRY.register(provider)


def get_image_provider(name: str) -> ImageProvider:
    """从全局注册表按名获取图片生成提供商。"""
    return _GLOBAL_PROVIDER_REGISTRY.get(name)


def list_image_providers() -> list[str]:
    """返回所有已注册图片生成提供商名。"""
    return _GLOBAL_PROVIDER_REGISTRY.list()


def resolve_image_provider(
    name: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> ImageProvider:
    """解析图片生成提供商配置。

    优先级：
        1. ``name`` 非空：从注册表取预设，再用显式参数覆盖。
        2. ``name`` 为空但 ``base_url`` 非空：构造临时自定义提供商。
        3. 均为空：返回全局默认提供商（读 config ``default_image_provider``）。

    Args:
        name:     提供商名（预设或自定义注册名）。
        base_url: 覆盖 base_url。
        api_key:  覆盖 api_key。
        model:    覆盖默认模型。

    Returns:
        ImageProvider: 解析后的提供商配置。
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

    # 2. 有 base_url 但无 name：构造临时提供商
    if base_url:
        return ImageProvider(
            name="custom",
            base_url=base_url,
            api_key=api_key,
            model=model or "",
            provider_type="openai",
        )

    # 3. 均为空：用全局默认提供商名
    from agentkit.config import get_default

    default_name = str(get_default("default_image_provider"))
    return _GLOBAL_PROVIDER_REGISTRY.get(default_name)


def create_image_client(provider: "ImageProvider | str | None" = None) -> "ImageClient":
    """根据提供商配置创建图片生成客户端。

    通过 ``_CLIENT_REGISTRY`` 字典查找对应的客户端类。未知 ``provider_type``
    显式抛出 ``ValueError``，不再静默兜底为 OpenAI。

    Args:
        provider: ``ImageProvider`` 实例 / 提供商名 / None。
                  None 时用全局默认提供商。

    Returns:
        ImageClient: 对应的客户端实例。

    Raises:
        ValueError: ``provider_type`` 未注册。
    """
    # 解析为 ImageProvider
    if provider is None:
        provider = resolve_image_provider(None)
    elif isinstance(provider, str):
        provider = resolve_image_provider(provider)

    client_cls = _CLIENT_REGISTRY.get(provider.provider_type)
    if client_cls is None:
        # 显式报错，不静默兜底
        raise ValueError(
            f"未注册的 provider_type={provider.provider_type!r}，"
            f"已注册类型: {list(_CLIENT_REGISTRY.keys())}"
        )
    return client_cls(
        api_key=provider.resolve_api_key(),
        base_url=provider.base_url,
        model=provider.model,
    )
