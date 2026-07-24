"""端到端集成测试：会话缓存 + 长篇循环增量生成 + 断点续传 + 多 provider。

覆盖 tasks.md T9.1-T9.3 出口标准：
    - T9.1: 长篇循环增量生成 + 缓存命中（loop + condition + has() 分支）
    - T9.2: 大会话断点续传不丢历史（旁路存储 + snapshot/restore）
    - T9.3: 动态多 provider 场景（loop + 模板 key + has() 分支）

依赖前置任务：
    - T1: core/conversation.py（会话核心模块）
    - T2: core/template.py has() 表达式函数
    - T3: steps/llm_step.py 会话集成 + cached_tokens 观测
    - T4: steps/base.py iter_child_steps
    - T5: config.py 会话存储配置项
"""
from __future__ import annotations

import pytest

from agentkit.config import reset_default, set_default
from agentkit.core.agent import AgentConfig
from agentkit.core.context import Context
from agentkit.core.conversation import (
    CONVERSATION_REF_TYPE,
    CONVERSATION_TYPE,
    LocalConversationStore,
    load_and_validate,
    set_conversation_store,
)
from agentkit.llm.base import LLMMessage
from agentkit.llm.mock import MockClient
from agentkit.steps.condition_step import ConditionStep
from agentkit.steps.llm_step import LLMStep
from agentkit.steps.loop_step import LoopStep


# ---------------------------------------------------------------------------
# 辅助 fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_store(tmp_path):
    """基于 tmp_path 的 LocalConversationStore，测试结束自动恢复全局 store。"""
    store = LocalConversationStore(base_dir=str(tmp_path))
    set_conversation_store(store)
    yield store
    import agentkit.core.conversation as _conv

    _conv._global_store = None


@pytest.fixture
def small_threshold():
    """临时把 large_object_threshold 调小为 5000 字节，触发旁路存储。

    阈值需同时满足：
      - 大于 conversation_ref dict 的 _sizeof（约 1500 字节），使其内联存储
        （FrozenDict）而非 LargeRef，否则 snapshot 只留摘要破坏旁路设计。
      - 小于大会话 blob，使其正确触发旁路存储。
    """
    set_default("large_object_threshold", 5000)
    yield
    reset_default("large_object_threshold")


def _agent(**kw) -> AgentConfig:
    """构造默认 AgentConfig。"""
    defaults = dict(name="test", model="gpt-4o-mini", system="你是助手")
    defaults.update(kw)
    return AgentConfig(**defaults)


# ---------------------------------------------------------------------------
# T9.1 长篇循环增量生成 + 缓存命中
# ---------------------------------------------------------------------------
async def test_loop_incremental_generation_with_cache():
    """长篇循环增量生成 + 缓存命中。

    场景：loop 迭代 3 次，每次用 condition + has() 分支 start/continue，
    验证：
    - 第 1 轮 start 创建会话 [system, user, assistant]
    - 第 2 轮 continue 追加 [user, assistant]，会话变为 5 条消息
    - 第 3 轮 continue 追加，会话变为 7 条消息
    - cached_tokens 跨轮累计
    - output 累加（loop output_mode=append）
    """
    # 1. MockClient：每次返回不同内容 + 递增的 cached_tokens
    mc = MockClient(script=[
        {"content": "第一章正文", "usage": {"cached_tokens": 100, "total_tokens": 50}},
        {"content": "第二章正文", "usage": {"cached_tokens": 200, "total_tokens": 60}},
        {"content": "第三章正文", "usage": {"cached_tokens": 300, "total_tokens": 70}},
    ])

    agent = _agent(system="你是小说家")

    # 2. start 分支（else）：首轮创建会话
    start_step = LLMStep(
        id="write_start",
        agent=agent,
        prompt="写第{{chapter_num}}章",
        output="chapter",
        llm_client=mc,
        conversation_mode="start",
        conversation_key="chat",
    )

    # 3. continue 分支（then）：后续轮次追加
    continue_step = LLMStep(
        id="write_continue",
        agent=agent,
        prompt="续写第{{chapter_num}}章",
        output="chapter",
        llm_client=mc,
        conversation_mode="continue",
        conversation_key="chat",
    )

    # 4. ConditionStep body：has('chat') 分支
    cond = ConditionStep(
        id="branch",
        when="has('chat')",
        then_steps=[continue_step],
        else_steps=[start_step],
        output="chapter",  # 使 loop body_out 能读到子 step 产出
    )

    # 5. LoopStep：append 模式累加
    loop = LoopStep(
        id="write_loop",
        iter="[1, 2, 3]",
        item_var="chapter_num",
        step=cond,
        output="story",
        output_mode="append",
        separator="\n\n",
    )

    ctx = Context()
    await loop.execute(ctx)

    # 6. 验证会话消息数：3 → 5 → 7（最终 7 条）
    conv = ctx.get("chat")
    assert conv["_type"] == CONVERSATION_TYPE
    msgs = conv["messages"]
    assert len(msgs) == 7
    # 验证消息角色交替
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "第一章正文"
    assert msgs[3]["role"] == "user"
    assert msgs[4]["role"] == "assistant"
    assert msgs[4]["content"] == "第二章正文"
    assert msgs[5]["role"] == "user"
    assert msgs[6]["role"] == "assistant"
    assert msgs[6]["content"] == "第三章正文"

    # 7. 验证 cached_tokens 跨轮累计：100 + 200 + 300 = 600
    assert conv["meta"]["cached_tokens"] == 600

    # 8. 验证 output 累加（loop output_mode=append）
    story = ctx.get("story")
    assert isinstance(story, str)
    assert "第一章正文" in story
    assert "第二章正文" in story
    assert "第三章正文" in story
    assert "\n\n" in story

    # 9. 验证 MockClient 收到的消息数递增（2 → 4 → 6）
    #    start 轮发送 [system, user]；continue 轮发送 loaded + [user]
    assert len(mc.history) == 3
    assert len(mc.history[0]["messages"]) == 2  # [system, user]
    assert len(mc.history[1]["messages"]) == 4  # [system, user, assistant, user]
    assert len(mc.history[2]["messages"]) == 6  # + [assistant, user]


