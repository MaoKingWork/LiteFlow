"""LLMStep:文本输出 / JSON 输出 / Function Call 循环 / 输出契约。"""
from __future__ import annotations

import pytest
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


# ===========================================================================
# 会话缓存集成测试（T3.8 + T7.3）
# ===========================================================================
# 辅助:构造带会话配置的 LLMStep
def _make_conv_step(
    mock_client: MockClient,
    agent: AgentConfig,
    *,
    mode: str,
    key: str = "chat",
    **kw,
) -> LLMStep:
    return LLMStep(
        id="chat",
        agent=agent,
        prompt="{{question}}",
        output="answer",
        llm_client=mock_client,
        conversation_mode=mode,
        conversation_key=key,
        **kw,
    )


# ---------------------------------------------------------------------------
# start 模式
# ---------------------------------------------------------------------------
async def test_conversation_no_mode_unchanged():
    """不配置 conversation → 行为不变（回归测试），不写入任何会话 key。"""
    mc = MockClient(script=[{"content": "hello"}])
    step = _make_step(mc, _agent())
    ctx = Context()
    ctx.set("question", "hi")
    await step.execute(ctx)

    assert ctx.get("answer") == "hello"
    # 未配置 conversation_mode，不应写入会话 key
    assert not ctx.has("llm1")


async def test_conversation_start_creates_conversation():
    """mode=start → ctx[key] 是 conversation 类型，含 [system, user, assistant]。"""
    mc = MockClient(script=[{"content": "hello"}])
    step = _make_conv_step(mc, _agent(), mode="start")
    ctx = Context()
    ctx.set("question", "hi")
    await step.execute(ctx)

    assert ctx.get("answer") == "hello"
    conv = ctx.get("chat")
    assert conv["_type"] == "conversation"
    msgs = conv["messages"]
    assert len(msgs) == 3
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "你是助手"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "hi"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "hello"
    # meta 包含派生属性快照
    assert conv["meta"]["cached_tokens"] == 0
    assert conv["meta"]["model"] == "gpt-4o-mini"


async def test_conversation_start_overwrites_existing():
    """mode=start 且 key 已存在 → 覆盖旧会话。"""
    mc = MockClient(script=[{"content": "second"}])
    step = _make_conv_step(mc, _agent(), mode="start")
    ctx = Context()
    ctx.set("question", "q2")
    # 预设一个旧的会话（内容不同）
    ctx.set("chat", {"_type": "conversation", "messages": [], "meta": {}})
    await step.execute(ctx)

    conv = ctx.get("chat")
    msgs = conv["messages"]
    assert len(msgs) == 3
    assert msgs[2]["content"] == "second"


async def test_conversation_start_with_fc_loop():
    """start 模式 + Function Call 循环 → 会话含 [system, user, assistant(tc), tool, assistant]。"""
    mc = MockClient(script=[
        {"tool_calls": [{"id": "c1", "name": "test.echo", "arguments": {"x": 1}}]},
        {"content": "final answer"},
    ])
    agent = _agent(tools=["test.echo"], max_tool_iterations=3)
    step = _make_conv_step(mc, agent, mode="start")
    ctx = Context()
    ctx.set("question", "call echo")
    # 需要注册 echo 工具
    from agentkit.tests.conftest import EchoTool
    from agentkit.tools.base import register as register_tool
    register_tool(EchoTool())
    await step.execute(ctx)

    conv = ctx.get("chat")
    msgs = conv["messages"]
    # [system, user, assistant(tool_calls), tool, assistant(final)]
    assert len(msgs) == 5
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["tool_calls"] is not None
    assert msgs[3]["role"] == "tool"
    assert msgs[4]["role"] == "assistant"
    assert msgs[4]["content"] == "final answer"


# ---------------------------------------------------------------------------
# continue 模式
# ---------------------------------------------------------------------------
async def test_conversation_continue_appends():
    """先 start 创建会话，再 continue → messages = loaded + [user, assistant]。"""
    ctx = Context()
    ctx.set("question", "q1")

    # start
    mc1 = MockClient(script=[{"content": "answer1"}])
    step1 = _make_conv_step(mc1, _agent(), mode="start")
    await step1.execute(ctx)

    # continue
    ctx.set("question", "q2")
    mc2 = MockClient(script=[{"content": "answer2"}])
    step2 = _make_conv_step(mc2, _agent(), mode="continue")
    await step2.execute(ctx)

    conv = ctx.get("chat")
    msgs = conv["messages"]
    # [system, user(q1), assistant(answer1), user(q2), assistant(answer2)]
    assert len(msgs) == 5
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "q1"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "answer1"
    assert msgs[3]["role"] == "user"
    assert msgs[3]["content"] == "q2"
    assert msgs[4]["role"] == "assistant"
    assert msgs[4]["content"] == "answer2"


