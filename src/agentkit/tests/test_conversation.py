"""core.conversation —— 会话缓存核心模块单元测试。

覆盖出口标准（对齐 tasks.md T1.12）：
    - 序列化往返：messages_to_dicts ↔ dicts_to_messages 保留所有字段
    - pack/unpack：内联格式封装/解析 + 类型不匹配抛 ConversationTypeError
    - _tools_signature / _hash_system：派生属性指纹稳定性
    - fork_at_user：last / N / new_prompt / 越界 / 无 user / 不修改输入
    - check_compatibility：strict/passthrough 档位 + system_override 静默
    - LocalConversationStore：内容寻址去重 / load / exists / 原子 rename
    - save_conversation + load_and_validate：内联路径 + 旁路路径 + snapshot/restore
    - cached_tokens 累计
"""
from __future__ import annotations

import json
import logging
import os

import pytest

from agentkit.config import reset_default, set_default
from agentkit.core.agent import AgentConfig
from agentkit.core.context import Context
from agentkit.core.conversation import (
    CONVERSATION_REF_TYPE,
    CONVERSATION_TYPE,
    ConversationCompatError,
    ConversationStore,
    ConversationTypeError,
    LocalConversationStore,
    MessageNormalizer,
    _hash_system,
    _tools_signature,
    check_compatibility,
    dicts_to_messages,
    fork_at_user,
    get_conversation_store,
    load_and_validate,
    messages_to_dicts,
    pack_conversation,
    save_conversation,
    set_conversation_store,
    unpack_conversation,
)
from agentkit.llm.base import LLMMessage, ToolCall


# ---------------------------------------------------------------------------
# 辅助 fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def agent() -> AgentConfig:
    """构造一个默认 AgentConfig（provider/model/system/tools 均有值）。"""
    return AgentConfig(
        name="test_agent",
        provider="deepseek",
        model="deepseek-chat",
        system="You are a helpful assistant.",
        tools=["search", "calc"],
    )


@pytest.fixture
def sample_messages() -> list[LLMMessage]:
    """构造覆盖全部字段的样本消息列表。"""
    return [
        LLMMessage(role="system", content="You are a helpful assistant."),
        LLMMessage(role="user", content="hello"),
        LLMMessage(
            role="assistant",
            content="I will call a tool",
            tool_calls=[
                ToolCall(id="call_1", name="search", arguments={"q": "test"}),
            ],
            reasoning_content="thinking about search",
        ),
        LLMMessage(
            role="tool",
            content="result data",
            tool_call_id="call_1",
            name="search",
        ),
        LLMMessage(role="assistant", content="final answer"),
        LLMMessage(role="user", content="another question"),
        LLMMessage(role="assistant", content="another answer"),
    ]


@pytest.fixture
def isolated_store(tmp_path) -> LocalConversationStore:
    """基于 tmp_path 的 LocalConversationStore，并注入全局，测试结束自动恢复。"""
    store = LocalConversationStore(base_dir=str(tmp_path))
    set_conversation_store(store)
    yield store
    # 恢复全局 store 为 None，避免污染其他测试
    import agentkit.core.conversation as _conv

    _conv._global_store = None


@pytest.fixture
def small_threshold():
    """临时把 large_object_threshold 调小为 5000 字节。

    阈值需同时满足：
      - 大于 conversation_ref dict 的 _sizeof（约 1500 字节，含 dict 结构
        开销 + meta），使其在 Context 中内联存储（FrozenDict）而非 LargeRef。
        若被 LargeRef 包装，snapshot 时只留摘要，破坏旁路存储设计。
      - 小于大会话 blob（~6300 字节），使其正确触发旁路存储。
    生产环境阈值为 1MB，conversation_ref（~1.5KB）远小于 1MB，设计无此问题。
    """
    set_default("large_object_threshold", 5000)
    yield
    reset_default("large_object_threshold")


