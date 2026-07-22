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
    - ``ChatChunk``:   流式输出的单个片段
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMUsage:
    """LLM 调用的 token 用量统计。

    基础三字段兼容所有 OpenAI 兼容 API;明细字段覆盖 MiMo / DeepSeek 等
    提供的 ``prompt_tokens_details`` 与 ``completion_tokens_details``,
    用于多模态计费分析与成本优化。未返回的明细字段默认 0。

    Attributes:
        prompt_tokens:      输入（提示）token 数。
        completion_tokens:  输出（生成）token 数。
        total_tokens:       合计 token 数（通常为前两者之和）。
        cached_tokens:      命中缓存的输入 token 数（prompt_tokens_details）。
        reasoning_tokens:   思考链消耗的输出 token 数（completion_tokens_details）。
        image_tokens:       图片输入消耗的 token 数。
        audio_tokens:       音频输入消耗的 token 数。
        video_tokens:       视频输入消耗的 token 数。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    image_tokens: int = 0
    audio_tokens: int = 0
    video_tokens: int = 0


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
class ChatChunk:
    """流式输出的单个片段。

    流式调用（``LLMClient.chat_stream``）按 SSE 顺序 yield 本类实例。

    字段语义：
        - ``delta_content``: 文本增量。实时推送，调用方累加得到完整文本。
          中间片段仅有此字段。
        - ``delta_reasoning_content``: 思考链增量。与 ``delta_content`` 同构,
          由 ``OpenAIClient`` 从 ``delta.reasoning_content`` 透传。框架不负责
          累积;上层消费者(LLMStep 钩子 / 前端)自行拼接。
        - ``tool_calls``: **完整**工具调用列表。客户端在流式过程中按 ``index``
          累积分片（拼接 ``arguments`` JSON 字符串），**仅在流末尾 chunk** 携带。
          调用方无需做分片合并。
        - ``finish_reason`` / ``usage``: 通常仅出现在流末尾 chunk。

    设计说明：
        - ``delta_reasoning_content`` 与 ``delta_content`` 处理方式完全对称:
          框架只透传 API 原始增量,不做缓冲 / 拼接 / Markdown 修复。上层是否
          消费由钩子决定(``on_llm_stream_delta`` 的 ``delta_reasoning`` 参数)。
        - ``tool_calls`` 选择"客户端累积、末尾一次交付"而非"分片实时推送"，
          因为调用方（LLMStep）只在流结束后判定"是否工具轮"，无需实时观察
          tool_calls 拼接过程。这样简化了所有调用方。

    Attributes:
        delta_content:           文本增量。None 表示本片段无文本。
        delta_reasoning_content: 思考链增量。None 表示本片段无思考链。
        tool_calls:              完整工具调用列表（仅末尾 chunk）。None 表示无工具调用。
        finish_reason:           流结束原因（仅末尾 chunk）。
        usage:                   token 用量（通常仅末尾 chunk）。
        raw:                     原始 SSE chunk（调试用）。
    """

    delta_content: str | None = None
    delta_reasoning_content: str | None = None
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None
    usage: LLMUsage | None = None
    raw: Any = None


@dataclass
class LLMMessage:
    """对话消息。

    用于构造发送给 LLM 的上下文。四类角色的字段使用约定：
        - system:    仅 ``content``。
        - user:      ``content`` 为 ``str`` 或 ``list[dict]``(多模态)。
                     多模态时每个 dict 是 OpenAI content part,如
                     ``{"type":"image_url","image_url":{"url":...}}``,
                     与 OpenAI SDK / LangChain / LiteLLM 原生格式一致。
        - assistant: ``content`` 可选;若发起过 Function Call,则 ``tool_calls``
                     非空。``reasoning_content`` 携带思考链(DeepSeek / MiMo),
                     多轮工具调用时必须回传给 API。
        - tool:      ``content`` 为工具执行结果;``tool_call_id`` 对应被回答的
                     ToolCall.id;``name`` 为该工具名。

    Attributes:
        role:              消息角色。system | user | assistant | tool。
        content:           文本内容或多模态 content part 列表。部分角色可能为 None。
        tool_calls:        assistant 消息携带的 Function Call 列表。
        tool_call_id:      role=tool 时,回传对应的 ToolCall.id。
        name:              role=tool 时,对应的工具名。
        reasoning_content: assistant 消息的思考链内容。多轮工具调用时需回传。
    """

    role: str
    content: str | list[dict] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    reasoning_content: str | None = None


class LLMClient(ABC):
    """LLM 客户端抽象基类。

    所有 LLM 提供商客户端必须继承本类并实现 ``chat`` 方法。
    AgentKit 的 ``LLMStep`` 等组件通过本接口与具体提供商解耦，
    便于在 OpenAI / Anthropic / 本地模型 / Mock 之间切换。

    实现方约定：
        - ``chat`` 为协程，支持异步并发调用。
        - ``tools`` 参数遵循 OpenAI Function Call JSON Schema 约定。
        - 返回 ``LLMResponse``，其中 ``raw`` 保留原始响应便于调试。
        - ``chat_stream`` 有默认实现（退化为 ``chat`` 一次性返回），
          子类按需覆盖以提供真流式。``MockClient`` 等简单实现可零成本
          满足接口契约。
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
        """调用 LLM 完成一次 Chat Completions（非流式）。

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

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """调用 LLM 流式输出（SSE）。

        默认实现：退化为 ``chat`` 一次性获取，包装成单个 ``ChatChunk`` yield。
        子类覆盖此方法以提供真流式（如 ``OpenAIClient`` 解析 SSE 增量推送）。

        默认实现保证：未覆盖 ``chat_stream`` 的子类（如 ``MockClient``）
        仍满足接口契约，``LLMStep`` 可无差别调用 ``chat_stream`` 而不必关心
        客户端是否真支持流式。

        Args:
            messages:    对话消息列表。
            tools:       Function Call 工具 schema；None 表示不启用工具。
            temperature: 采样温度。
            model:       模型名覆盖。

        Yields:
            ChatChunk: 流式片段。默认实现仅 yield 一个含完整响应的 chunk。
        """
        resp = await self.chat(
            messages,
            tools=tools,
            temperature=temperature,
            model=model,
        )
        yield ChatChunk(
            delta_content=resp.content,
            tool_calls=resp.tool_calls or None,
            finish_reason=resp.finish_reason or None,
            usage=resp.usage,
            raw=resp.raw,
        )


__all__ = [
    "LLMUsage",
    "ToolCall",
    "LLMResponse",
    "LLMMessage",
    "ChatChunk",
    "LLMClient",
]
