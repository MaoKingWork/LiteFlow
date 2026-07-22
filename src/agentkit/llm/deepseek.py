"""llm.deepseek —— DeepSeek 深度适配客户端(薄壳)。

本模块实现 ``DeepSeekClient``,继承 ``OpenAIClient``,通过**组合调用**
``thinking.py`` 纯函数叠加 DeepSeek 特有优化,而非在继承链中间插入基类。

DeepSeek 特有逻辑(与 MiMo 共有的部分由 ``thinking.py`` 承载):
    1. ``reasoning_effort``:思考强度(``"high"`` / ``"max"``),DeepSeek 专属。
    2. 其余(thinking 注入 / temperature 抑制 / JSON Output /
       reasoning_content 回传 / 响应提取)均由 ``thinking.py`` 函数处理。

设计原则:
    - **组合优于继承**:不引入中间基类,直接继承 ``OpenAIClient`` + 调用纯函数。
    - **薄壳**:``_build_body`` / ``_message_to_dict`` / ``_parse_response``
      仅调用父类 + thinking 函数,无重复逻辑。
    - 未来新增"OpenAI 兼容 + 思考"模型(如 MiMo)同理继承 ``OpenAIClient``
      并组合调用 ``thinking.py``,无需触碰本类。

公开 API:
    - DeepSeekClient: DeepSeek 深度适配客户端
"""

from __future__ import annotations

from typing import Any

from agentkit.llm.base import LLMMessage, LLMResponse
from agentkit.llm.openai import OpenAIClient
from agentkit.llm.provider import DeepSeekOptions
from agentkit.llm.thinking import (
    apply_thinking,
    attach_reasoning,
    with_reasoning_content,
)

__all__ = ["DeepSeekClient"]


class DeepSeekClient(OpenAIClient):
    """DeepSeek 深度适配客户端(薄壳)。

    继承 ``OpenAIClient``,在请求体组装时调用 ``apply_thinking`` 注入思考参数,
    在消息转换时调用 ``with_reasoning_content`` 回传思考链,
    在响应解析时调用 ``attach_reasoning`` 提取思考链。

    DeepSeek 专属参数(``reasoning_effort``)由 ``DeepSeekOptions`` 承载,
    在 ``_build_body`` 中注入。

    Args:
        api_key:  DeepSeek API Key。为 None 时读 ``DEEPSEEK_API_KEY`` 环境变量。
        base_url: API 根地址,默认 ``https://api.deepseek.com``。
        timeout:  请求超时秒数。
        client:   可选的已构造 ``httpx.AsyncClient``。
        model:    默认模型名,默认 ``deepseek-v4-pro``。
        options:  DeepSeek 深度优化选项;``None`` 时用默认(思考模式开启)。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        timeout: float | None = None,
        client: Any = None,
        model: str = "deepseek-v4-pro",
        options: DeepSeekOptions | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            client=client,
            model=model,
        )
        self.options: DeepSeekOptions = options or DeepSeekOptions()

    # ------------------------------------------------------------------
    # 请求体组装(薄壳:父类 + apply_thinking + reasoning_effort)
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
        """覆盖:父类组装基础 body → ``apply_thinking`` 注入思考参数 → 追加 reasoning_effort。"""
        body = super()._build_body(messages, tools, temperature, model, stream=stream)

        # 组合调用:注入 thinking / JSON Output / max_completion_tokens,抑制 temperature
        apply_thinking(body, self.options)

        # DeepSeek 专属:reasoning_effort(仅在思考模式开启时注入)
        thinking_enabled = (
            self.options.thinking is not None
            and self.options.thinking != "disabled"
        )
        if self.options.reasoning_effort and thinking_enabled:
            body.setdefault("extra_body", {})["reasoning_effort"] = (
                self.options.reasoning_effort
            )

        return body

    async def _ensure_api_key(self) -> None:
        """覆盖:DeepSeek 专用错误信息。"""
        if not self.api_key:
            raise RuntimeError(
                "DeepSeekClient 缺少 api_key:未显式传入且环境变量 "
                "DEEPSEEK_API_KEY 未设置。"
            )

    # ------------------------------------------------------------------
    # 消息格式转换(薄壳:父类 + with_reasoning_content)
    # ------------------------------------------------------------------
    @staticmethod
    def _message_to_dict(msg: LLMMessage) -> dict[str, Any]:
        """覆盖:父类转换 → ``with_reasoning_content`` 追加思考链回传。"""
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
