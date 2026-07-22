"""llm.mock —— 测试用 Mock 客户端，不消耗 token 不发网络请求。

本模块提供 ``MockClient``，用于在单元测试 / 本地调试中替代真实 LLM 客户端。
通过预设响应序列（``responses`` 或便利构造 ``script``），按调用顺序依次返回，
并将每次入参记录到 ``history`` 供断言。

设计原则：
    - 零网络零 token：所有调用纯内存完成，可在 CI 中无密钥运行。
    - 可断言：``history`` 完整记录 messages / tools / temperature / model。
    - 可组合：``add_response`` 支持运行时追加响应，便于动态测试场景。
    - 仅依赖 ``llm.base``，无循环依赖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agentkit.llm.base import (
    ChatChunk,
    LLMClient,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    ToolCall,
)


class MockClient(LLMClient):
    """测试用 Mock LLM 客户端。

    按预设响应序列依次返回 ``chat`` 调用结果，并将每次调用入参记录到
    ``history``，便于在测试中精确断言 LLMStep 等组件的请求构造是否正确。

    Args:
        responses: 预设的 ``LLMResponse`` 序列。按 ``chat`` 调用顺序返回；
                   耗尽后再调用抛 ``RuntimeError``。
        call_count: 初始调用计数，默认 0。
        script:    便利构造：list[dict]，每个 dict 形如
                   ``{"content": "...", "tool_calls": [{"id","name","arguments"}]}``，
                   自动转换为 ``LLMResponse``。与 ``responses`` 同时给出时，
                   ``responses`` 优先。
        stream_chunk_size: 流式切片字符数。默认 0 表示不切片（退化为单 chunk，
                   继承 ``LLMClient.chat_stream`` 默认行为）。设为正整数时，
                   ``chat_stream`` 会把 content 按此大小切成多片依次 yield，
                   用于测试流式累积逻辑。

    用法示例::

        # 便利构造：第一次返回文本，第二次返回工具调用
        mc = MockClient(script=[
            {"content": "hello"},
            {"tool_calls": [{"id": "c1", "name": "db.query", "arguments": {"sql": "SELECT 1"}}]},
        ])
        r1 = await mc.chat([LLMMessage(role="user", content="hi")])
        r2 = await mc.chat([LLMMessage(role="user", content="go")])
        assert r1.content == "hello"
        assert r2.tool_calls[0].name == "db.query"
        assert mc.call_count == 2

        # 断言入参
        assert mc.history[0]["messages"][0].content == "hi"

        # 运行时追加响应
        mc.add_response(LLMResponse(content="more"))

        # 流式测试：按 3 字符切片
        mc = MockClient(script=[{"content": "hello world"}], stream_chunk_size=3)
        chunks = [c async for c in mc.chat_stream([LLMMessage(role="user", content="hi")])]
        # chunks[0].delta_content="hel", chunks[1].delta_content="lo ", ...
        assert "".join(c.delta_content for c in chunks if c.delta_content) == "hello world"
    """

    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        call_count: int = 0,
        script: list[dict] | None = None,
        stream_chunk_size: int = 0,
    ) -> None:
        # script 便利构造：自动转 LLMResponse；responses 显式传入时优先
        if responses is None and script is not None:
            responses = [self._script_to_response(s) for s in script]
        self.responses: list[LLMResponse] = list(responses) if responses else []
        self.call_count: int = call_count
        # history 记录每次 chat 的入参，供测试断言
        self.history: list[dict[str, Any]] = []
        # 流式切片大小：>0 时 chat_stream 按 char 切片，模拟真流式
        self.stream_chunk_size: int = stream_chunk_size

    @staticmethod
    def _script_to_response(item: dict) -> LLMResponse:
        """把 script dict 转换为 ``LLMResponse``。

        支持的 key：
            - ``content``:            文本内容
            - ``reasoning_content``:  思考链内容(存入 raw 供 LLMStep 回填)
            - ``tool_calls``:         list[dict]，每个 dict 含 id / name / arguments
            - ``finish_reason``:      结束原因（可选）
            - ``usage``:              LLMUsage 或 dict（可选）
        """
        tool_calls: list[ToolCall] = []
        for tc in item.get("tool_calls") or []:
            tool_calls.append(
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments", {}) or {},
                )
            )
        raw: dict[str, Any] | None = None
        if item.get("reasoning_content"):
            raw = {"reasoning_content": item["reasoning_content"]}
        return LLMResponse(
            content=item.get("content"),
            tool_calls=tool_calls,
            finish_reason=item.get("finish_reason", ""),
            usage=item.get("usage", None) or LLMUsage(),  # type: ignore[arg-type]
            raw=raw,
        )

    def add_response(self, resp: LLMResponse) -> None:
        """追加一个响应到序列末尾。

        适用于测试中动态决定后续响应的场景。
        """
        self.responses.append(resp)

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> LLMResponse:
        """返回预设响应并记录入参。

        每次调用递增 ``call_count``，返回 ``responses[call_count - 1]``。
        若响应已耗尽（``call_count > len(responses)``），抛 ``RuntimeError``。
        """
        self.call_count += 1
        # 完整记录入参，便于测试断言 LLMStep 的请求构造
        self.history.append(
            {
                "messages": messages,
                "tools": tools,
                "temperature": temperature,
                "model": model,
            }
        )
        # 索引越界即响应耗尽
        if self.call_count > len(self.responses):
            raise RuntimeError(
                f"MockClient 响应已耗尽: call_count={self.call_count}, "
                f"len(responses)={len(self.responses)}"
            )
        return self.responses[self.call_count - 1]

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """流式返回预设响应。

        ``stream_chunk_size > 0`` 时，把 content 按字符切片依次 yield（模拟真流式），
        末尾 yield 一个携带 ``tool_calls`` / ``finish_reason`` / ``usage`` 的 chunk。
        ``stream_chunk_size == 0`` 时退化为单 chunk（继承父类默认行为）。
        """
        # 复用 chat 取预设响应（含 history 记录与耗尽检查）
        resp = await self.chat(
            messages, tools=tools, temperature=temperature, model=model
        )

        # 切片推送 content（chunk_size==0 时整段作为一个 chunk，等价于非切片）
        if resp.content:
            if self.stream_chunk_size > 0:
                for i in range(0, len(resp.content), self.stream_chunk_size):
                    yield ChatChunk(
                        delta_content=resp.content[i : i + self.stream_chunk_size]
                    )
            else:
                yield ChatChunk(delta_content=resp.content)

        # 末尾 chunk：携带 tool_calls / finish_reason / usage
        # （stream_chunk_size==0 时也走这里，等价于单 chunk 携带完整信息，
        #   但 content 已在 chat 中返回，这里 delta_content=None）
        yield ChatChunk(
            tool_calls=resp.tool_calls or None,
            finish_reason=resp.finish_reason or None,
            usage=resp.usage,
        )


__all__ = ["MockClient"]
