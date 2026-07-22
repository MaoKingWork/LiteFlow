"""llm.thinking —— 思考模式(Thinking)纯函数工具。

本模块将 DeepSeek / MiMo 等"OpenAI 兼容 + 思考模式"模型共有的逻辑抽取为
**无状态纯函数**,供各客户端显式组合调用,而非通过继承复用。

设计原则:
    - **组合优于继承**:不引入 ``ThinkingOpenAIClient`` 中间基类,避免脆弱基类
      问题。各客户端直接继承 ``OpenAIClient``,按需调用本模块函数。
    - **无状态**:所有函数不持有可变状态,入参出参明确,易于测试。
    - **单一职责**:每个函数只做一件事,未来模型差异只需"调不调 / 怎么调"。

共有的思考行为:
    1. ``apply_thinking``:在请求体中注入 thinking / JSON Output / max_completion_tokens,
       并在思考模式下抑制 temperature(DeepSeek / MiMo 共识)。
    2. ``with_reasoning_content``:assistant 消息 dict 追加 reasoning_content 字段,
       用于多轮工具调用的历史回传。
    3. ``attach_reasoning``:从非流式响应中提取 reasoning_content 到 ``LLMResponse.raw``。
    4. ``extract_delta_reasoning``:从流式 delta 中提取 reasoning_content 增量。

公开 API:
    - ThinkingOptions: 思考模式配置选项
    - apply_thinking:   请求体注入思考参数
    - with_reasoning_content: 消息 dict 追加 reasoning_content
    - attach_reasoning: 响应提取 reasoning_content
    - extract_delta_reasoning: 流式 delta 提取 reasoning_content
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentkit.llm.base import LLMMessage, LLMResponse

__all__ = [
    "ThinkingOptions",
    "apply_thinking",
    "with_reasoning_content",
    "attach_reasoning",
    "extract_delta_reasoning",
]


# ---------------------------------------------------------------------------
# ThinkingOptions —— 思考模式配置
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ThinkingOptions:
    """思考模式配置选项。

    封装 DeepSeek / MiMo 等模型共有的思考相关参数。各客户端在构造时传入,
    ``apply_thinking`` 据此修改请求体。

    不包含 ``reasoning_effort``(DeepSeek 专属)或视频参数(MiMo 专属),
    这些由各自的子选项承载,避免跨模型耦合。

    Attributes:
        thinking:             思考模式开关:``"enabled"`` | ``"disabled"`` | ``None``。
                              ``None`` 表示不传该参数(用 API 默认值)。
        json_output:          是否强制 JSON Output(``response_format=json_object``)。
        max_completion_tokens: 最大输出 token 数(含思考链)。``None`` 表示不传。
    """

    thinking: str | None = None
    json_output: bool = False
    max_completion_tokens: int | None = None


# ---------------------------------------------------------------------------
# apply_thinking —— 请求体注入思考参数
# ---------------------------------------------------------------------------
def apply_thinking(body: dict[str, Any], opts: ThinkingOptions) -> dict[str, Any]:
    """在请求体中注入思考模式相关参数。

    修改并返回同一 ``body`` dict(就地修改,便于链式调用)。

    注入逻辑:
        1. ``thinking`` 非 None:写入 ``extra_body.thinking``。
        2. 思考模式开启时(``thinking != "disabled"``):移除 ``temperature``
           (DeepSeek / MiMo 在思考模式下强制 1.0,传入无意义)。
        3. ``json_output``:注入 ``response_format``。
        4. ``max_completion_tokens`` 非 None:注入该字段。

    Args:
        body: 已由 ``OpenAIClient._build_body`` 组装的基础请求体。
        opts: 思考模式配置。

    Returns:
        dict: 修改后的同一 body(就地修改)。
    """
    thinking_enabled = opts.thinking is not None and opts.thinking != "disabled"

    # 思考模式开启时移除 temperature(模型强制默认值,传入无效)
    if thinking_enabled:
        body.pop("temperature", None)

    # extra_body:thinking 参数(非 OpenAI 标准,需置于 extra_body)
    if opts.thinking is not None:
        extra_body = body.setdefault("extra_body", {})
        extra_body["thinking"] = {"type": opts.thinking}

    # JSON Output
    if opts.json_output:
        body["response_format"] = {"type": "json_object"}

    # 最大输出 token(含思考链)
    if opts.max_completion_tokens is not None:
        body["max_completion_tokens"] = opts.max_completion_tokens

    return body


# ---------------------------------------------------------------------------
# with_reasoning_content —— 消息 dict 追加 reasoning_content
# ---------------------------------------------------------------------------
def with_reasoning_content(
    msg_dict: dict[str, Any], msg: LLMMessage
) -> dict[str, Any]:
    """在 assistant 消息 dict 中追加 ``reasoning_content`` 字段。

    DeepSeek / MiMo 要求:多轮工具调用中,若历史 assistant 消息包含工具调用,
    其 ``reasoning_content`` 必须回传给 API,否则返回 400 错误。

    仅当 ``msg.role == "assistant"`` 且 ``msg.reasoning_content`` 非空时追加。

    Args:
        msg_dict: 已由 ``OpenAIClient._message_to_dict`` 生成的基础消息 dict。
        msg:      原始 ``LLMMessage``(含 reasoning_content)。

    Returns:
        dict: 修改后的同一 msg_dict(就地修改)。
    """
    if msg.role == "assistant" and msg.reasoning_content:
        msg_dict["reasoning_content"] = msg.reasoning_content
    return msg_dict


# ---------------------------------------------------------------------------
# attach_reasoning —— 非流式响应提取 reasoning_content
# ---------------------------------------------------------------------------
def attach_reasoning(resp: LLMResponse, data: dict[str, Any]) -> LLMResponse:
    """从非流式响应 dict 中提取 ``reasoning_content`` 到 ``resp.raw``。

    DeepSeek / MiMo 的 ``reasoning_content`` 与 ``content`` 同级,位于
    ``choices[0].message`` 中。本函数将其存入 ``resp.raw``(dict 形态),
    供 ``LLMStep`` 在多轮工具调用时回填到 ``LLMMessage.reasoning_content``。

    Args:
        resp: 已由 ``OpenAIClient._parse_response`` 解析的响应。
        data: 原始响应 dict。

    Returns:
        LLMResponse: 修改后的同一 resp(就地修改 raw)。
    """
    choices = data.get("choices") or []
    message: dict[str, Any] = choices[0]["message"] if choices else {}
    reasoning_content = message.get("reasoning_content")

    raw = resp.raw if isinstance(resp.raw, dict) else {}
    raw["reasoning_content"] = reasoning_content
    resp.raw = raw
    return resp


# ---------------------------------------------------------------------------
# extract_delta_reasoning —— 流式 delta 提取 reasoning_content
# ---------------------------------------------------------------------------
def extract_delta_reasoning(delta: dict[str, Any]) -> str | None:
    """从流式 SSE delta 中提取 ``reasoning_content`` 增量。

    与 ``delta.content`` 同构:返回原始增量片段,不做缓冲或拼接。
    上层消费者(LLMStep 钩子 / 前端)自行累积。

    Args:
        delta: SSE chunk 中的 ``choices[0].delta`` dict。

    Returns:
        str | None: 思考链增量;无则 None。
    """
    rc = delta.get("reasoning_content")
    return rc if rc else None
