"""llm.mimo —— MiMo 深度适配客户端(薄壳)。

本模块实现 ``MiMoClient``,继承 ``OpenAIClient``,通过**组合调用**
``thinking.py`` 纯函数叠加思考模式支持。MiMo 的多模态能力(图片 / 音频 /
视频)使用 OpenAI content part 原生 dict 格式,由 ``OpenAIClient`` 直接透传,
无需特殊序列化逻辑。

MiMo 与 DeepSeek 的思考机制完全同构(thinking / reasoning_content /
temperature 抑制),因此共享 ``thinking.py`` 函数。两者差异仅在于:
    - MiMo 无 ``reasoning_effort`` 参数。
    - MiMo 默认模型为 ``mimo-v2.5-pro``。
    - MiMo 的 API Key 环境变量为 ``MIMO_API_KEY``。
    - MiMo ``mimo-v2.5`` 支持多模态(图片 / 音频 / 视频)。

多模态使用方式(与 OpenAI 原生格式一致)::

    messages = [LLMMessage(
        role="user",
        content=[
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {"url": "https://..."}},
        ],
    )]

    # 视频(可带 fps 与 media_resolution)
    content = [
        {"type": "text", "text": "分析视频内容"},
        {"type": "video_url", "video_url": {"url": "..."}, "fps": 2, "media_resolution": "default"},
    ]

公开 API:
    - MiMoClient: MiMo 深度适配客户端
"""

from __future__ import annotations

import os
from typing import Any

from agentkit.llm.base import LLMMessage, LLMResponse
from agentkit.llm.openai import OpenAIClient
from agentkit.llm.thinking import (
    ThinkingOptions,
    apply_thinking,
    attach_reasoning,
    with_reasoning_content,
)

__all__ = ["MiMoClient"]


class MiMoClient(OpenAIClient):
    """MiMo 深度适配客户端(薄壳)。

    继承 ``OpenAIClient``,组合调用 ``thinking.py`` 函数实现思考模式。
    多模态 content part 由用户以原生 dict 构造,``OpenAIClient`` 直接透传。

    Args:
        api_key:  MiMo API Key。为 None 时读 ``MIMO_API_KEY`` 环境变量。
        base_url: API 根地址,默认 ``https://api.xiaomimimo.com/v1``。
        timeout:  请求超时秒数。
        client:   可选的已构造 ``httpx.AsyncClient``。
        model:    默认模型名,默认 ``mimo-v2.5-pro``。
        options:  思考模式选项;``None`` 时用默认(思考模式开启)。

    用法示例::

        from agentkit.llm.mimo import MiMoClient

        client = MiMoClient(api_key="...")
        resp = await client.chat([
            LLMMessage(role="user", content=[
                {"type": "text", "text": "描述图片"},
                {"type": "image_url", "image_url": {"url": "https://..."}},
            ]),
        ])

    YAML 配置示例::

        providers:
          - name: mimo
            api_key: ${MIMO_API_KEY}
            options:
              thinking: enabled
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.xiaomimimo.com/v1",
        timeout: float | None = None,
        client: Any = None,
        model: str = "mimo-v2.5-pro",
        options: ThinkingOptions | None = None,
    ) -> None:
        # api_key: 显式传入优先;否则读 MIMO_API_KEY
        api_key = api_key if api_key is not None else os.getenv("MIMO_API_KEY")
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            client=client,
            model=model,
        )
        self.options: ThinkingOptions = options or ThinkingOptions(thinking="enabled")

    # ------------------------------------------------------------------
    # 请求体组装(薄壳:父类 + apply_thinking)
    # ------------------------------------------------------------------
    def _build_body(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None,
        temperature: float,
        model: str | None,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """覆盖:父类组装基础 body → ``apply_thinking`` 注入思考参数。

        MiMo 与 DeepSeek 共享思考逻辑(temperature 抑制 / thinking 注入 /
        JSON Output / max_completion_tokens),由 ``apply_thinking`` 统一处理。
        MiMo 无 ``reasoning_effort``,故不追加该字段。
        """
        body = super()._build_body(messages, tools, temperature, model, stream=stream)
        apply_thinking(body, self.options)
        return body

    async def _ensure_api_key(self) -> None:
        """覆盖:MiMo 专用错误信息。"""
        if not self.api_key:
            raise RuntimeError(
                "MiMoClient 缺少 api_key:未显式传入且环境变量 "
                "MIMO_API_KEY 未设置。"
            )

    # ------------------------------------------------------------------
    # 消息格式转换(薄壳:父类 + with_reasoning_content)
    # ------------------------------------------------------------------
    @staticmethod
    def _message_to_dict(msg: LLMMessage) -> dict[str, Any]:
        """覆盖:父类转换 → ``with_reasoning_content`` 追加思考链回传。

        MiMo 与 DeepSeek 一样要求多轮工具调用中回传 ``reasoning_content``。
        """
        d = OpenAIClient._message_to_dict(msg)
        with_reasoning_content(d, msg)
        return d

    # ------------------------------------------------------------------
    # 响应解析(薄壳:父类 + attach_reasoning)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_response(data: dict[str, Any]) -> LLMResponse:
        """覆盖:父类解析标准字段 → ``attach_reasoning`` 提取 reasoning_content。"""
        resp = OpenAIClient._parse_response(data)
        attach_reasoning(resp, data)
        return resp
