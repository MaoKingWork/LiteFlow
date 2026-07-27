"""image —— 图片生成客户端子包。

提供统一的图片生成调用抽象与多提供商支持（与 ``llm`` 子包对称）：
    - base:     ``ImageClient`` 抽象基类，定义 ``generate`` 接口与核心数据类
    - _http:    安全传输层（URL 校验 / DNS 解析 / HTTP 请求 / 流式下载 / 异常映射）
    - provider: 多提供商配置层（``ImageProvider`` / MiniMax+AIMIXHUB+StepFun 预设 / 注册表）
    - openai:   OpenAI 兼容客户端实现（AIMIXHUB / StepFun 等）
    - minimax:  MiniMax 原生 API 客户端实现
    - mock:     测试用 Mock 客户端，不消耗资源不发网络请求

本模块提供两层客户端机制（与 ``llm`` 子包对称）：
    1. **默认客户端**（``set_default_image_client`` / ``get_default_image_client``）：
       直接注册一个 ``ImageClient`` 实例，供 ``ImageStep`` 在未注入时使用。
       适合测试与固定单提供商场景。
    2. **提供商工厂**（``create_image_client`` / ``resolve_image_provider``）：
       按提供商名或配置动态创建客户端，支持多提供商切换。适合 YAML
       声明式配置场景。默认提供商由 config ``default_image_provider`` 控制。

设计原则：
- 高度模块化：仅聚合本子包各模块，无外部循环依赖。
- 与 llm 子包完全对称的 API 设计，学习成本趋近于零。
- 可注入：默认客户端与提供商均可随时替换，便于测试隔离。
"""

from __future__ import annotations

from agentkit.image.base import (
    GeneratedImage,
    ImageClient,
    ImageGenerationError,
    ImageRef,
    ImageRequest,
    ImageResponse,
)
from agentkit.image.mock import MockImageClient
from agentkit.image.openai import OpenAIImageClient
from agentkit.image.minimax import MiniMaxImageClient
from agentkit.image.provider import (
    ImageProvider,
    ImageProviderRegistry,
    create_image_client,
    get_image_provider,
    list_image_providers,
    register_image_client,
    register_image_provider,
    resolve_image_provider,
    PRESET_IMAGE_PROVIDERS,
)

__all__ = [
    # 核心数据类
    "ImageClient",
    "ImageRequest",
    "ImageResponse",
    "GeneratedImage",
    "ImageRef",
    "ImageGenerationError",
    # 客户端实现
    "OpenAIImageClient",
    "MiniMaxImageClient",
    "MockImageClient",
    # 提供商配置层
    "ImageProvider",
    "ImageProviderRegistry",
    "register_image_provider",
    "register_image_client",
    "get_image_provider",
    "list_image_providers",
    "resolve_image_provider",
    "create_image_client",
    "PRESET_IMAGE_PROVIDERS",
    # 默认客户端机制（与 llm 子包对称）
    "set_default_image_client",
    "get_default_image_client",
    "clear_default_image_client",
    # 提供商客户端缓存（按提供商名复用客户端）
    "get_client_for_provider",
    "clear_provider_client_cache",
]


# ---------------------------------------------------------------------------
# 全局默认客户端（与 llm 子包对称）
# ---------------------------------------------------------------------------
# 优先级：显式 set_default_image_client > 提供商工厂（按 default_image_provider 创建）。
_DEFAULT_IMAGE_CLIENT: ImageClient | None = None


def set_default_image_client(client: ImageClient | None) -> None:
    """注册全局默认图片生成客户端。

    供 ``ImageStep`` 等组件在未显式注入客户端时使用。传入 None 等价于清除。

    Args:
        client: 默认客户端实例（或 None 清除）。
    """
    global _DEFAULT_IMAGE_CLIENT
    _DEFAULT_IMAGE_CLIENT = client


def get_default_image_client() -> ImageClient | None:
    """取回全局默认图片生成客户端。

    优先级：
        1. 显式通过 ``set_default_image_client`` 注册的客户端（测试 / 固定场景）。
        2. 未注册时：按 config ``default_image_provider`` 用工厂创建（首次惰性创建）。

    Returns:
        ImageClient | None: 默认客户端。工厂创建失败时返回 None（由调用方处理）。
    """
    global _DEFAULT_IMAGE_CLIENT
    if _DEFAULT_IMAGE_CLIENT is not None:
        return _DEFAULT_IMAGE_CLIENT

    # 惰性创建：按默认提供商创建客户端
    try:
        _DEFAULT_IMAGE_CLIENT = create_image_client(None)
        return _DEFAULT_IMAGE_CLIENT
    except Exception:
        # 创建失败（API Key 未配置等）：返回 None，由 ImageStep 抛明确错误
        return None


def clear_default_image_client() -> None:
    """清除全局默认图片生成客户端（等价于 ``set_default_image_client(None)``）。"""
    global _DEFAULT_IMAGE_CLIENT
    _DEFAULT_IMAGE_CLIENT = None


# ---------------------------------------------------------------------------
# 提供商客户端缓存 —— 按提供商名复用客户端实例（与 llm 子包对称）
# ---------------------------------------------------------------------------
# 当 ImageStep 按 provider 路由到非默认提供商时，用此缓存避免每次 run()
# 都重建客户端。按提供商名键控，同一提供商复用同一客户端。测试可用
# clear_provider_client_cache 清除。
_PROVIDER_CLIENT_CACHE: dict[str, ImageClient] = {}


def get_client_for_provider(name: str) -> ImageClient:
    """按提供商名获取（惰性创建并缓存的）图片生成客户端。

    首次调用时通过 ``create_image_client(name)`` 创建并缓存；后续同名调用
    直接返回缓存实例，避免重复创建。

    Args:
        name: 提供商名（预设或自定义注册名）。

    Returns:
        ImageClient: 对应提供商的客户端实例。

    Raises:
        KeyError / ValueError: 提供商未注册（由 ``create_image_client`` 传播）。
    """
    if name not in _PROVIDER_CLIENT_CACHE:
        _PROVIDER_CLIENT_CACHE[name] = create_image_client(name)
    return _PROVIDER_CLIENT_CACHE[name]


def clear_provider_client_cache() -> None:
    """清除提供商客户端缓存（测试用）。"""
    _PROVIDER_CLIENT_CACHE.clear()
