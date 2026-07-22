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
from collections.abc import AsyncIterator
from typing import Any

import httpx

from agentkit.config import get_default
from agentkit.llm.base import (
    ChatChunk,
    LLMClient,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    ToolCall,
)


def _normalize_content(content: str | list[dict] | None) -> str | list[dict] | None:
    """校验多模态 content part 列表,仅检查 ``type`` 键存在性后原样透传。

    与 OpenAI SDK / LangChain / LiteLLM 原生格式兼容:用户直接传 dict 列表,
    无需转换为自定义数据类。``str`` 与 ``None`` 直接返回。

    Args:
        content: ``str`` / ``list[dict]`` / ``None``。

    Returns:
        原样透传的 content。

    Raises:
        TypeError:  content 为 list 但元素非 dict。
        ValueError: content part dict 缺少 ``type`` 键。
    """
    if not isinstance(content, list):
        return content
    for i, part in enumerate(content):
        if not isinstance(part, dict):
            raise TypeError(
                f"content[{i}] 必须为 dict,实际为 {type(part).__name__}"
            )
        if "type" not in part:
            raise ValueError(f"content[{i}] 缺少必需的 'type' 键: {part}")
    return content


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
              ``content`` 为 ``list[dict]`` 时(多模态),逐项校验 ``type`` 键存在后透传,
              与 OpenAI SDK / LangChain / LiteLLM 原生格式一致。
            - assistant:     ``content`` 可选；若携带 ``tool_calls``，转为 OpenAI
                             tool_calls 格式（``function.arguments`` 序列化为 JSON 字符串）
            - tool:          ``{"role":"tool", "content", "tool_call_id", "name"}``
        """
        if msg.role in ("system", "user"):
            return {"role": msg.role, "content": _normalize_content(msg.content)}

        if msg.role == "assistant":
            d: dict[str, Any] = {"role": "assistant"}
            # content 可能为 None（仅发起 tool_calls 时），OpenAI 接受 null
            d["content"] = _normalize_content(msg.content)
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
                "content": _normalize_content(msg.content),
                "tool_call_id": msg.tool_call_id,
            }
            # name 在 OpenAI 协议中为可选字段，存在时附带
            if msg.name is not None:
                d["name"] = msg.name
            return d

        # 兜底：未知 role 按 system/user 风格透传 content
        return {"role": msg.role, "content": _normalize_content(msg.content)}

    # ------------------------------------------------------------------
    # 请求组装(子类可覆盖 _build_body 注入特有参数)
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        """请求头。子类可覆盖以追加特有头(如 DeepSeek 无额外头)。"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_body(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None,
        temperature: float,
        model: str | None,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """组装 Chat Completions 请求体。

        子类（如 ``DeepSeekClient``）覆盖此方法注入特有参数（thinking /
        response_format 等），``chat`` / ``chat_stream`` 共用此方法。

        Args:
            stream: 是否流式。为 True 时注入 ``stream=True``。
        """
        body: dict[str, Any] = {
            "model": model or self.model or "gpt-4o-mini",
            "messages": [self._message_to_dict(m) for m in messages],
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
            # 流式 + usage 末尾返回(OpenAI 2.5+ 支持,DeepSeek 也支持)
            body["stream_options"] = {"include_usage": True}
        return body

    def _url(self) -> str:
        """Chat Completions 端点 URL。"""
        return f"{self.base_url.rstrip('/')}/chat/completions"

    async def _ensure_api_key(self) -> None:
        """延迟校验 api_key。"""
        if not self.api_key:
            raise RuntimeError(
                "OpenAIClient 缺少 api_key：未显式传入且环境变量 OPENAI_API_KEY 未设置。"
            )

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
        """调用 OpenAI Chat Completions（非流式）。

        详见 ``LLMClient.chat`` 的接口契约。本实现将 ``LLMMessage`` 转为 OpenAI
        格式，发送 POST 请求并解析响应为 ``LLMResponse``。
        """
        await self._ensure_api_key()
        body = self._build_body(messages, tools, temperature, model, stream=False)
        resp = await self._client.post(self._url(), json=body, headers=self._headers())
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(
                f"OpenAI Chat Completions 请求失败: HTTP {resp.status_code}, "
                f"body={resp.text}"
            )
        return self._parse_response(resp.json())

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """调用 OpenAI Chat Completions 流式接口（SSE）。

        解析 SSE 增量：
            - ``delta.content`` 实时 yield（文本片段）
            - ``delta.reasoning_content`` 实时 yield（思考链增量,与 content 同构透传）
            - ``delta.tool_calls`` 按 ``index`` 累积分片，流末尾一次性 yield 完整列表
            - 末尾 chunk 携带 ``finish_reason`` 与 ``usage``（含多模态明细）
        """
        await self._ensure_api_key()
        body = self._build_body(messages, tools, temperature, model, stream=True)

        # tool_calls 分片累积器：index -> {id, name, arguments_str}
        # OpenAI SSE 协议：每个 tool_call 分片含 index（第几个工具调用），
        # arguments 为 JSON 字符串片段，需按 index 拼接得到完整 arguments。
        tc_acc: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: LLMUsage | None = None

        async with self._client.stream(
            "POST", self._url(), json=body, headers=self._headers()
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                body_text = await response.aread()
                raise RuntimeError(
                    f"OpenAI Chat Completions 流式请求失败: "
                    f"HTTP {response.status_code}, body={body_text.decode('utf-8', 'replace')}"
                )
            async for line in response.aiter_lines():
                # SSE 格式：每行形如 "data: {...}" 或 "data: [DONE]"
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue  # 跳过无法解析的行（部分实现会发空行/注释）

                # 解析 choices[0].delta
                choices = chunk.get("choices") or []
                delta: dict[str, Any] = (
                    choices[0].get("delta") if choices else {}
                ) or {}

                # 1. 文本增量：实时 yield
                delta_content = delta.get("content")
                # 1b. 思考链增量:与 content 同构,直接透传(不累积)
                delta_reasoning = delta.get("reasoning_content")
                if delta_content or delta_reasoning:
                    yield ChatChunk(
                        delta_content=delta_content,
                        delta_reasoning_content=delta_reasoning,
                        raw=chunk,
                    )

                # 2. tool_calls 分片：按 index 累积
                for raw_tc in delta.get("tool_calls") or []:
                    idx = raw_tc.get("index", 0)
                    slot = tc_acc.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    func = raw_tc.get("function") or {}
                    if raw_tc.get("id"):
                        slot["id"] = raw_tc["id"]
                    if func.get("name"):
                        slot["name"] = func["name"]
                    if func.get("arguments"):
                        slot["arguments"] += func["arguments"]

                # 3. finish_reason（末尾 chunk）
                if choices:
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish_reason = fr

                # 4. usage（末尾 chunk，需 stream_options.include_usage=True）
                if chunk.get("usage"):
                    usage = OpenAIClient._parse_usage(chunk["usage"])

        # 流末尾：yield 完整 tool_calls + finish_reason + usage。
        # OpenAI SSE 总会在末尾发 finish_reason chunk，故末尾 chunk 总会 yield。
        tool_calls: list[ToolCall] | None = None
        if tc_acc:
            tool_calls = []
            for idx in sorted(tc_acc.keys()):
                slot = tc_acc[idx]
                # arguments 解析失败降级为空 dict（与非流式 _parse_response 一致）
                try:
                    arguments: dict = (
                        json.loads(slot["arguments"])
                        if slot["arguments"]
                        else {}
                    )
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                tool_calls.append(
                    ToolCall(id=slot["id"], name=slot["name"], arguments=arguments)
                )

        yield ChatChunk(
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    @staticmethod
    def _parse_usage(raw_usage: dict[str, Any]) -> LLMUsage:
        """解析 OpenAI / MiMo / DeepSeek 响应的 usage 字段。

        基础三字段(prompt/completion/total)所有 OpenAI 兼容 API 均返回;
        明细字段从 ``prompt_tokens_details`` 与 ``completion_tokens_details``
        解析,用于多模态计费分析。未返回的明细字段默认 0。

        Args:
            raw_usage: 响应 dict 中的 ``usage`` 子 dict。

        Returns:
            LLMUsage: 含全部明细字段的用量统计。
        """
        pt_details = raw_usage.get("prompt_tokens_details") or {}
        ct_details = raw_usage.get("completion_tokens_details") or {}
        return LLMUsage(
            prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
            completion_tokens=int(raw_usage.get("completion_tokens", 0)),
            total_tokens=int(raw_usage.get("total_tokens", 0)),
            cached_tokens=int(pt_details.get("cached_tokens", 0)),
            reasoning_tokens=int(ct_details.get("reasoning_tokens", 0)),
            image_tokens=int(pt_details.get("image_tokens", 0)),
            audio_tokens=int(pt_details.get("audio_tokens", 0)),
            video_tokens=int(pt_details.get("video_tokens", 0)),
        )

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> LLMResponse:
        """解析 OpenAI 响应 dict 为 ``LLMResponse``。

        - ``choices[0].message.content`` -> content
        - ``choices[0].message.tool_calls`` -> ToolCall 列表（arguments JSON 解析失败用 {}）
        - ``usage`` -> LLMUsage(含多模态明细字段)
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

        # 解析 usage(含多模态明细)
        raw_usage = data.get("usage") or {}
        usage = OpenAIClient._parse_usage(raw_usage)

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