@pytest.fixture
def large_messages() -> list[LLMMessage]:
    """构造大会话（blob > 5000 字节，用于触发旁路存储）。

    content 撑大确保序列化后 blob（~6300 字节）明显超过 small_threshold(5000)，
    同时 conversation_ref dict（~1500 字节）保持 < 5000 字节内联存储。
    """
    return [
        LLMMessage(role="system", content="You are a helpful assistant."),
        LLMMessage(role="user", content="x" * 3000),
        LLMMessage(role="assistant", content="y" * 3000),
    ]


# ---------------------------------------------------------------------------
# T1.2 序列化往返
# ---------------------------------------------------------------------------
def test_messages_to_dicts_preserves_all_fields(sample_messages):
    """messages_to_dicts 保留 role/content/tool_calls/tool_call_id/name/reasoning_content。"""
    dicts = messages_to_dicts(sample_messages)

    # system
    assert dicts[0]["role"] == "system"
    assert dicts[0]["content"] == "You are a helpful assistant."
    # user
    assert dicts[1]["role"] == "user"
    assert dicts[1]["content"] == "hello"
    # assistant with tool_calls + reasoning_content
    assert dicts[2]["role"] == "assistant"
    assert dicts[2]["content"] == "I will call a tool"
    assert dicts[2]["reasoning_content"] == "thinking about search"
    tcs = dicts[2]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "call_1"
    assert tcs[0]["name"] == "search"
    assert tcs[0]["arguments"] == {"q": "test"}
    # tool message
    assert dicts[3]["role"] == "tool"
    assert dicts[3]["tool_call_id"] == "call_1"
    assert dicts[3]["name"] == "search"
    assert dicts[3]["content"] == "result data"


def test_dicts_to_messages_restores_all_fields(sample_messages):
    """dicts_to_messages 完整还原 LLMMessage（含 ToolCall 实例）。"""
    dicts = messages_to_dicts(sample_messages)
    restored = dicts_to_messages(dicts)

    assert len(restored) == len(sample_messages)
    for orig, got in zip(sample_messages, restored):
        assert got.role == orig.role
        assert got.content == orig.content
        assert got.tool_call_id == orig.tool_call_id
        assert got.name == orig.name
        assert got.reasoning_content == orig.reasoning_content
        # tool_calls 还原为 ToolCall 实例
        if orig.tool_calls is None:
            assert got.tool_calls is None
        else:
            assert got.tool_calls is not None
            assert len(got.tool_calls) == len(orig.tool_calls)
            for o_tc, g_tc in zip(orig.tool_calls, got.tool_calls):
                assert g_tc.id == o_tc.id
                assert g_tc.name == o_tc.name
                assert g_tc.arguments == o_tc.arguments
                # 确保是 ToolCall 实例而非 dict
                assert isinstance(g_tc, ToolCall)


def test_serialization_roundtrip_jsonable(sample_messages):
    """序列化结果可被 json.dumps/dumps 往返。"""
    dicts = messages_to_dicts(sample_messages)
    s = json.dumps(dicts, ensure_ascii=False)
    restored = dicts_to_messages(json.loads(s))
    assert len(restored) == len(sample_messages)


# ---------------------------------------------------------------------------
# T1.3 派生属性指纹
# ---------------------------------------------------------------------------
def test_tools_signature_order_independent():
    """相同工具集不同顺序生成相同签名。"""
    sig1 = _tools_signature(["search", "calc"])
    sig2 = _tools_signature(["calc", "search"])
    assert sig1 == sig2
    assert len(sig1) == 8


def test_tools_signature_dedup():
    """重复工具名去重后取签名。"""
    sig1 = _tools_signature(["search", "search", "calc"])
    sig2 = _tools_signature(["search", "calc"])
    assert sig1 == sig2


def test_tools_signature_different_tools_differ():
    """不同工具集生成不同签名。"""
    sig1 = _tools_signature(["search", "calc"])
    sig2 = _tools_signature(["search", "weather"])
    assert sig1 != sig2


def test_tools_signature_empty():
    """空工具集生成稳定签名。"""
    sig = _tools_signature([])
    assert len(sig) == 8


