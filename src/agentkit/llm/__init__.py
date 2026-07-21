"""llm —— LLM 客户端子包。

提供统一的 LLM 调用抽象与多提供商支持:
    - base:      LLMClient 抽象基类,定义 chat 接口与核心数据类
    - provider:  多提供商配置层(LLMProvider / DeepSeek 预设 / 注册表)
    - openai:    OpenAI 兼容客户端实现(基于 httpx,通用兼容)
    - deepseek:  DeepSeek 深度适配(thinking 模式 / JSON Output / reasoning_effort)
    - mock:      测试用 Mock 客户端,不消耗 token 不发网络请求

本模块提供两层客户端机制:
    1. **默认客户端**(set_default_client / get_default_client):
       直接注册一个 LLMClient 实例,供 LLMStep 在未注入时使用。适合测试
       与固定单提供商场景。
    2. **提供商工厂**(create_client / resolve_provider):
       按提供商名或配置动态创建客户端,支持多提供商切换。适合 YAML
       声明式配置场景。默认提供商由 config ``default_llm_provider`` 控制。

设计原则:
- 高度模块化:仅聚合本子包各模块,无外部循环依赖。
- 可注入:默认客户端与提供商均可随时替换,便于测试隔离。
- DeepSeek 优先:默认提供商为 deepseek,内置深度优化预设。
- 类型注解完整,中文 docstring。
"""

from __future__ import annotations

from agentkit.llm.base import (
    LLMClient,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    ToolCall,
)
from agentkit.llm.mock import MockClient
from agentkit.llm.openai import OpenAIClient
from agentkit.llm.deepseek import DeepSeekClient
from agentkit.llm.provider import (
    LLMProvider,
    DeepSeekOptions,
    register_provider,
    get_provider,
    list_providers,
    resolve_provider,
    create_client,
    PRESET_PROVIDERS,
)


__all__ = [
    # 核心数据类
    "LLMClient",
    "LLMMessage",
    "LLMResponse",
    "LLMUsage",
    "ToolCall",
    # 客户端实现
    "OpenAIClient",
    "DeepSeekClient",
    "MockClient",
    # 提供商配置层
    "LLMProvider",
    "DeepSeekOptions",
    "register_provider",
    "get_provider",
    "list_providers",
    "resolve_provider",
    "create_client",
    "PRESET_PROVIDERS",
    # 默认客户端机制(向后兼容)
    "set_default_client",
    "get_default_client",
    "clear_default_client",
]


# 全局默认客户端(未注册时为 None)。单进程内共享;测试可用 clear_default_client 清除。
# 优先级:显式 set_default_client > 提供商工厂(按 default_llm_provider 创建)。
_DEFAULT_CLIENT: LLMClient | None = None


def set_default_client(client: LLMClient | None) -> None:
    """注册全局默认 LLM 客户端。

    供 LLMStep 等组件在未显式注入客户端时使用。传入 None 等价于清除。

    Args:
        client: 默认客户端实例(或 None 清除)。
    """
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = client


def get_default_client() -> LLMClient | None:
    """取回全局默认 LLM 客户端。

    优先级:
        1. 显式通过 set_default_client 注册的客户端(测试 / 固定场景)。
        2. 未注册时:按 config ``default_llm_provider`` 用工厂创建(首次惰性创建)。

    Returns:
        LLMClient | None: 默认客户端。工厂创建失败时返回 None(由调用方处理)。
    """
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is not None:
        return _DEFAULT_CLIENT

    # 惰性创建:按默认提供商创建客户端
    try:
        _DEFAULT_CLIENT = create_client(None)
        return _DEFAULT_CLIENT
    except Exception:
        # 创建失败(API Key 未配置等):返回 None,由 LLMStep 抛明确错误
        return None


def clear_default_client() -> None:
    """清除全局默认客户端(等价于 set_default_client(None))。"""
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = None
