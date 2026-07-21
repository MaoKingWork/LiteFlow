"""llm.openai —— 基于 httpx 的 OpenAI Chat Completions 客户端实现。

本模块提供 ``OpenAIClient``，通过 ``httpx.AsyncClient`` 异步调用 OpenAI
Chat Completions API（``POST {base_url}/chat/completions``），并兼容 OpenAI
规格的第三方网关（如 Azure OpenAI、DeepSeek、本地 vLLM 等，只需调整 ``base_url``）。

设计原则：
    - 高度模块化：仅依赖 ``httpx`` 与本包 ``llm.base``，无循环依赖。
    - 可注入：构造函数接受外部 ``httpx.AsyncClient``，便于测试与连接复用。
    - 延迟校验：构造时不要求 ``api_key`` 必须存在，仅在真正发起 ``chat`` 时校验，
      方便在配置未就绪的环境下先行实例化。
    - 可拓展：新增 LLM 提供商可参考本实现继承 ``LLMClient``。
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from agentkit.config import get_default
from agentkit.llm.base import (
    LLMClient,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    ToolCall,
)


class OpenAIClient(LLMClient):
    """OpenAI Chat Completions 异步客户端。

    通过 ``httpx.AsyncClient`` 调用 OpenAI 兼容的 Chat Completions 接口，
    支持 Function Call（``tools`` 参数）与标准文本对话。

    Args:
        api_key: OpenAI API Key。为 None 时读取 ``OPENAI_API_KEY`` 环境变量；
                 若仍为 None，构造不报错，延迟到 ``chat`` 时抛 ``RuntimeError``。
        base_url: API 根地址。默认 ``https://api.openai.com/v1``，可指向兼容网关。
        timeout: 请求超时秒数。为 None 时读取 config ``llm_request_timeout_seconds``。
        client:  可选的已构造 ``httpx.AsyncClient``，用于测试或连接复用。
                 若传入则不由本实例负责关闭；若未传入则内部自建并在 ``close`` 时关闭。
        model:   默认模型名。``chat`` 未显式传 ``model`` 时使用。

    用法示例::

        client = OpenAIClient(api_key="sk-...")
        resp = await client.chat(
            [LLMMessage(role="user", content="你好")],
            model="gpt-4o-mini",
        )
        print(resp.content)
        await client.close()
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
        model: str | None = None,
    ) -> None:
        # api_key: 显式传入优先；否则回落到环境变量；可能仍为 None（延迟到 chat 报错）
        self.api_key: str | None = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self.base_url: str = base_url
        # timeout: 显式传入优先；否则读取 config 默认值（llm_request_timeout_seconds -> 120.0）
        self.timeout: float = timeout if timeout is not None else float(
            get_default("llm_request_timeout_seconds")
        )
        self.model: str | None = model

        # client 注入与所有权标记：外部传入不负责关闭，自建则由 close() 关闭
        if client is not None:
            self._client: httpx.AsyncClient = client
            self._owns_client: bool = False
        else:
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True

    # ------------------------------------------------------------------
    # 消息格式转换
    # ------------------------------------------------------------------
    @staticmethod
    def _message_to_dict(msg: LLMMessage) -> dict[str, Any]:
        """把 ``LLMMessage`` 转为 OpenAI Chat Completions 消息 dict。

        转换规则：
            - system / user: ``{"role", "content"}``
            - assistant:     ``content`` 可选；若携带 ``tool_calls``，转为 OpenAI
                             tool_calls 格式（``function.arguments`` 序列化为 JSON 字符串）
            - tool:          ``{"role":"tool", "content", "tool_call_id", "name"}``
        """
        if msg.role in ("system", "user"):
            return {"role": msg.role, "content": msg.content}

        if msg.role == "assistant":
            d: dict[str, Any] = {"role": "assistant"}
            # content 可能为 None（仅发起 tool_calls 时），OpenAI 接受 null
            d["content"] = msg.content
            if msg.tool_calls:
                d["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            # OpenAI 要求 arguments 为 JSON 字符串
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            return d

        if msg.role == "tool":
            d = {
                "role": "tool",
                "content": msg.content,
                "tool_call_id": msg.tool_call_id,
            }
            # name 在 OpenAI 协议中为可选字段，存在时附带
            if msg.name is not None:
                d["name"] = msg.name
            return d

        # 兜底：未知 role 按 system/user 风格透传 content
        return {"role": msg.role, "content": msg.content}

    # ------------------------------------------------------------------
    # LLMClient 接口实现
    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> LLMResponse:
        """调用 OpenAI Chat Completions。

        详见 ``LLMClient.chat`` 的接口契约。本实现将 ``LLMMessage`` 转为 OpenAI
        格式，发送 POST 请求并解析响应为 ``LLMResponse``。
        """
        # 延迟校验 api_key：允许构造时未配置，但真正调用时必须有
        if not self.api_key:
            raise RuntimeError(
                "OpenAIClient 缺少 api_key：未显式传入且环境变量 OPENAI_API_KEY 未设置。"
            )

        # 组装请求体
        body: dict[str, Any] = {
            "model": model or self.model or "gpt-4o-mini",
            "messages": [self._message_to_dict(m) for m in messages],
            "temperature": temperature,
        }
        # tools 非空才加入（OpenAI 不接受空列表语义）
        if tools:
            body["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url.rstrip('/')}/chat/completions"

        # 发送请求；网络层错误（连接失败、超时等）原样上抛
        resp = await self._client.post(url, json=body, headers=headers)

        # HTTP 非 2xx 抛 RuntimeError 并附带响应体，便于排查
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(
                f"OpenAI Chat Completions 请求失败: HTTP {resp.status_code}, "
                f"body={resp.text}"
            )

        data = resp.json()
        return self._parse_response(data)

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> LLMResponse:
        """解析 OpenAI 响应 dict 为 ``LLMResponse``。

        - ``choices[0].message.content`` -> content
        - ``choices[0].message.tool_calls`` -> ToolCall 列表（arguments JSON 解析失败用 {}）
        - ``usage`` -> LLMUsage
        - ``choices[0].finish_reason`` -> finish_reason
        """
        choices = data.get("choices") or []
        message: dict[str, Any] = choices[0]["message"] if choices else {}
        content: str | None = message.get("content")

        # 解析 tool_calls
        tool_calls: list[ToolCall] = []
        for raw_tc in message.get("tool_calls") or []:
            func = raw_tc.get("function") or {}
            raw_args = func.get("arguments", "{}")
            # arguments 是 JSON 字符串；解析失败降级为空 dict，避免整次响应失败
            try:
                arguments: dict = json.loads(raw_args) if isinstance(raw_args, str) else (
                    raw_args or {}
                )
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=raw_tc.get("id", ""),
                    name=func.get("name", ""),
                    arguments=arguments,
                )
            )

        # 解析 usage
        raw_usage = data.get("usage") or {}
        usage = LLMUsage(
            prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
            completion_tokens=int(raw_usage.get("completion_tokens", 0)),
            total_tokens=int(raw_usage.get("total_tokens", 0)),
        )

        finish_reason = ""
        if choices:
            finish_reason = choices[0].get("finish_reason", "") or ""

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            raw=data,
            finish_reason=finish_reason,
        )

    async def close(self) -> None:
        """关闭内部自建的 ``httpx.AsyncClient``。

        若客户端为外部注入（``client`` 参数传入），则不执行关闭，由调用方自行管理。
        """
        if self._owns_client:
            await self._client.aclose()


__all__ = ["OpenAIClient"]