def test_hash_system_stable():
    """相同 system 相同哈希。"""
    h1 = _hash_system("You are a helpful assistant.")
    h2 = _hash_system("You are a helpful assistant.")
    assert h1 == h2
    assert len(h1) == 8


def test_hash_system_different():
    """不同 system 不同哈希。"""
    h1 = _hash_system("You are a helpful assistant.")
    h2 = _hash_system("You are a coder.")
    assert h1 != h2


def test_hash_system_unicode():
    """中文 system 正常哈希。"""
    h = _hash_system("你是一个助手。")
    assert len(h) == 8


# ---------------------------------------------------------------------------
# T1.4 pack / unpack
# ---------------------------------------------------------------------------
def test_pack_conversation_structure(sample_messages, agent):
    """pack_conversation 生成正确结构（_type/messages/meta）。"""
    conv = pack_conversation(sample_messages, agent)

    assert conv["_type"] == CONVERSATION_TYPE
    assert len(conv["messages"]) == len(sample_messages)
    meta = conv["meta"]
    assert meta["provider"] == "deepseek"
    assert meta["model"] == "deepseek-chat"
    assert meta["tools_sig"] == _tools_signature(agent.tools)
    assert meta["system_hash"] == _hash_system(agent.system)
    assert meta["cached_tokens"] == 0


def test_pack_conversation_none_provider_model():
    """provider/model 为 None 时 meta 存空字符串。"""
    agent = AgentConfig(provider=None, model="", system="", tools=[])
    conv = pack_conversation([], agent)
    assert conv["meta"]["provider"] == ""
    assert conv["meta"]["model"] == ""


def test_unpack_conversation_restores(sample_messages, agent):
    """unpack_conversation 正确还原 messages + meta。"""
    conv = pack_conversation(sample_messages, agent)
    messages, meta = unpack_conversation(conv, "test_key")

    assert len(messages) == len(sample_messages)
    for orig, got in zip(sample_messages, messages):
        assert got.role == orig.role
        assert got.content == orig.content
    assert meta["provider"] == "deepseek"
    assert meta["tools_sig"] == _tools_signature(agent.tools)


def test_unpack_conversation_type_error_on_non_mapping():
    """非 Mapping 类型抛 ConversationTypeError。"""
    with pytest.raises(ConversationTypeError):
        unpack_conversation("not a dict", "my_key")


def test_unpack_conversation_type_error_on_wrong_type():
    """_type 不匹配抛 ConversationTypeError。"""
    raw = {"_type": "other_type", "messages": []}
    with pytest.raises(ConversationTypeError):
        unpack_conversation(raw, "my_key")


def test_unpack_conversation_type_error_message_mentions_key():
    """ConversationTypeError 文案包含 key 与实际类型名。"""
    raw = {"_type": "other"}
    with pytest.raises(ConversationTypeError) as exc_info:
        unpack_conversation(raw, "my_special_key")
    msg = str(exc_info.value)
    assert "my_special_key" in msg
    assert "dict" in msg  # 实际类型名


# ---------------------------------------------------------------------------
# T1.7 fork_at_user
# ---------------------------------------------------------------------------
def test_fork_at_user_last(sample_messages):
    """fork_at='last' 截断末尾 assistant，保留末尾 user。"""
    result = fork_at_user(sample_messages, fork_at="last")

    # 末尾 user 在 index 5，截断后应保留 0..5（6 条）
    assert len(result) == 6
    assert result[-1].role == "user"
    assert result[-1].content == "another question"
    # 末尾 assistant 被去掉
    assert all(m.role != "assistant" or i < 5 for i, m in enumerate(result))


def test_fork_at_user_integer_n(sample_messages):
    """fork_at=N 截断到第 N 个 user（0-indexed）。"""
    # 第 0 个 user 在 index 1，截断后保留 0..1（2 条）
    result = fork_at_user(sample_messages, fork_at=0)
    assert len(result) == 2
    assert result[-1].role == "user"
    assert result[-1].content == "hello"


