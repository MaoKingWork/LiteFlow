"""LLMStep:文本输出 / JSON 输出 / Function Call 循环 / 输出契约。"""
from __future__ import annotations

from pydantic import BaseModel

from agentkit.core.agent import AgentConfig
from agentkit.core.context import Context
from agentkit.llm.base import LLMResponse, LLMUsage
from agentkit.llm.mock import MockClient
from agentkit.steps.llm_step import LLMStep
from agentkit.tests.conftest import EchoTool, RecordingHooks


# ---------------------------------------------------------------------------
# 辅助:构造带 MockClient 的 LLMStep
# ---------------------------------------------------------------------------
def _make_step(mock_client: MockClient, agent: AgentConfig, **kw) -> LLMStep:
    return LLMStep(
        id="llm1",
        agent=agent,
        prompt="{{question}}",
        output="answer",
        llm_client=mock_client,
        **kw,
    )


def _agent(**kw) -> AgentConfig:
    defaults = dict(name="test", model="gpt-4o-mini", system="你是助手")
    defaults.update(kw)
    return AgentConfig(**defaults)


# ---------------------------------------------------------------------------
# 文本输出
# ---------------------------------------------------------------------------
async def test_llm_text_output():
    mc = MockClient(script=[{"content": "hello world"}])
    step = _make_step(mc, _agent())
    ctx = Context()
    ctx.set("question", "hi")
    await step.execute(ctx)

    assert ctx.get("answer") == "hello world"
    assert mc.call_count == 1


# ---------------------------------------------------------------------------
# JSON 输出
# ---------------------------------------------------------------------------
async def test_llm_json_output():
    mc = MockClient(script=[{"content": '{"name": "alice", "age": 30}'}])
    step = _make_step(mc, _agent(), output_format="json")
    ctx = Context()
    ctx.set("question", "extract")
    await step.execute(ctx)

    result = ctx.get("answer")
    assert result["name"] == "alice"
    assert result["age"] == 30


# ---------------------------------------------------------------------------
# Function Call 循环
# ---------------------------------------------------------------------------
async def test_llm_function_call_loop(echo_tool):
    """LLM 返回 tool_calls → 执行工具 → LLM 返回最终文本。"""
    mc = MockClient(script=[
        {"tool_calls": [{"id": "c1", "name": "test.echo", "arguments": {"x": 1}}]},
        {"content": "tool returned"},
    ])
    agent = _agent(tools=["test.echo"], max_tool_iterations=3)
    step = _make_step(mc, agent)
    ctx = Context()
    ctx.set("question", "call echo")
    await step.execute(ctx)

    assert ctx.get("answer") == "tool returned"
    assert mc.call_count == 2  # 一次 tool_call + 一次最终文本


# ---------------------------------------------------------------------------
# 输出契约:Pydantic Model 校验
# ---------------------------------------------------------------------------
class _Person(BaseModel):
    name: str
    age: int


async def test_llm_output_contract_pydantic():
    mc = MockClient(script=[{"content": '{"name": "bob", "age": 25}'}])
    agent = _agent(output_model=_Person)
    step = _make_step(mc, agent)
    ctx = Context()
    ctx.set("question", "extract")
    await step.execute(ctx)

    result = ctx.get("answer")
    assert isinstance(result, _Person)
    assert result.name == "bob"
    assert result.age == 25


# ---------------------------------------------------------------------------
# on_llm_call Hook + token usage trace
# ---------------------------------------------------------------------------
async def test_llm_on_llm_call_hook_and_usage():
    mc = MockClient(script=[
        {"content": "ok", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ])
    hooks = RecordingHooks()
    step = _make_step(mc, _agent())
    ctx = Context()
    ctx.set("question", "hi")
    trace = await step.execute(ctx, hooks)

    assert "on_llm_call" in hooks.events
    assert trace.token_usage == 15


# ---------------------------------------------------------------------------
# prompt 模板解析
# ---------------------------------------------------------------------------
async def test_llm_prompt_template():
    mc = MockClient(script=[{"content": "response"}])
    step = _make_step(mc, _agent())
    ctx = Context()
    ctx.set("question", "what is 1+1?")
    await step.execute(ctx)

    # MockClient 记录了 messages
    messages = mc.history[0]["messages"]
    user_msg = [m for m in messages if m.role == "user"][0]
    assert user_msg.content == "what is 1+1?"
