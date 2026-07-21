"""llm.base —— LLM 客户端抽象基类与核心数据类。

本模块定义 AgentKit 与大语言模型交互的统一抽象层，是整个 ``llm`` 子包的基石。
所有具体的 LLM 提供商实现（如 ``openai.OpenAIClient``、``mock.MockClient``）
均继承自 ``LLMClient`` 并实现 ``chat`` 方法。

设计原则：
    - 高度模块化：仅依赖 Python 标准库（abc / dataclasses / typing），
      不引入 httpx / pydantic 等第三方依赖，可被任意子模块安全导入。
    - 无循环依赖：本模块不依赖 agentkit 内任何其他子模块。
    - 可拓展：新增 LLM 提供商只需继承 ``LLMClient`` 并实现 ``chat``。
    - 类型注解完整，便于 IDE 静态检查与自动补全。

数据模型说明：
    - ``LLMUsage``:    token 用量统计
    - ``ToolCall``:    一次 Function Call 调用（LLM 请求执行工具）
    - ``LLMResponse``: 一次 LLM 调用的完整响应
    - ``LLMMessage``:  对话消息（system / user / assistant / tool）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMUsage:
    """LLM 调用的 token 用量统计。

    Attributes:
        prompt_tokens:     输入（提示）token 数。
        completion_tokens: 输出（生成）token 数。
        total_tokens:      合计 token 数（通常为前两者之和）。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ToolCall:
    """一次 Function Call 调用（LLM 请求执行工具）。

    由 LLM 在响应中产出，调用方据此执行对应工具，并通过 ``LLMMessage``
    （role=tool）将结果回传给 LLM。

    Attributes:
        id:        调用 id（回传工具结果时需原样附带，便于 LLM 对齐上下文）。
        name:      工具名（与 tools 注册时的 ``function.name`` 一致）。
        arguments: 解析后的参数字典。
    """

    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    """一次 LLM 调用的完整响应。

    一次响应要么是最终文本（``content`` 非空且无 ``tool_calls``），
    要么是工具调用请求（``tool_calls`` 非空，``content`` 可能为空）。

    Attributes:
        content:      文本输出。可能为 None（例如 LLM 仅发起 tool_calls 时）。
        tool_calls:   Function Call 列表。为空表示 LLM 给出最终文本。
        usage:        token 用量统计。
        raw:          原始响应对象（调试用，可能是 dict 或 SDK 对象）。
        finish_reason: 结束原因，常见取值：``stop`` | ``tool_calls`` | ``length``。
    """

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: Any = None
    finish_reason: str = ""

    @property
    def has_tool_calls(self) -> bool:
        """是否包含工具调用请求。"""
        return len(self.tool_calls) > 0


@dataclass
class LLMMessage:
    """对话消息。

    用于构造发送给 LLM 的上下文。四类角色的字段使用约定：
        - system:    仅 ``content``。
        - user:      仅 ``content``。
        - assistant: ``content`` 可选；若发起过 Function Call，则 ``tool_calls`` 非空。
        - tool:      ``content`` 为工具执行结果；``tool_call_id`` 对应被回答的
                     ToolCall.id；``name`` 为该工具名。

    Attributes:
        role:         消息角色。system | user | assistant | tool。
        content:      文本内容（部分角色可能为 None）。
        tool_calls:   assistant 消息携带的 Function Call 列表。
        tool_call_id: role=tool 时，回传对应的 ToolCall.id。
        name:         role=tool 时，对应的工具名。
    """

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class LLMClient(ABC):
    """LLM 客户端抽象基类。

    所有 LLM 提供商客户端必须继承本类并实现 ``chat`` 方法。
    AgentKit 的 ``LLMStep`` 等组件通过本接口与具体提供商解耦，
    便于在 OpenAI / Anthropic / 本地模型 / Mock 之间切换。

    实现方约定：
        - ``chat`` 为协程，支持异步并发调用。
        - ``tools`` 参数遵循 OpenAI Function Call JSON Schema 约定。
        - 返回 ``LLMResponse``，其中 ``raw`` 保留原始响应便于调试。
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> LLMResponse:
        """调用 LLM 完成一次 Chat Completions。

        Args:
            messages:    对话消息列表（system / user / assistant / tool）。
            tools:       Function Call 的 JSON Schema 列表，每个形如
                         ``{"type":"function","function":{"name":...,
                         "description":...,"parameters":...}}``。为 None 或空表示
                         不启用工具调用。
            temperature: 采样温度，越高越随机。默认 0.2。
            model:       指定模型名。为 None 时由具体实现使用其默认模型。

        Returns:
            LLMResponse: LLM 的响应。
        """


__all__ = [
    "LLMUsage",
    "ToolCall",
    "LLMResponse",
    "LLMMessage",
    "LLMClient",
]