def test_fork_at_user_integer_n_second(sample_messages):
    """fork_at=1 截断到第 2 个 user。"""
    # 第 1 个 user（0-indexed）在 index 5
    result = fork_at_user(sample_messages, fork_at=1)
    assert len(result) == 6
    assert result[-1].role == "user"
    assert result[-1].content == "another question"


def test_fork_at_user_with_new_prompt(sample_messages):
    """new_prompt 替换分支点 user 消息 content。"""
    result = fork_at_user(
        sample_messages, fork_at="last", new_prompt="rewritten question"
    )
    assert result[-1].role == "user"
    assert result[-1].content == "rewritten question"


def test_fork_at_user_integer_with_new_prompt(sample_messages):
    """fork_at=N + new_prompt 替换第 N 个 user。"""
    result = fork_at_user(sample_messages, fork_at=0, new_prompt="new q")
    assert result[-1].content == "new q"


def test_fork_at_user_out_of_range_raises(sample_messages):
    """fork_at 超出 user 消息范围抛 ValueError。"""
    # sample_messages 有 2 个 user 消息，索引 [0, 1]
    with pytest.raises(ValueError, match="超出 user 消息范围"):
        fork_at_user(sample_messages, fork_at=5)


def test_fork_at_user_negative_raises(sample_messages):
    """fork_at 负数抛 ValueError。"""
    with pytest.raises(ValueError, match="超出 user 消息范围"):
        fork_at_user(sample_messages, fork_at=-1)


def test_fork_at_user_no_user_raises():
    """无 user 消息抛 ValueError。"""
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="assistant", content="hi"),
    ]
    with pytest.raises(ValueError, match="无 user 消息"):
        fork_at_user(messages, fork_at="last")


def test_fork_at_user_invalid_type_raises(sample_messages):
    """fork_at 非法类型抛 ValueError。"""
    with pytest.raises(ValueError, match="仅支持 'last' 或整数"):
        fork_at_user(sample_messages, fork_at="middle")


def test_fork_at_user_does_not_modify_input(sample_messages):
    """fork_at_user 不修改输入列表。"""
    original_len = len(sample_messages)
    original_last_role = sample_messages[-1].role
    original_last_content = sample_messages[-1].content

    _ = fork_at_user(sample_messages, fork_at="last", new_prompt="changed")

    assert len(sample_messages) == original_len
    assert sample_messages[-1].role == original_last_role
    assert sample_messages[-1].content == original_last_content


def test_fork_at_user_bool_rejected(sample_messages):
    """bool 不应被当作 int（True/False 非法）。"""
    with pytest.raises(ValueError, match="仅支持 'last' 或整数"):
        fork_at_user(sample_messages, fork_at=True)


# ---------------------------------------------------------------------------
# T1.8 check_compatibility
# ---------------------------------------------------------------------------
def test_check_compatibility_strict_tools_mismatch_raises(agent):
    """strict 档 tools_sig 不一致抛 ConversationCompatError。"""
    conv_meta = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "tools_sig": _tools_signature(["search", "calc"]),
        "system_hash": _hash_system(agent.system),
        "cached_tokens": 0,
    }
    # 改变 agent 的工具集
    agent.tools = ["search", "weather"]
    with pytest.raises(ConversationCompatError, match="tools_sig 变更=True"):
        check_compatibility(conv_meta, agent, compat="strict")


def test_check_compatibility_strict_provider_mismatch_raises(agent):
    """strict 档 provider 不一致抛 ConversationCompatError。"""
    conv_meta = {
        "provider": "openai",
        "model": "deepseek-chat",
        "tools_sig": _tools_signature(agent.tools),
        "system_hash": _hash_system(agent.system),
        "cached_tokens": 0,
    }
    with pytest.raises(ConversationCompatError, match="provider/model"):
        check_compatibility(conv_meta, agent, compat="strict")


