"""SkillStep:Skill 加载 + 委托 LLMStep + prompt_injection_append + 废弃参数警告。"""
from __future__ import annotations

import warnings

import pytest

from agentkit.core.context import Context
from agentkit.llm.mock import MockClient
from agentkit.skill.registry import SkillManifest, register_skill
from agentkit.steps.skill_step import SkillStep


def _register_test_skill(name: str = "test_skill", **kw) -> SkillManifest:
    defaults = dict(
        name=name,
        system_prompt="你是测试助手",
        description="test skill",
    )
    defaults.update(kw)
    manifest = SkillManifest(**defaults)
    register_skill(manifest)
    return manifest


async def test_skill_step_basic():
    """加载 Skill → 构造 AgentConfig → 委托 LLMStep → 写入 output。"""
    _register_test_skill()
    mc = MockClient(script=[{"content": "skill result"}])
    step = SkillStep(
        id="sk1",
        skill="test_skill",
        prompt="{{question}}",
        output="answer",
    )
    step.bind_llm_client(mc)

    ctx = Context()
    ctx.set("question", "hi")
    await step.execute(ctx)

    assert ctx.get("answer") == "skill result"
    assert mc.call_count == 1


async def test_skill_step_prompt_injection_append():
    """prompt_injection_append 追加到 system prompt 末尾。"""
    _register_test_skill(prompt_injection_append="额外指令:回答简短")
    mc = MockClient(script=[{"content": "ok"}])
    step = SkillStep(
        id="sk1",
        skill="test_skill",
        prompt="hi",
        output="answer",
    )
    step.bind_llm_client(mc)

    ctx = Context()
    await step.execute(ctx)

    # 验证 system prompt 包含追加内容
    messages = mc.history[0]["messages"]
    system_msg = [m for m in messages if m.role == "system"][0]
    assert "你是测试助手" in system_msg.content
    assert "额外指令:回答简短" in system_msg.content


async def test_skill_step_input_deprecated_warning():
    """input 参数已废弃,传入时触发 DeprecationWarning。"""
    _register_test_skill()
    mc = MockClient(script=[{"content": "ok"}])
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        step = SkillStep(
            id="sk1",
            skill="test_skill",
            input="hi",  # 废弃参数
            output="answer",
        )
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "input" in str(w[0].message)

    step.bind_llm_client(mc)
    ctx = Context()
    await step.execute(ctx)
    assert ctx.get("answer") == "ok"


async def test_skill_step_prompt_and_input_mutually_exclusive():
    """prompt 与 input 不可同时指定。"""
    _register_test_skill()
    with pytest.raises(ValueError, match="不可同时指定"):
        SkillStep(
            id="sk1",
            skill="test_skill",
            prompt="a",
            input="b",
        )