async def test_conversation_continue_missing_from_key():
    """continue 但 from_key 不存在 → 抛 KeyError。"""
    mc = MockClient(script=[{"content": "x"}])
    step = LLMStep(
        id="chat",
        agent=_agent(),
        prompt="{{question}}",
        output="answer",
        llm_client=mc,
        conversation_mode="continue",
        conversation_key="chat",
        conversation_from="nonexistent",
    )
    ctx = Context()
    ctx.set("question", "hi")
    with pytest.raises(KeyError):
        await step.execute(ctx)


async def test_conversation_continue_wrong_type():
    """continue 但 key 非会话类型 → 抛 ConversationTypeError。"""
    from agentkit.core.conversation import ConversationTypeError

    mc = MockClient(script=[{"content": "x"}])
    step = _make_conv_step(mc, _agent(), mode="continue")
    ctx = Context()
    ctx.set("question", "hi")
    ctx.set("chat", "not a conversation")  # 普通字符串
    with pytest.raises(ConversationTypeError):
        await step.execute(ctx)


# ---------------------------------------------------------------------------
# fork 模式
# ---------------------------------------------------------------------------
async def test_conversation_fork_last():
    """fork_at="last" → 截断末尾 assistant，替换最后 user。"""
    ctx = Context()
    ctx.set("question", "q1")

    # start: [system, user(q1), assistant(answer1)]
    mc1 = MockClient(script=[{"content": "answer1"}])
    step1 = _make_conv_step(mc1, _agent(), mode="start")
    await step1.execute(ctx)

    # fork at last user: 截断到最后一个 user(q1) 并替换为 q2_new
    ctx.set("question", "q2_new")
    mc2 = MockClient(script=[{"content": "answer2"}])
    step2 = _make_conv_step(mc2, _agent(), mode="fork", conversation_fork_at="last")
    await step2.execute(ctx)

    conv = ctx.get("chat")
    msgs = conv["messages"]
    # [system, user(q2_new), assistant(answer2)]
    assert len(msgs) == 3
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "q2_new"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "answer2"


async def test_conversation_fork_at_zero():
    """fork_at=0 → 截断到第 1 个 user 消息。"""
    ctx = Context()
    ctx.set("question", "q1")

    # start
    mc1 = MockClient(script=[{"content": "answer1"}])
    step1 = _make_conv_step(mc1, _agent(), mode="start")
    await step1.execute(ctx)

    # continue: [system, user(q1), assistant(a1), user(q2), assistant(a2)]
    ctx.set("question", "q2")
    mc2 = MockClient(script=[{"content": "answer2"}])
    step2 = _make_conv_step(mc2, _agent(), mode="continue")
    await step2.execute(ctx)

    # fork at 0: 截断到第 1 个 user(q1) 并替换为 q3_new
    ctx.set("question", "q3_new")
    mc3 = MockClient(script=[{"content": "answer3"}])
    step3 = _make_conv_step(mc3, _agent(), mode="fork", conversation_fork_at=0)
    await step3.execute(ctx)

    conv = ctx.get("chat")
    msgs = conv["messages"]
    # [system, user(q3_new), assistant(answer3)]
    assert len(msgs) == 3
    assert msgs[1]["content"] == "q3_new"
    assert msgs[2]["content"] == "answer3"


# ---------------------------------------------------------------------------
# 模板 key
# ---------------------------------------------------------------------------
async def test_conversation_template_key():
    """conversation_key="chat_{{provider}}" → 渲染为 chat_openai。"""
    mc = MockClient(script=[{"content": "hello"}])
    step = _make_conv_step(mc, _agent(), mode="start", key="chat_{{provider}}")
    ctx = Context()
    ctx.set("question", "hi")
    ctx.set("provider", "openai")
    await step.execute(ctx)

    assert ctx.has("chat_openai")
    conv = ctx.get("chat_openai")
    assert conv["_type"] == "conversation"


