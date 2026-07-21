"""skill.merger —— 多 Skill 配置合并。

本模块负责把 Agent 同时引用的多个 Skill 配置合并为统一上下文，供
``SkillStep`` / ``LLMStep`` 构造 Agent 时使用。

合并规则（来自 spec）：
    - ``system`` prompt 按声明顺序拼接（Skill 的在前，Agent 自带 system
      在最前，由 ``apply_skills_to_agent`` 处理）。
    - ``tools`` 取并集（保序去重，后出现的不重复添加）。
    - ``output_model`` 若冲突则取第一个非 None 的，并记录冲突；Agent 显式
      声明优先原则由 ``apply_skills_to_agent`` 处理。
    - ``max_tool_iterations`` 取最大值。
    - ``prompt_injection_append`` 按声明顺序拼接。
    - ``requires_mcp`` 取并集（保序去重）。

设计原则：
    - 高度模块化：仅依赖 ``skill.registry`` + ``core.agent`` + 标准库，
      不依赖其他 agentkit 子模块。
    - 不可变：合并与应用均产生新对象，不修改输入。
    - 规则清晰：system 顺序拼接 / tools 并集 / output_model Agent 优先。

公开 API：
    - MergedSkillConfig:        多 Skill 合并后的配置数据类
    - merge_skills:             合并多个 Skill 配置
    - apply_skills_to_agent:    把合并结果应用到 AgentConfig（返回新实例）
    - SkillMergeError:          Skill 合并错误
"""

from __future__ import annotations

import dataclasses
import warnings
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from agentkit.core.agent import AgentConfig
from agentkit.skill.registry import SkillManifest, get_skill, has_skill


__all__ = [
    "MergedSkillConfig",
    "merge_skills",
    "apply_skills_to_agent",
    "SkillMergeError",
]


# ---------------------------------------------------------------------------
# SkillMergeError —— Skill 合并错误
# ---------------------------------------------------------------------------
class SkillMergeError(Exception):
    """Skill 合并错误（引用了不存在的 skill 等）。"""


# ---------------------------------------------------------------------------
# MergedSkillConfig —— 多 Skill 合并后的配置
# ---------------------------------------------------------------------------
@dataclass
class MergedSkillConfig:
    """多个 Skill 合并后的配置（供 SkillStep / LLMStep 构造 Agent 用）。

    Attributes:
        system_prompt:            按 Skill 声明顺序拼接的 system 提示词
                                  （非空片段以 ``\\n\\n`` 分隔）。
        tools:                    并集（保序去重）。
        output_model:             合并后的输出契约；取第一个非 None 的。
        max_tool_iterations:      取所有 Skill 中的最大值。
        prompt_injection_append:  按声明顺序拼接的追加注入内容。
        requires_mcp:             并集（保序去重）。
        skills_used:              记录实际使用了哪些 skill（按声明顺序）。
        output_model_conflicts:   记录与首个非 None output_model 冲突的 skill 名。
    """

    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)
    output_model: type[BaseModel] | None = None
    max_tool_iterations: int = 0
    prompt_injection_append: str = ""
    requires_mcp: list[str] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)
    output_model_conflicts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _union_preserve_order(*lists: list[str]) -> list[str]:
    """把多个 list 合并去重，保持首次出现顺序。

    后出现的重复元素不再添加，从而实现「保序去重」。

    Args:
        *lists: 待合并的多个 list。

    Returns:
        list[str]: 合并去重后的新 list（不修改任何输入）。
    """
    seen: set[str] = set()
    result: list[str] = []
    for lst in lists:
        for item in lst:
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _join_nonempty(parts: list[str], sep: str = "\n\n") -> str:
    """把非空字符串片段按分隔符拼接。

    Args:
        parts: 待拼接的字符串片段列表。
        sep:   分隔符，默认 ``\\n\\n``。

    Returns:
        str: 拼接结果；全为空时返回空串。
    """
    return sep.join(p for p in parts if p)


