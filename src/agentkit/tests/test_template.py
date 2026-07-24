"""core.template 的 has() 表达式函数测试。

覆盖 eval_expression 新增的 ``has('key')`` 预处理:
- 存在/缺失 key 返回 True/False
- 参数含 ``{{var}}`` 模板时先渲染再判断
- 与其他表达式组合(布尔/比较)
- 缺失模板变量抛 KeyError(配置错误)
"""

from __future__ import annotations

import pytest

from agentkit.core.context import Context
from agentkit.core.template import eval_expression


@pytest.fixture
def ctx() -> Context:
    """构造带 chat_v1 / chat_openai / provider / count 的 Context。"""
    c = Context()
    c.set("chat_v1", {"model": "gpt"})
    c.set("provider", "openai")
    c.set("chat_openai", {"model": "gpt-4"})
    c.set("count", 5)
    return c


def test_has_exists(ctx: Context) -> None:
    """has('chat_v1') 存在时返回 True。"""
    assert eval_expression("has('chat_v1')", ctx) is True


def test_has_missing(ctx: Context) -> None:
    """has('chat_nope') 不存在时返回 False(不抛错)。"""
    assert eval_expression("has('chat_nope')", ctx) is False


def test_has_with_template_arg(ctx: Context) -> None:
    """has('chat_{{provider}}') 模板参数渲染后判断存在性。

    provider=openai → 渲染为 chat_openai → 存在 → True。
    """
    assert eval_expression("has('chat_{{provider}}')", ctx) is True


def test_has_with_template_arg_missing_key(ctx: Context) -> None:
    """has('chat_{{provider}}') 渲染后的 key 不存在时返回 False。

    chat_openai 存在,但 chat_other 不存在;用 missing_provider 指向它。
    """
    ctx.set("missing_provider", "other")
    assert eval_expression("has('chat_{{missing_provider}}')", ctx) is False


def test_has_missing_template_var_raises(ctx: Context) -> None:
    """has('chat_{{missing}}') 缺失模板变量应抛 KeyError(配置错误,不吞掉)。"""
    with pytest.raises(KeyError):
        eval_expression("has('chat_{{missing}}')", ctx)


def test_has_combined_with_comparison(ctx: Context) -> None:
    """has('chat_v1') and {{count}} > 3 组合表达式。"""
    # chat_v1 存在 且 count=5 > 3 → True
    assert eval_expression("has('chat_v1') and {{count}} > 3", ctx) is True
    # chat_nope 不存在 → 短路返回 False
    assert eval_expression("has('chat_nope') and {{count}} > 3", ctx) is False


def test_has_combined_with_not(ctx: Context) -> None:
    """not has('chat_nope') → not False → True。"""
    assert eval_expression("not has('chat_nope')", ctx) is True


def test_has_double_quotes(ctx: Context) -> None:
    """has("chat_v1") 双引号也能解析。"""
    assert eval_expression('has("chat_v1")', ctx) is True


def test_has_with_whitespace(ctx: Context) -> None:
    """has( 'chat_v1' ) 允许括号内空白。"""
    assert eval_expression("has( 'chat_v1' )", ctx) is True


def test_has_or_other(ctx: Context) -> None:
    """has('chat_nope') or {{count}} > 3 → False or True → True。"""
    assert eval_expression("has('chat_nope') or {{count}} > 3", ctx) is True