# ---------------------------------------------------------------------------
# T9.2 大会话断点续传不丢历史
# ---------------------------------------------------------------------------
async def test_large_conversation_checkpoint_resume(isolated_store, small_threshold):
    """大会话断点续传不丢历史。

    场景：
    1. 把 large_object_threshold 设为很小（5000 字节）
    2. start 模式创建一个大会话（messages 总和 > 5000 字节）
    3. 验证 ctx 中存的是 conversation_ref（不是 conversation）
    4. ctx.snapshot() → 保留 ref（小对象，不走 LargeRef）
    5. Context.restore(snapshot) → 恢复 ref
    6. load_and_validate 从 ConversationStore 回填完整 messages
    7. 验证回填的 messages 与原始一致（不丢历史）
    8. cached_tokens meta 跨 restore 保留
    """
    agent = _agent(provider="test", model="test-model", system="你是助手", tools=[])

    # MockClient 返回大 content，配合大 user prompt 使会话 blob > 5000 字节
    large_response = "y" * 3000
    mc = MockClient(script=[
        {
            "content": large_response,
            "usage": {"cached_tokens": 42, "total_tokens": 100},
        }
    ])

    step = LLMStep(
        id="chat",
        agent=agent,
        prompt="{{question}}",
        output="answer",
        llm_client=mc,
        conversation_mode="start",
        conversation_key="chat",
    )

    ctx = Context()
    ctx.set("question", "x" * 3000)  # 大 user prompt
    await step.execute(ctx)

    # 验证 LLMStep 输出正常
    assert ctx.get("answer") == large_response

    # 1. 验证 ctx 存的是 conversation_ref（旁路存储，不是内联 conversation）
    raw = ctx.get("chat")
    assert raw.get("_type") == CONVERSATION_REF_TYPE
    assert "ref" in raw
    assert raw.get("store") == "local"
    assert raw.get("size", 0) > 5000

    # 2. snapshot → 保留 ref（conversation_ref 是小对象，不走 LargeRef 摘要）
    snap = ctx.snapshot()

    # 3. restore → 恢复 ref
    restored_ctx = Context.restore(snap)
    raw_after = restored_ctx.get("chat")
    assert raw_after.get("_type") == CONVERSATION_REF_TYPE
    assert raw_after.get("ref") == raw.get("ref")

    # 4. load_and_validate 从 ConversationStore 回填完整 messages
    messages_loaded, meta_loaded = load_and_validate(
        restored_ctx, "chat", agent, compat="strict"
    )

    # 5. 验证回填的 messages 与原始一致（不丢历史）
    assert len(messages_loaded) == 3  # [system, user, assistant]
    assert messages_loaded[0].role == "system"
    assert messages_loaded[0].content == "你是助手"
    assert messages_loaded[1].role == "user"
    assert messages_loaded[1].content == "x" * 3000
    assert messages_loaded[2].role == "assistant"
    assert messages_loaded[2].content == large_response

    # 6. cached_tokens meta 跨 restore 保留
    assert meta_loaded["cached_tokens"] == 42
    assert meta_loaded["provider"] == "test"
    assert meta_loaded["model"] == "test-model"