# ---------------------------------------------------------------------------
# merge_skills —— 合并多个 Skill 配置
# ---------------------------------------------------------------------------
def merge_skills(skill_names: list[str]) -> MergedSkillConfig:
    """合并多个 Skill 配置。

    步骤：
        1. 按 ``skill_names`` 顺序逐个 ``get_skill``（不存在抛
           ``SkillMergeError``）。
        2. ``system_prompt``：拼接每个 ``manifest.system_prompt``（非空的，
           用 ``\\n\\n`` 分隔）。
        3. ``tools``：并集保序（后出现的不重复添加）。
        4. ``output_model``：取第一个非 None 的；若多个非 None 且不同，
           记录冲突但仍取第一个（Agent 显式优先原则由
           ``apply_skills_to_agent`` 处理）。
        5. ``max_tool_iterations``：取最大值。
        6. ``prompt_injection_append``：按顺序拼接（非空，``\\n\\n`` 分隔）。
        7. ``requires_mcp``：并集保序。
        8. ``skills_used`` = ``skill_names``。

    Args:
        skill_names: Skill 名称列表（按声明顺序）。

    Returns:
        MergedSkillConfig: 合并后的配置。空列表返回空配置。

    Raises:
        SkillMergeError: 引用了未注册的 skill。
    """
    merged = MergedSkillConfig()
    # 记录实际使用的 skill（拷贝避免外部修改影响）
    merged.skills_used = list(skill_names)

    system_parts: list[str] = []
    injection_parts: list[str] = []
    tools_per_skill: list[list[str]] = []
    mcp_per_skill: list[list[str]] = []

    first_output_model: type[BaseModel] | None = None
    conflict_names: list[str] = []
    max_iter = 0

    for name in skill_names:
        # 1. 检查存在性；不存在则抛 SkillMergeError（包装 registry 的 KeyError 语义）
        if not has_skill(name):
            raise SkillMergeError(f"Skill {name!r} 未注册，无法合并")
        manifest: SkillManifest = get_skill(name)

        # 2. system_prompt：收集非空片段
        if manifest.system_prompt:
            system_parts.append(manifest.system_prompt)

        # 4. output_model：取第一个非 None；后续不同的非 None 记录冲突
        #    （类型对象用 is 判同，类是单例；冲突时不覆盖首个）
        if manifest.output_model is not None:
            if first_output_model is None:
                first_output_model = manifest.output_model
            elif manifest.output_model is not first_output_model:
                conflict_names.append(name)

        # 5. max_tool_iterations：取最大值
        if manifest.max_tool_iterations > max_iter:
            max_iter = manifest.max_tool_iterations

        # 6. prompt_injection_append：收集非空片段
        if manifest.prompt_injection_append:
            injection_parts.append(manifest.prompt_injection_append)

        # 3 / 7. tools / requires_mcp：先收集，稍后并集保序
        tools_per_skill.append(list(manifest.tools))
        mcp_per_skill.append(list(manifest.requires_mcp))

    # 装配合并结果
    merged.system_prompt = _join_nonempty(system_parts)
    merged.tools = _union_preserve_order(*tools_per_skill)
    merged.output_model = first_output_model
    merged.max_tool_iterations = max_iter
    merged.prompt_injection_append = _join_nonempty(injection_parts)
    merged.requires_mcp = _union_preserve_order(*mcp_per_skill)
    merged.output_model_conflicts = conflict_names

    # 冲突告警：告知取了首个，Agent 显式优先由 apply_skills_to_agent 处理
    if conflict_names:
        model_name = (
            first_output_model.__name__ if first_output_model is not None else None
        )
        warnings.warn(
            f"output_model 冲突：已采用首个非 None 的 {model_name}，"
            f"冲突 skill: {conflict_names}。"
            f"Agent 显式声明优先原则由 apply_skills_to_agent 处理。",
            stacklevel=2,
        )

    return merged


# ---------------------------------------------------------------------------
# apply_skills_to_agent —— 把合并结果应用到 AgentConfig
# ---------------------------------------------------------------------------
def apply_skills_to_agent(
    agent: AgentConfig,
    skill_names: list[str] | None = None,
) -> AgentConfig:
    """把 Skill 合并结果应用到 AgentConfig，返回新的 AgentConfig。

    遵循不可变理念：用 ``dataclasses.replace`` 创建副本，不修改原对象。

    步骤：
        1. 用 ``dataclasses.replace`` 创建副本。
        2. ``skill_names`` 为 None 时用 ``agent.skills``。
        3. ``merged = merge_skills(skill_names)``。
        4. ``system``：``agent.system`` + merged.system_prompt（若非空）；
           prompt_injection_append 追加到 system 末尾（AgentConfig 无此字段）。
        5. ``tools``：``agent.tools`` + merged.tools 的并集保序
           （agent 自带优先）。
        6. ``output_model``：``agent.output_model`` 优先（若 agent 显式声明
           则保留；否则用 merged.output_model）—— 这就是 spec 的
           「Agent 显式声明优先」。
        7. ``max_tool_iterations``：``agent.max_tool_iterations`` 优先
           （非 0 则保留；否则用 merged 的）。
        8. 返回新 AgentConfig。

    Args:
        agent:       原始 AgentConfig 实例（不被修改）。
        skill_names: 待合并的 Skill 名列表；None 时用 ``agent.skills``。

    Returns:
        AgentConfig: 应用合并结果后的新实例（与原 agent 同类型）。

    Raises:
        SkillMergeError: 引用了未注册的 skill（由 merge_skills 抛出）。

    注意:
        不重复加载已加载的 skill（``get_skill`` 从 registry 取，无 IO）。
    """
    # 2. skill_names 缺省回退到 agent.skills
    if skill_names is None:
        skill_names = list(agent.skills)

    # 3. 合并 Skill 配置
    merged = merge_skills(skill_names)

    # 4. system：agent.system 在最前，Skill system_prompt 次之，
    #    prompt_injection_append 追加到末尾
    system_parts: list[str] = []
    if agent.system:
        system_parts.append(agent.system)
    if merged.system_prompt:
        system_parts.append(merged.system_prompt)
    if merged.prompt_injection_append:
        system_parts.append(merged.prompt_injection_append)
    new_system = _join_nonempty(system_parts)

    # 5. tools：agent.tools + merged.tools 并集保序（agent 自带优先）
    new_tools = _union_preserve_order(list(agent.tools), merged.tools)

    # 6. output_model：agent 显式声明绝对优先（spec 的「Agent 显式声明优先」）
    new_output_model: type[BaseModel] | None = (
        agent.output_model if agent.output_model is not None else merged.output_model
    )

    # 7. max_tool_iterations：agent 非零则保留，否则用 merged 的
    new_max_iter = (
        agent.max_tool_iterations
        if agent.max_tool_iterations != 0
        else merged.max_tool_iterations
    )

    # mcp:agent.mcp + Skill 声明的 requires_mcp 并集保序(agent 自带优先)。
    # MCPManager.connect_all 后据此把对应 server 的工具注入 agent.tools。
    new_mcp = _union_preserve_order(list(agent.mcp), merged.requires_mcp)

    # 1 / 8. 用 dataclasses.replace 产生新实例（同类型，保留 output_validator 等方法）
    return dataclasses.replace(
        agent,
        system=new_system,
        tools=new_tools,
        mcp=new_mcp,
        output_model=new_output_model,
        max_tool_iterations=new_max_iter,
    )
