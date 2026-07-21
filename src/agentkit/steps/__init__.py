"""steps —— Step 类型子包。

导入本包即自动注册所有内置 Step 类型到全局 ``StepRegistry``:

    - LLMStep (type="llm")
    - ToolStep (type="tool")
    - SkillStep (type="skill")
    - ParallelStep (type="parallel")
    - ConditionStep (type="condition")
    - LoopStep (type="loop")
"""

# 导入各 Step 模块,触发 @register_step 装饰器注册到全局 StepRegistry
from agentkit.steps.base import BaseStep, StepTrace, get_step_type, register_step
from agentkit.steps.llm_step import LLMStep
from agentkit.steps.tool_step import ToolStep
from agentkit.steps.skill_step import SkillStep
from agentkit.steps.parallel_step import ParallelStep, ParallelError
from agentkit.steps.condition_step import ConditionStep
from agentkit.steps.loop_step import LoopStep, LoopMaxReachedError

__all__ = [
    "BaseStep",
    "StepTrace",
    "get_step_type",
    "register_step",
    "LLMStep",
    "ToolStep",
    "SkillStep",
    "ParallelStep",
    "ParallelError",
    "ConditionStep",
    "LoopStep",
    "LoopMaxReachedError",
]