def test_check_compatibility_strict_model_mismatch_raises(agent):
    """strict 档 model 不一致抛 ConversationCompatError。"""
    conv_meta = {
        "provider": "deepseek",
        "model": "gpt-4o",
        "tools_sig": _tools_signature(agent.tools),
        "system_hash": _hash_system(agent.system),
        "cached_tokens": 0,
    }
    with pytest.raises(ConversationCompatError, match="provider/model"):
        check_compatibility(conv_meta, agent, compat="strict")


def test_check_compatibility_strict_system_mismatch_only_warning(
    agent, caplog
):
    """strict 档 system_hash 不一致仅 warning（不抛异常）。"""
    conv_meta = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "tools_sig": _tools_signature(agent.tools),
        "system_hash": _hash_system("different system"),
        "cached_tokens": 0,
    }
    with caplog.at_level(logging.WARNING, logger="agentkit.core.conversation"):
        check_compatibility(conv_meta, agent, compat="strict")
    # 不抛异常即通过；校验 warning 已记录
    assert any("system" in r.message and "缓存前缀" in r.message for r in caplog.records)


def test_check_compatibility_system_override_silences(agent, caplog):
    """system_override=True 时 system 不一致静默（无 warning）。"""
    conv_meta = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "tools_sig": _tools_signature(agent.tools),
        "system_hash": _hash_system("different system"),
        "cached_tokens": 0,
    }
    with caplog.at_level(logging.WARNING, logger="agentkit.core.conversation"):
        check_compatibility(
            conv_meta, agent, compat="strict", system_override=True
        )
    # 不应有 system 相关 warning
    assert not any("缓存前缀" in r.message for r in caplog.records)


def test_check_compatibility_passthrough_tools_mismatch_only_warning(
    agent, caplog
):
    """passthrough 档 tools_sig 不一致仅 warning。"""
    conv_meta = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "tools_sig": "deadbeef",
        "system_hash": _hash_system(agent.system),
        "cached_tokens": 0,
    }
    agent.tools = ["search", "weather"]
    with caplog.at_level(logging.WARNING, logger="agentkit.core.conversation"):
        # 不抛异常
        check_compatibility(conv_meta, agent, compat="passthrough")
    assert any("passthrough" in r.message for r in caplog.records)


def test_check_compatibility_passthrough_provider_mismatch_only_warning(
    agent, caplog
):
    """passthrough 档 provider 不一致仅 warning。"""
    conv_meta = {
        "provider": "openai",
        "model": "deepseek-chat",
        "tools_sig": _tools_signature(agent.tools),
        "system_hash": _hash_system(agent.system),
        "cached_tokens": 0,
    }
    with caplog.at_level(logging.WARNING, logger="agentkit.core.conversation"):
        check_compatibility(conv_meta, agent, compat="passthrough")
    assert any("passthrough" in r.message for r in caplog.records)


def test_check_compatibility_all_match_no_warning(agent, caplog):
    """全部一致时不抛异常也不 warning。"""
    conv_meta = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "tools_sig": _tools_signature(agent.tools),
        "system_hash": _hash_system(agent.system),
        "cached_tokens": 100,
    }
    with caplog.at_level(logging.WARNING, logger="agentkit.core.conversation"):
        check_compatibility(conv_meta, agent, compat="strict")
    assert len(caplog.records) == 0


# ---------------------------------------------------------------------------
# T1.5 LocalConversationStore
# ---------------------------------------------------------------------------
def test_local_store_save_returns_md5_ref(isolated_store):
    """save 返回 md5 hex ref。"""
    data = b'{"messages":[]}'
    ref = isolated_store.save(data)
    import hashlib

    assert ref == hashlib.md5(data).hexdigest()
    assert len(ref) == 32  # md5 hex


def test_local_store_save_dedup(isolated_store):
    """同内容同 ref（内容寻址去重）。"""
    data = b"same content"
    ref1 = isolated_store.save(data)
    ref2 = isolated_store.save(data)
    assert ref1 == ref2