async def test_conversation_template_key_missing_var():
    """模板 key 缺失变量 → 抛 KeyError。"""
    mc = MockClient(script=[{"content": "hello"}])
    step = _make_conv_step(mc, _agent(), mode="start", key="chat_{{missing_var}}")
    ctx = Context()
    ctx.set("question", "hi")
    with pytest.raises(KeyError):
        await step.execute(ctx)


# ---------------------------------------------------------------------------
# compat 档位
# ---------------------------------------------------------------------------
async def test_conversation_compat_strict_tools_mismatch():
    """strict 档 tools_sig 不一致 → 抛 ConversationCompatError。"""
    from agentkit.core.conversation import ConversationCompatError

    ctx = Context()
    ctx.set("question", "q1")

    # start with tools=["test.echo"]
    mc1 = MockClient(script=[{"content": "answer1"}])
    step1 = _make_conv_step(
        mc1, _agent(tools=["test.echo"], max_tool_iterations=3), mode="start"
    )
    await step1.execute(ctx)

    # continue with tools=[] → tools_sig 不一致
    ctx.set("question", "q2")
    mc2 = MockClient(script=[{"content": "answer2"}])
    step2 = _make_conv_step(
        mc2, _agent(tools=[]), mode="continue", conversation_compat="strict"
    )
    with pytest.raises(ConversationCompatError):
        await step2.execute(ctx)


async def test_conversation_compat_passthrough_tools_mismatch():
    """passthrough 档 tools_sig 不一致 → warning 放行。"""
    ctx = Context()
    ctx.set("question", "q1")

    mc1 = MockClient(script=[{"content": "answer1"}])
    step1 = _make_conv_step(
        mc1, _agent(tools=["test.echo"], max_tool_iterations=3), mode="start"
    )
    await step1.execute(ctx)

    ctx.set("question", "q2")
    mc2 = MockClient(script=[{"content": "answer2"}])
    step2 = _make_conv_step(
        mc2, _agent(tools=[]), mode="continue", conversation_compat="passthrough"
    )
    await step2.execute(ctx)  # 不应抛异常

    conv = ctx.get("chat")
    msgs = conv["messages"]
    assert len(msgs) == 5  # [sys, user1, asst1, user2, asst2]


async def test_conversation_compat_strict_system_mismatch_warning():
    """strict 档 system_hash 不一致 → 仅 warning（不抛错）。"""
    ctx = Context()
    ctx.set("question", "q1")

    mc1 = MockClient(script=[{"content": "answer1"}])
    step1 = _make_conv_step(mc1, _agent(system="sys1"), mode="start")
    await step1.execute(ctx)

    ctx.set("question", "q2")
    mc2 = MockClient(script=[{"content": "answer2"}])
    step2 = _make_conv_step(
        mc2, _agent(system="sys2"), mode="continue", conversation_compat="strict"
    )
    await step2.execute(ctx)  # 不应抛异常（system 不一致仅 warning）

    conv = ctx.get("chat")
    msgs = conv["messages"]
    assert len(msgs) == 5


# ---------------------------------------------------------------------------
# cached_tokens 累计（T7）
# ---------------------------------------------------------------------------
async def test_cached_tokens_single_round():
    """MockClient 返回 cached_tokens=500 → meta.cached_tokens = 500。"""
    mc = MockClient(script=[{
        "content": "hello",
        "usage": {"cached_tokens": 500, "total_tokens": 100},
    }])
    step = _make_conv_step(mc, _agent(), mode="start")
    ctx = Context()
    ctx.set("question", "hi")
    await step.execute(ctx)

    conv = ctx.get("chat")
    assert conv["meta"]["cached_tokens"] == 500


async def test_cached_tokens_multi_round():
    """多轮累计（start + continue）→ 1300。"""
    ctx = Context()
    ctx.set("question", "q1")

    # start: cached_tokens=500
    mc1 = MockClient(script=[{
        "content": "answer1",
        "usage": {"cached_tokens": 500, "total_tokens": 100},
    }])
    step1 = _make_conv_step(mc1, _agent(), mode="start")
    await step1.execute(ctx)
    assert ctx.get("chat")["meta"]["cached_tokens"] == 500

    # continue: cached_tokens=800, 累计 500+800=1300
    ctx.set("question", "q2")
    mc2 = MockClient(script=[{
        "content": "answer2",
        "usage": {"cached_tokens": 800, "total_tokens": 200},
    }])
    step2 = _make_conv_step(mc2, _agent(), mode="continue")
    await step2.execute(ctx)

    conv = ctx.get("chat")
    assert conv["meta"]["cached_tokens"] == 1300
