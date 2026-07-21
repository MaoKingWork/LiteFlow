"""yaml.validator —— YAML 工作流静态校验器。

本模块对 YAML 工作流定义进行静态校验,在编译为 SDK 对象前发现配置错误,
提供清晰的错误报告。

校验项目:
    1. 顶层必填字段(``name`` / ``steps``)。
    2. Step type 已注册。
    3. 同级 Step id 唯一。
    4. LLMStep 的 agent 引用存在于 ``agents`` 段。
    5. ToolStep 的 tool 引用存在于 ``ToolRegistry``。
    6. SkillStep 的 skill 引用存在于 ``SkillRegistry``。
    7. ConditionStep 必须有 ``when``。
    8. LoopStep 必须有 ``iter`` 或 ``until`` 之一。
    9. ParallelStep 的 branches output key 唯一。

设计原则:
    - 高度模块化:仅依赖 ``steps.base`` / ``tools.base`` / ``skill.registry``
      的注册表查询,不构造对象。
    - 收集所有错误而非首个:返回完整错误列表,便于一次性修正。
    - 类型注解完整,中文 docstring。

公开 API:
    - validate_workflow:    校验 YAML config,返回错误列表
    - ValidationError:      校验错误数据类
    - ValidationReport:     校验报告
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentkit.steps.base import _GLOBAL_STEP_REGISTRY
from agentkit.tools.base import list_tools
from agentkit.skill.registry import list_skills

__all__ = ["ValidationError", "ValidationReport", "validate_workflow"]


@dataclass
class ValidationError:
    """单个校验错误。

    Attributes:
        path:    错误所在路径(如 ``steps[2]`` / ``steps[0].then[1]``)。
        message: 错误描述。
    """

    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.path}] {self.message}"


@dataclass
class ValidationReport:
    """校验报告。

    Attributes:
        errors:   错误列表。
        warnings: 警告列表(不阻止执行)。
    """

    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """是否通过校验(无错误)。"""
        return len(self.errors) == 0

    def __str__(self) -> str:
        lines: list[str] = []
        if self.errors:
            lines.append(f"错误 ({len(self.errors)}):")
            for e in self.errors:
                lines.append(f"  {e}")
        if self.warnings:
            lines.append(f"警告 ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"  {w}")
        if not lines:
            lines.append("校验通过,无错误无警告。")
        return "\n".join(lines)


def validate_workflow(config: dict) -> ValidationReport:
    """校验 YAML 工作流配置。

    Args:
        config: YAML 解析后的 dict。

    Returns:
        ValidationReport: 校验报告(含所有错误与警告)。
    """
    report = ValidationReport()

    # 1. 顶层必填字段
    if not config.get("name"):
        report.errors.append(ValidationError("[root]", "缺少必填字段 name"))
    if not config.get("steps"):
        report.errors.append(ValidationError("[root]", "缺少必填字段 steps 或为空"))

    # 2. 收集 agent 名
    agent_names = set()
    for agent_dict in config.get("agents", []):
        name = agent_dict.get("name", "")
        if name:
            agent_names.add(name)

    # 3. 校验 steps
    steps = config.get("steps", [])
    if isinstance(steps, list):
        _validate_steps(steps, "steps", agent_names, report)

    return report


def _validate_steps(
    steps: list[dict],
    path_prefix: str,
    agent_names: set[str],
    report: ValidationReport,
) -> None:
    """递归校验 Step 列表。

    Args:
        steps:        Step dict 列表。
        path_prefix:  路径前缀(如 ``steps`` / ``steps[0].then``)。
        agent_names:  已声明的 agent 名集合。
        report:       校验报告。
    """
    seen_ids: set[str] = set()
    seen_outputs: set[str] = set()
    registered_tools = set(list_tools())
    registered_skills = set(list_skills())
    registered_types = set(_GLOBAL_STEP_REGISTRY.list())

    for i, step_dict in enumerate(steps):
        if not isinstance(step_dict, dict):
            report.errors.append(
                ValidationError(
                    f"{path_prefix}[{i}]",
                    "Step 定义应为 dict",
                )
            )
            continue

        path = f"{path_prefix}[{i}]"
        step_type = step_dict.get("type", "")
        step_id = step_dict.get("id", "")

        # 3a. type 必填且已注册
        if not step_type:
            report.errors.append(
                ValidationError(path, "缺少 type 字段")
            )
            continue
        if step_type not in registered_types:
            report.errors.append(
                ValidationError(
                    path,
                    f"未注册的 Step type: {step_type!r}。"
                    f"已注册: {sorted(registered_types)}",
                )
            )

        # 3b. id 唯一性
        if step_id:
            if step_id in seen_ids:
                report.errors.append(
                    ValidationError(path, f"重复的 Step id: {step_id!r}")
                )
            else:
                seen_ids.add(step_id)

        # 3c. output 唯一性(同级)
        output = step_dict.get("output")
        if output:
            if output in seen_outputs:
                report.errors.append(
                    ValidationError(path, f"同级 output key 重复: {output!r}")
                )
            else:
                seen_outputs.add(output)

        # 3d. 按类型校验
        if step_type == "llm":
            agent_ref = step_dict.get("agent", "")
            if isinstance(agent_ref, str) and agent_ref and agent_names:
                if agent_ref not in agent_names:
                    report.errors.append(
                        ValidationError(
                            path,
                            f"agent 引用 {agent_ref!r} 未在 agents 段声明",
                        )
                    )
            # output_format 取值合法性
            output_format = step_dict.get("output_format", "text")
            if output_format not in ("text", "json"):
                report.errors.append(
                    ValidationError(
                        path,
                        f"output_format 必须为 'text' 或 'json',"
                        f"当前: {output_format!r}",
                    )
                )
            # stream 必须为 bool
            stream = step_dict.get("stream", False)
            if not isinstance(stream, bool):
                report.errors.append(
                    ValidationError(
                        path,
                        f"stream 必须为布尔值(true/false),当前: {stream!r}",
                    )
                )
            _check_deprecated_prompt_field(step_dict, path, report)

        elif step_type == "tool":
            tool_name = step_dict.get("tool", "")
            if tool_name and registered_tools:
                if tool_name not in registered_tools:
                    report.warnings.append(
                        ValidationError(
                            path,
                            f"tool 引用 {tool_name!r} 未在 ToolRegistry 中注册"
                            f"(可能是 MCP 动态注册的)",
                        )
                    )

        elif step_type == "skill":
            skill_name = step_dict.get("skill", "")
            if skill_name and registered_skills:
                if skill_name not in registered_skills:
                    report.warnings.append(
                        ValidationError(
                            path,
                            f"skill 引用 {skill_name!r} 未在 SkillRegistry 中注册"
                            f"(可能是运行时加载的)",
                        )
                    )
            _check_deprecated_prompt_field(step_dict, path, report)

        elif step_type == "condition":
            if not step_dict.get("when"):
                report.errors.append(
                    ValidationError(path, "ConditionStep 缺少 when 字段")
                )
            # 递归校验 then / else
            then_steps = step_dict.get("then", [])
            if then_steps:
                _validate_steps(then_steps, f"{path}.then", agent_names, report)
            else_steps = step_dict.get("else", [])
            if else_steps:
                _validate_steps(else_steps, f"{path}.else", agent_names, report)

        elif step_type == "loop":
            has_iter = bool(step_dict.get("iter"))
            has_until = bool(step_dict.get("until"))
            if not has_iter and not has_until:
                report.errors.append(
                    ValidationError(path, "LoopStep 必须有 iter 或 until 之一")
                )
            if has_iter and has_until:
                report.warnings.append(
                    ValidationError(path, "LoopStep 同时有 iter 和 until,iter 优先生效")
                )
            # 递归校验 step(循环体)
            body = step_dict.get("step")
            if body and isinstance(body, dict):
                _validate_steps([body], f"{path}.step", agent_names, report)
            elif not body:
                report.errors.append(
                    ValidationError(path, "LoopStep 缺少 step(循环体)")
                )

        elif step_type == "parallel":
            branches = step_dict.get("branches", [])
            if not branches:
                report.errors.append(
                    ValidationError(path, "ParallelStep 缺少 branches")
                )
            else:
                _validate_steps(branches, f"{path}.branches", agent_names, report)
            # output 唯一性(branches 内)
            branch_outputs = [
                b.get("output") for b in branches if b.get("output")
            ]
            if len(branch_outputs) != len(set(branch_outputs)):
                report.errors.append(
                    ValidationError(path, "ParallelStep branches 的 output key 重复")
                )


def _check_deprecated_prompt_field(
    step_dict: dict,
    path: str,
    report: ValidationReport,
) -> None:
    """检测 llm/skill step 使用已废弃的 ``input`` 字段。

    规范字段为 ``prompt``;``input`` 作为向后兼容别名保留,但会在校验阶段
    记录一条 :class:`ValidationError`(归入 warnings),提示迁移。``prompt``
    与 ``input`` 同时存在时,提示以 ``prompt`` 为准并建议移除 ``input``。

    Args:
        step_dict: Step 定义 dict。
        path:     当前 Step 的配置路径(用于定位)。
        report:   校验报告,warnings 追加于此。
    """
    has_prompt = "prompt" in step_dict
    has_input = "input" in step_dict
    if has_prompt and has_input:
        report.warnings.append(
            ValidationError(
                path,
                "同时存在 prompt 与 input,以 prompt 为准;建议移除已废弃的 input",
            )
        )
    elif has_input:
        report.warnings.append(
            ValidationError(
                path,
                "input 字段已废弃,请改用 prompt",
            )
        )
