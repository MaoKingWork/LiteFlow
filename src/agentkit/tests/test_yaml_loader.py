"""yaml.loader —— conversation 配置块解析单元测试（T6.4）。

覆盖 tasks.md T6.4 出口标准：
    - 合法配置解析：完整 conversation 块 → LLMStep 实例 conversation_* 属性正确
    - 默认值：不配置 conversation / 仅部分字段 → 默认值正确（向后兼容）
    - 非法 compat 报错：compat 非 strict|passthrough → ValueError
    - 模板 key 透传：含 ``{{var}}`` 的 key 原样保留为字符串（运行期才渲染）

依赖 T3：LLMStep 需已新增 ``conversation_*`` 参数。
T3 未完成时，除 ``test_invalid_compat_*`` 外的用例会以 TypeError 失败（预期行为，
T3 完成后自动通过）。非法 compat 校验在 ``step_cls(...)`` 调用之前触发，不依赖 T3。
"""
from __future__ import annotations

import pytest

from agentkit.yaml.loader import _compile_step


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _llm_step_dict(step_id: str = "s1", **overrides) -> dict:
    """构造最小合法 llm step_dict，可通过 overrides 追加/覆盖字段。"""
    base: dict = {
        "id": step_id,
        "type": "llm",
        "agent": "writer",  # 字符串引用；空 agent_configs 时原样传入 LLMStep
        "prompt": "hi",
        "output": "out",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. 合法配置解析
# ---------------------------------------------------------------------------
def test_full_conversation_block_parsed():
    """完整 conversation 块（mode/key/from/fork_at/compat）解析到 LLMStep 属性。"""
    step_dict = _llm_step_dict(
        conversation={
            "mode": "continue",
            "key": "story_chat",
            "from": "story_chat",
            "fork_at": 2,
            "compat": "passthrough",
        }
    )
    step = _compile_step(step_dict, agent_configs={})

    assert step.conversation_mode == "continue"
    assert step.conversation_key == "story_chat"
    assert step.conversation_from == "story_chat"
    assert step.conversation_fork_at == 2
    assert step.conversation_compat == "passthrough"


def test_full_conversation_block_strict_compat():
    """compat=strict 完整路径解析正确。"""
    step_dict = _llm_step_dict(
        conversation={
            "mode": "start",
            "key": "k1",
            "fork_at": "last",
            "compat": "strict",
        }
    )
    step = _compile_step(step_dict, agent_configs={})

    assert step.conversation_mode == "start"
    assert step.conversation_compat == "strict"
    assert step.conversation_fork_at == "last"


# ---------------------------------------------------------------------------
# 2. 默认值（向后兼容）
# ---------------------------------------------------------------------------
def test_no_conversation_block_defaults():
    """不配置 conversation → mode/key/from=None, compat="strict", fork_at="last"。"""
    step_dict = _llm_step_dict()
    step = _compile_step(step_dict, agent_configs={})

    assert step.conversation_mode is None
    assert step.conversation_key is None
    assert step.conversation_from is None
    assert step.conversation_fork_at == "last"
    assert step.conversation_compat == "strict"


def test_conversation_block_without_compat_defaults_strict():
    """conversation 块不含 compat → 默认 "strict"。"""
    step_dict = _llm_step_dict(
        conversation={"mode": "start", "key": "k1"}
    )
    step = _compile_step(step_dict, agent_configs={})

    assert step.conversation_compat == "strict"


def test_conversation_block_without_fork_at_defaults_last():
    """conversation 块不含 fork_at → 默认 "last"。"""
    step_dict = _llm_step_dict(
        conversation={"mode": "fork", "key": "k1"}
    )
    step = _compile_step(step_dict, agent_configs={})

    assert step.conversation_fork_at == "last"


def test_conversation_block_without_from_defaults_none():
    """conversation 块不含 from → None（运行期由 LLMStep 回落到 key）。"""
    step_dict = _llm_step_dict(
        conversation={"mode": "continue", "key": "k1"}
    )
    step = _compile_step(step_dict, agent_configs={})

    assert step.conversation_from is None


# ---------------------------------------------------------------------------
# 3. 非法 compat 报错（在 LLMStep 实例化之前触发，不依赖 T3）
# ---------------------------------------------------------------------------
def test_invalid_compat_raises_value_error():
    """compat 非 strict|passthrough → 抛 ValueError。"""
    step_dict = _llm_step_dict(
        conversation={"mode": "continue", "key": "k1", "compat": "invalid"}
    )
    with pytest.raises(ValueError, match="conversation.compat 仅支持"):
        _compile_step(step_dict, agent_configs={})


def test_invalid_compat_error_mentions_step_id():
    """ValueError 文案包含 step id，便于定位。"""
    step_dict = _llm_step_dict(
        id="my_step",
        conversation={"mode": "continue", "compat": "loose"},
    )
    with pytest.raises(ValueError, match="my_step"):
        _compile_step(step_dict, agent_configs={})


def test_invalid_compat_raises_before_instantiation():
    """非法 compat 在 LLMStep 构造之前抛出（不依赖 T3 的 conversation_* 参数）。

    以 agent_configs 含一个具名 AgentConfig 为例，确保即使 agent 解析正常，
    compat 校验仍优先于 step_cls(...) 调用生效。
    """
    from agentkit.core.agent import AgentConfig

    agent = AgentConfig(name="writer", model="gpt-4o-mini")
    step_dict = _llm_step_dict(
        agent="writer",
        conversation={"mode": "start", "compat": "bogus"},
    )
    with pytest.raises(ValueError):
        _compile_step(step_dict, agent_configs={"writer": agent})


# ---------------------------------------------------------------------------
# 4. 模板 key 透传
# ---------------------------------------------------------------------------
def test_template_key_passed_through_verbatim():
    """含 {{var}} 的 key 原样保留为字符串，运行期才渲染。"""
    step_dict = _llm_step_dict(
        conversation={
            "mode": "continue",
            "key": "chat_{{provider}}",
            "from": "chat_{{provider}}",
        }
    )
    step = _compile_step(step_dict, agent_configs={})

    assert step.conversation_key == "chat_{{provider}}"
    assert step.conversation_from == "chat_{{provider}}"


def test_template_key_with_fork_at_int():
    """模板 key + 整数 fork_at 组合解析正确。"""
    step_dict = _llm_step_dict(
        conversation={
            "mode": "fork",
            "key": "chat_{{provider}}",
            "fork_at": 3,
            "compat": "passthrough",
        }
    )
    step = _compile_step(step_dict, agent_configs={})

    assert step.conversation_key == "chat_{{provider}}"
    assert step.conversation_fork_at == 3
    assert step.conversation_compat == "passthrough"
