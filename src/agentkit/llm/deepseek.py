"""llm.deepseek —— DeepSeek 深度适配客户端。

本模块实现 ``DeepSeekClient``,继承 ``OpenAIClient`` 并叠加 DeepSeek 特有优化:

1. **思考模式(thinking)**:通过 ``extra_body={"thinking": {"type": "enabled"}}``
   开启思维链推理,大幅提升复杂推理任务质量。
2. **思考强度(reasoning_effort)**:``"high"`` / ``"max"`` 控制推理深度。
3. **JSON Output**:设置 ``response_format={'type': 'json_object'}`` 确保输出
   合法 JSON,配合 ``output_model`` 契约链实现结构化输出零失败。
4. **reasoning_content 处理**:解析思维链内容并保存到 ``LLMResponse.raw``,
   在多轮工具调用中自动拼接到上下文(DeepSeek 要求工具调用轮次的
   reasoning_content 必须回传)。
5. **思考模式参数抑制**:思考模式下 temperature / top_p 等不生效,自动移除
   避免无意义传输。

设计原则:
    - 继承复用:不重写 HTTP 层,仅在请求体组装与响应解析处覆盖。
    - 配置驱动:所有优化通过 ``DeepSeekOptions`` 控制,可按需开关。
    - 向下兼容:关闭 thinking 后行为与 ``OpenAIClient`` 完全一致。
    - 类型注解完整,中文 docstring。

公开 API:
    - DeepSeekClient: DeepSeek 深度适配客户端
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agentkit.llm.base import LLMMessage, LLMResponse, LLMUsage, ToolCall
from agentkit.llm.openai import OpenAIClient
from agentkit.llm.provider import DeepSeekOptions

__all__ = ["DeepSeekClient"]


class DeepSeekClient(OpenAIClient):
    """DeepSeek 深度适配客户端。

    继承 ``OpenAIClient``,在请求体组装时注入 DeepSeek 特有参数(thinking /
    reasoning_effort / response_format),在响应解析时提取 reasoning_content。

    Args:
        api_key:  DeepSeek API Key。为 None 时读 ``DEEPSEEK_API_KEY`` 环境变量。
        base_url: API 根地址,默认 ``https://api.deepseek.com``。
        timeout:  请求超时秒数。
        client:   可选的已构造 ``httpx.AsyncClient``。
        model:    默认模型名,默认 ``deepseek-v4-pro``。
        options:  DeepSeek 深度优化选项;``None`` 时用默认(思考模式开启)。

    用法示例::

        from agentkit.llm.provider import create_client

        # 用预设创建
        client = create_client("deepseek")
        resp = await client.chat([LLMMessage(role="user", content="你好")])

    YAML 配置示例::

        providers:
          - name: deepseek
            # 用内置预设,只需配 api_key
            api_key: ${DEEPSEEK_API_KEY}
            options:
              thinking: enabled
              reasoning_effort: high
              json_output: true
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
    # 请求体组装(覆盖父类,注入 DeepSeek 特有参数)
    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> LLMResponse:
        """调用 DeepSeek Chat Completions,注入 thinking / JSON Output 等参数。

        与 ``OpenAIClient.chat`` 的区别:
            - 注入 ``thinking`` / ``reasoning_effort``(通过 extra_body)。
            - ``options.json_output`` 为 True 时注入 ``response_format``。
            - thinking=enabled 时移除 temperature(思考模式不生效)。
            - 解析响应中的 ``reasoning_content``。
        """
        if not self.api_key:
            raise RuntimeError(
                "DeepSeekClient 缺少 api_key:未显式传入且环境变量 "
                "DEEPSEEK_API_KEY 未设置。"
            )

        # 组装请求体
        body: dict[str, Any] = {
            "model": model or self.model or "deepseek-v4-pro",
            "messages": [self._message_to_dict(m) for m in messages],
        }

        # thinking 模式参数注入
        thinking_enabled = (
            self.options.thinking is not None
            and self.options.thinking != "disabled"
        )

        # 思考模式不生效的参数:仅在非思考模式时传 temperature
        if not thinking_enabled:
            body["temperature"] = temperature

        # tools 非空才加入
        if tools:
            body["tools"] = tools

        # JSON Output:注入 response_format
        if self.options.json_output:
            body["response_format"] = {"type": "json_object"}

        # extra_body:thinking + reasoning_effort
        extra_body: dict[str, Any] = {}
        if self.options.thinking is not None:
            extra_body["thinking"] = {"type": self.options.thinking}
        if self.options.reasoning_effort is not None and thinking_enabled:
            extra_body["reasoning_effort"] = self.options.reasoning_effort
        if extra_body:
            body["extra_body"] = extra_body

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        resp = await self._client.post(url, json=body, headers=headers)

        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(
                f"DeepSeek Chat Completions 请求失败: HTTP {resp.status_code}, "
                f"body={resp.text}"
            )

        data = resp.json()
        return self._parse_response(data)

    # ------------------------------------------------------------------
    # 消息格式转换(覆盖父类,处理 reasoning_content 多轮拼接)
    # ------------------------------------------------------------------
    @staticmethod
    def _message_to_dict(msg: LLMMessage) -> dict[str, Any]:
        """把 ``LLMMessage`` 转为 DeepSeek 消息 dict。

        与 ``OpenAIClient._message_to_dict`` 的区别:
            - assistant 消息携带 ``reasoning_content`` 时回传给 API
              (DeepSeek 要求工具调用轮次的 reasoning_content 必须回传)。
        """
        d = OpenAIClient._message_to_dict(msg)

        # assistant 消息:回传 reasoning_content(存在时)
        if msg.role == "assistant" and hasattr(msg, "reasoning_content"):
            rc = getattr(msg, "reasoning_content", None)
            if rc:
                d["reasoning_content"] = rc

        return d

    # ------------------------------------------------------------------
    # 响应解析(覆盖父类,提取 reasoning_content)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_response(data: dict[str, Any]) -> LLMResponse:
        """解析 DeepSeek 响应 dict 为 ``LLMResponse``。

        与 ``OpenAIClient._parse_response`` 的区别:
            - 提取 ``reasoning_content`` 保存到 ``raw`` 中。
            - 其余字段(content / tool_calls / usage / finish_reason)复用父类逻辑。
        """
        # 先用父类解析标准字段
        resp = OpenAIClient._parse_response(data)

        # 提取 reasoning_content(DeepSeek 特有,与 content 同级)
        choices = data.get("choices") or []
        message: dict[str, Any] = choices[0]["message"] if choices else {}
        reasoning_content = message.get("reasoning_content")

        # 把 reasoning_content 存到 raw 中,供多轮拼接使用
        raw = resp.raw if isinstance(resp.raw, dict) else {}
        raw["reasoning_content"] = reasoning_content
        resp.raw = raw

        return resp