def test_local_store_load_returns_bytes(isolated_store):
    """load 回填完整字节。"""
    data = b'{"role":"user","content":"hello"}'
    ref = isolated_store.save(data)
    loaded = isolated_store.load(ref)
    assert loaded == data


def test_local_store_load_unicode(isolated_store):
    """中文等多字节内容正确回填。"""
    data = "你好，世界".encode("utf-8")
    ref = isolated_store.save(data)
    assert isolated_store.load(ref) == data


def test_local_store_exists(isolated_store):
    """exists 判断 ref 是否已落盘。"""
    data = b"some data"
    ref = isolated_store.save(data)
    assert isolated_store.exists(ref)
    assert not isolated_store.exists("nonexistent_ref_1234567890")


def test_local_store_atomic_rename_no_tmp_residue(isolated_store):
    """原子 rename 后 .tmp 不残留。"""
    data = b"atomic test"
    ref = isolated_store.save(data)
    conv_dir = isolated_store._conv_dir
    tmp_path = os.path.join(conv_dir, f"{ref}.tmp")
    final_path = os.path.join(conv_dir, ref)
    assert not os.path.exists(tmp_path)
    assert os.path.exists(final_path)


def test_local_store_creates_conversations_subdir(tmp_path):
    """save 自动创建 conversations/ 子目录。"""
    store = LocalConversationStore(base_dir=str(tmp_path / "nested" / "runs"))
    ref = store.save(b"data")
    assert os.path.isdir(os.path.join(tmp_path, "nested", "runs", "conversations"))
    assert store.load(ref) == b"data"