# ---------------------------------------------------------------------------
# T9.3 动态多 provider 场景
# ---------------------------------------------------------------------------
async def test_dynamic_multi_provider():
    """动态多 provider 场景。

    场景：loop 遍历 ["openai", "deepseek", "mimo"]，
    每个 provider 用模板 key "chat_{{provider}}" 管理独立会话。

    验证：
    - 第 1 轮：has('chat_openai')=False → start，创建 chat_openai
    - 第 2 轮：has('chat_deepseek')=False → start，创建 chat_deepseek
    - 第 3 轮：has('chat_mimo')=False → start，创建 chat_mimo
    - 再循环一轮：三个 provider 都 continue
    - 最终 ctx 中有 3 个独立会话
    """
    mc = MockClient(script=[
        {"content": "openai-1"},
        {"content": "deepseek-1"},
        {"content": "mimo-1"},
        {"content": "openai-2"},
        {"content": "deepseek-2"},
        {"content": "mimo-2"},
    ])

    agent = _agent(system="你是助手")

    # start 分支：首轮为每个 provider 创建独立会话
    start_step = LLMStep(
        id="start",
        agent=agent,
        prompt="provider={{provider}} 的问题",
        output="reply",
        llm_client=mc,
        conversation_mode="start",
        conversation_key="chat_{{provider}}",
    )

    # continue 分支：后续轮次追加到对应 provider 的会话
    continue_step = LLMStep(
        id="continue",
        agent=agent,
        prompt="provider={{provider}} 的追问",
        output="reply",
        llm_client=mc,
        conversation_mode="continue",
        conversation_key="chat_{{provider}}",
    )

    # ConditionStep：has('chat_{{provider}}') 动态判断当前 provider 会话是否存在
    cond = ConditionStep(
        id="branch",
        when="has('chat_{{provider}}')",
        then_steps=[continue_step],
        else_steps=[start_step],
        output="reply",
    )

    # 两轮循环：第一轮三个 provider 各 start，第二轮各 continue
    ctx = Context()
    ctx.set(
        "providers",
        ["openai", "deepseek", "mimo", "openai", "deepseek", "mimo"],
    )
    loop = LoopStep(
        id="multi_provider_loop",
        iter="{{providers}}",
        item_var="provider",
        step=cond,
        output="replies",
        output_mode="collect",
    )
    await loop.execute(ctx)

    # 验证 3 个独立会话存在
    assert ctx.has("chat_openai")
    assert ctx.has("chat_deepseek")
    assert ctx.has("chat_mimo")

    # 每个会话 5 条消息：[system, user, assistant, user, assistant]
    # 第一轮 start 创建 [system, user, assistant]（3 条）
    # 第二轮 continue 追加 [user, assistant]（共 5 条）
    conv_openai = ctx.get("chat_openai")
    assert conv_openai["_type"] == CONVERSATION_TYPE
    assert len(conv_openai["messages"]) == 5
    assert conv_openai["messages"][0]["role"] == "system"
    assert conv_openai["messages"][1]["role"] == "user"
    assert conv_openai["messages"][2]["role"] == "assistant"
    assert conv_openai["messages"][2]["content"] == "openai-1"
    assert conv_openai["messages"][3]["role"] == "user"
    assert conv_openai["messages"][4]["role"] == "assistant"
    assert conv_openai["messages"][4]["content"] == "openai-2"

    conv_deepseek = ctx.get("chat_deepseek")
    assert len(conv_deepseek["messages"]) == 5
    assert conv_deepseek["messages"][2]["content"] == "deepseek-1"
    assert conv_deepseek["messages"][4]["content"] == "deepseek-2"

    conv_mimo = ctx.get("chat_mimo")
    assert len(conv_mimo["messages"]) == 5
    assert conv_mimo["messages"][2]["content"] == "mimo-1"
    assert conv_mimo["messages"][4]["content"] == "mimo-2"

    # 验证 MockClient 调用次数（6 次：3 start + 3 continue）
    assert mc.call_count == 6

    # 验证 collect 模式收集了 6 条回复
    replies = ctx.get("replies")
    assert len(replies) == 6
