"""BaseStep.iter_child_steps + walk_all_steps:子 Step 遍历契约。

覆盖:
    - 单层:LoopStep 含一个 body → walk_all_steps 返回 [loop, body]
    - 嵌套:Parallel 含 Loop,Loop 含 body → [parallel, loop, body]
    - Condition then/else → [condition, then_step, else_step]
    - 叶子 Step(LLMStep / 默认 BaseStep 子类)→ iter_child_steps 返回 []
    - 空列表 → walk_all_steps 返回 []
"""
from __future__ import annotations

from agentkit.steps.base import walk_all_steps
from agentkit.steps.condition_step import ConditionStep
from agentkit.steps.llm_step import LLMStep
from agentkit.steps.loop_step import LoopStep
from agentkit.steps.parallel_step import ParallelStep
from agentkit.tests.conftest import SetterStep


# ---------------------------------------------------------------------------
# 单层:LoopStep 含 body
# ---------------------------------------------------------------------------
def test_loop_single_body_returns_loop_then_body():
    """walk_all_steps([loop]) → [loop, body](深度优先,自身在前)。"""
    body = SetterStep(id="body", key="k", value="v")
    loop = LoopStep(id="loop", until="{{done}}", step=body)

    result = walk_all_steps([loop])

    assert result == [loop, body]
    # iter_child_steps 直接返回 body
    assert loop.iter_child_steps() == [body]


def test_loop_without_body_returns_empty_children():
    """未配置 body 的 LoopStep → iter_child_steps 返回空列表。"""
    loop = LoopStep(id="loop", until="{{done}}")
    assert loop.iter_child_steps() == []


# ---------------------------------------------------------------------------
# 嵌套:Parallel 含 Loop,Loop 含 body
# ---------------------------------------------------------------------------
def test_nested_parallel_loop_body_depth_first():
    """walk_all_steps([parallel]) → [parallel, loop, body](深度优先)。

    Parallel 分支为 [loop],loop 的子为 body,遍历顺序应为先 parallel,
    再递归进入 loop,再递归进入 body。
    """
    body = SetterStep(id="body", key="k", value="v")
    loop = LoopStep(id="loop", until="{{done}}", step=body)
    parallel = ParallelStep(id="parallel", branches=[loop])

    result = walk_all_steps([parallel])

    assert result == [parallel, loop, body]
    # 分层断言
    assert parallel.iter_child_steps() == [loop]
    assert loop.iter_child_steps() == [body]
    assert body.iter_child_steps() == []


# ---------------------------------------------------------------------------
# Condition then/else
# ---------------------------------------------------------------------------
def test_condition_then_else_children():
    """walk_all_steps([condition]) → [condition, then_step, else_step]。

    iter_child_steps 返回 then + else 拼接(then 在前)。
    """
    then_step = SetterStep(id="t", key="tk", value="tv")
    else_step = SetterStep(id="e", key="ek", value="ev")
    cond = ConditionStep(
        id="cond",
        when="{{flag}}",
        then_steps=[then_step],
        else_steps=[else_step],
    )

    result = walk_all_steps([cond])

    assert result == [cond, then_step, else_step]
    # iter_child_steps 顺序:then 在前,else 在后
    assert cond.iter_child_steps() == [then_step, else_step]


def test_condition_only_then_children():
    """只有 then 分支的 Condition → iter_child_steps 仅返回 then。"""
    then_step = SetterStep(id="t", key="tk", value="tv")
    cond = ConditionStep(id="cond", when="{{flag}}", then_steps=[then_step])

    assert cond.iter_child_steps() == [then_step]
    assert walk_all_steps([cond]) == [cond, then_step]


# ---------------------------------------------------------------------------
# 叶子 Step:LLMStep / 默认 BaseStep 子类
# ---------------------------------------------------------------------------
def test_llm_step_is_leaf():
    """LLMStep 是叶子 → iter_child_steps 返回空列表。"""
    llm = LLMStep(id="leaf", prompt="hi", output="out")
    assert llm.iter_child_steps() == []


def test_setter_step_is_leaf():
    """默认 BaseStep 子类(未重写)→ iter_child_steps 返回空列表。"""
    setter = SetterStep(id="s", key="k", value="v")
    assert setter.iter_child_steps() == []


# ---------------------------------------------------------------------------
# 边界:空列表
# ---------------------------------------------------------------------------
def test_walk_all_steps_empty():
    """walk_all_steps([]) → []。"""
    assert walk_all_steps([]) == []


def test_walk_all_steps_returns_new_list():
    """walk_all_steps 返回新列表,不修改输入容器。"""
    body = SetterStep(id="body", key="k", value="v")
    loop = LoopStep(id="loop", until="{{done}}", step=body)
    src = [loop]

    result = walk_all_steps(src)

    assert result == [loop, body]
    # 输入列表不应被修改
    assert src == [loop]
    assert len(src) == 1