def test_local_store_load_missing_raises(isolated_store):
    """load 不存在的 ref 抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        isolated_store.load("nonexistent_ref_abcdef0123456789")


# ---------------------------------------------------------------------------
# T1.6 全局存储实例注入
# ---------------------------------------------------------------------------
def test_get_conversation_store_lazy_default():
    """未注入时懒加载 LocalConversationStore。"""
    import agentkit.core.conversation as _conv

    original = _conv._global_store
    try:
        _conv._global_store = None
        store = get_conversation_store()
        assert isinstance(store, LocalConversationStore)
        # 再次获取返回同一实例
        assert get_conversation_store() is store
    finally:
        _conv._global_store = original


def test_set_conversation_store_injects_custom():
    """set_conversation_store 注入自定义实现。"""
    import agentkit.core.conversation as _conv

    original = _conv._global_store

    class CustomStore(ConversationStore):
        def save(self, data: bytes) -> str:
            return "custom_ref"

        def load(self, ref: str) -> bytes:
            return b"custom"

        def exists(self, ref: str) -> bool:
            return True

    try:
        custom = CustomStore()
        set_conversation_store(custom)
        assert get_conversation_store() is custom
    finally:
        _conv._global_store = original


# ---------------------------------------------------------------------------
# T1.10 + T1.9 save_conversation + load_and_validate 内联路径
# ---------------------------------------------------------------------------
def test_save_conversation_inline_path(agent, sample_messages):
    """小会话 → 内联存储（_type=conversation）。"""
    ctx = Context()
    save_conversation(ctx, "conv_key", sample_messages, agent)

    raw = ctx.get("conv_key")
    assert raw.get("_type") == CONVERSATION_TYPE
    assert "messages" in raw
    assert "meta" in raw


def test_load_and_validate_inline_restores(agent, sample_messages):
    """内联路径 load_and_validate 正确还原 messages + meta。"""
    ctx = Context()
    save_conversation(ctx, "conv_key", sample_messages, agent)

    messages, meta = load_and_validate(
        ctx, "conv_key", agent, compat="strict"
    )
    assert len(messages) == len(sample_messages)
    for orig, got in zip(sample_messages, messages):
        assert got.role == orig.role
        assert got.content == orig.content
    assert meta["provider"] == "deepseek"


def test_load_and_validate_missing_key_raises(agent):
    """from_key 不存在抛 KeyError。"""
    ctx = Context()
    with pytest.raises(KeyError):
        load_and_validate(ctx, "nonexistent", agent, compat="strict")


def test_load_and_validate_wrong_type_raises(agent):
    """key 存在但非会话类型抛 ConversationTypeError。"""
    ctx = Context()
    ctx.set("plain_var", {"not": "a conversation"})
    with pytest.raises(ConversationTypeError):
        load_and_validate(ctx, "plain_var", agent, compat="strict")


def test_load_and_validate_strict_compat_mismatch_raises(agent):
    """strict 档不一致抛 ConversationCompatError。"""
    ctx = Context()
    save_conversation(ctx, "conv_key", [], agent)
    # 改变 agent 的工具集
    agent.tools = ["different_tool"]
    with pytest.raises(ConversationCompatError):
        load_and_validate(ctx, "conv_key", agent, compat="strict")


def test_load_and_validate_passthrough_compat_mismatch_ok(agent, caplog):
    """passthrough 档不一致不抛异常。"""
    ctx = Context()
    save_conversation(ctx, "conv_key", [], agent)
    agent.tools = ["different_tool"]
    with caplog.at_level(logging.WARNING, logger="agentkit.core.conversation"):
        messages, meta = load_and_validate(
            ctx, "conv_key", agent, compat="passthrough"
        )
    assert messages == []


# ---------------------------------------------------------------------------
# T1.10 + T1.9 save_conversation + load_and_validate 旁路路径
# ---------------------------------------------------------------------------
def test_save_conversation_bypass_path(
    agent, large_messages, isolated_store, small_threshold
):
    """大会话（> 阈值）→ 旁路存储（_type=conversation_ref）。"""
    ctx = Context()
    save_conversation(ctx, "conv_key", large_messages, agent)

    raw = ctx.get("conv_key")
    assert raw.get("_type") == CONVERSATION_REF_TYPE
    assert "ref" in raw
    assert raw.get("store") == "local"
    assert raw.get("size", 0) > 5000
    assert "meta" in raw


def test_load_and_validate_bypass_restores(
    agent, large_messages, isolated_store, small_threshold
):
    """旁路路径 load_and_validate 从 store 回填完整 messages。"""
    ctx = Context()
    save_conversation(ctx, "conv_key", large_messages, agent)

    messages, meta = load_and_validate(
        ctx, "conv_key", agent, compat="strict"
    )
    assert len(messages) == len(large_messages)
    for orig, got in zip(large_messages, messages):
        assert got.role == orig.role
        assert got.content == orig.content
    assert meta["provider"] == "deepseek"


def test_bypass_snapshot_restore_roundtrip(
    agent, large_messages, isolated_store, small_threshold
):
    """旁路存储 snapshot/restore 不丢历史：ref 是小对象，restore 后可回填。"""
    ctx = Context()
    save_conversation(ctx, "conv_key", large_messages, agent)

    # snapshot 前确认是旁路 ref
    raw_before = ctx.get("conv_key")
    assert raw_before.get("_type") == CONVERSATION_REF_TYPE

    # snapshot + restore
    snap = ctx.snapshot()
    restored_ctx = Context.restore(snap)

    # restore 后仍是 conversation_ref（小对象完整保留，非 LargeRef 摘要）
    raw_after = restored_ctx.get("conv_key")
    assert raw_after.get("_type") == CONVERSATION_REF_TYPE
    assert raw_after.get("ref") == raw_before.get("ref")

    # load_and_validate 仍能从 store 回填完整 messages
    messages, meta = load_and_validate(
        restored_ctx, "conv_key", agent, compat="strict"
    )
    assert len(messages) == len(large_messages)


def test_inline_snapshot_restore_roundtrip(agent, sample_messages):
    """内联存储 snapshot/restore 也能还原（对照测试）。"""
    ctx = Context()
    save_conversation(ctx, "conv_key", sample_messages, agent)

    snap = ctx.snapshot()
    restored_ctx = Context.restore(snap)

    messages, meta = load_and_validate(
        restored_ctx, "conv_key", agent, compat="strict"
    )
    assert len(messages) == len(sample_messages)


# ---------------------------------------------------------------------------
# T1.10 cached_tokens 累计
# ---------------------------------------------------------------------------
def test_save_conversation_writes_cached_tokens(agent, sample_messages):
    """save_conversation 写入 meta.cached_tokens。"""
    ctx = Context()
    save_conversation(
        ctx, "conv_key", sample_messages, agent, cached_tokens_total=42
    )

    raw = ctx.get("conv_key")
    if raw.get("_type") == CONVERSATION_REF_TYPE:
        meta = dict(raw.get("meta", {}))
    else:
        meta = dict(raw.get("meta", {}))
    assert meta["cached_tokens"] == 42


def test_save_conversation_cached_tokens_default_zero(agent, sample_messages):
    """cached_tokens_total 默认 0。"""
    ctx = Context()
    save_conversation(ctx, "conv_key", sample_messages, agent)

    raw = ctx.get("conv_key")
    meta = dict(raw.get("meta", {}))
    assert meta["cached_tokens"] == 0


def test_cached_tokens_accumulates_across_rounds(agent, sample_messages):
    """多轮累计 cached_tokens 正确（调用方累计后传入）。"""
    ctx = Context()

    # 第一轮：累计 10
    save_conversation(
        ctx, "conv_key", sample_messages, agent, cached_tokens_total=10
    )
    _, meta1 = load_and_validate(ctx, "conv_key", agent, compat="strict")
    assert meta1["cached_tokens"] == 10

    # 第二轮：调用方累计到 35（10 + 25）
    more_messages = sample_messages + [
        LLMMessage(role="user", content="round 2"),
        LLMMessage(role="assistant", content="answer 2"),
    ]
    save_conversation(
        ctx, "conv_key", more_messages, agent, cached_tokens_total=35
    )
    _, meta2 = load_and_validate(ctx, "conv_key", agent, compat="strict")
    assert meta2["cached_tokens"] == 35


def test_cached_tokens_preserved_in_bypass(
    agent, large_messages, isolated_store, small_threshold
):
    """旁路路径 meta 内联，cached_tokens 不丢。"""
    ctx = Context()
    save_conversation(
        ctx, "conv_key", large_messages, agent, cached_tokens_total=99
    )

    raw = ctx.get("conv_key")
    assert raw.get("_type") == CONVERSATION_REF_TYPE
    meta = dict(raw.get("meta", {}))
    assert meta["cached_tokens"] == 99

    # load_and_validate 回填后 meta 仍含 cached_tokens
    _, meta_loaded = load_and_validate(
        ctx, "conv_key", agent, compat="strict"
    )
    assert meta_loaded["cached_tokens"] == 99


# ---------------------------------------------------------------------------
# T1.11 MessageNormalizer
# ---------------------------------------------------------------------------
def test_message_normalizer_default_identity(sample_messages, agent):
    """默认 normalize 原样返回（identity）。"""
    normalizer = MessageNormalizer()
    conv_meta = {"provider": "deepseek"}
    result = normalizer.normalize(sample_messages, conv_meta, agent)
    assert result is sample_messages or result == sample_messages


def test_message_normalizer_subclass_can_override(agent):
    """子类可覆盖 normalize 实现自定义适配。"""
    from agentkit.llm.base import LLMMessage

    class UpperCaseNormalizer(MessageNormalizer):
        def normalize(self, messages, conv_meta, agent):
            return [
                LLMMessage(
                    role=m.role,
                    content=m.content.upper() if isinstance(m.content, str) else m.content,
                    tool_calls=m.tool_calls,
                    tool_call_id=m.tool_call_id,
                    name=m.name,
                    reasoning_content=m.reasoning_content,
                )
                for m in messages
            ]

    messages = [LLMMessage(role="user", content="hello")]
    normalizer = UpperCaseNormalizer()
    result = normalizer.normalize(messages, {}, agent)
    assert result[0].content == "HELLO"
