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
   10. 端口校验:端口名唯一 / type 与 schema 互斥 / output 与 outputs 互斥 /
      模板变量引用扫描 / 并行端口冲突提示。

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

import re
from dataclasses import dataclass, field
from typing import Any

from agentkit.steps.base import BaseStep, _GLOBAL_STEP_REGISTRY
from agentkit.tools.base import list_tools
from agentkit.skill.registry import list_skills

__all__ = ["ValidationError", "ValidationReport", "validate_workflow"]


@dataclass
class ValidationError:
    """单个校验错误。

    Attributes:
        path:      错误所在路径(如 ``steps[2]`` / ``steps[0].then[1]``)。
        message:   错误描述。
        severity:  严重级别,``error``(默认,阻止执行)或 ``warning``(仅提示)。
        code:      机器可读错误码(如 ``step.type_unknown``),空字符串表示无码。
                   命名规范 ``<类别>.<具体>``,供前端按 code 分组展示或国际化。
    """

    path: str
    message: str
    severity: str = "error"
    code: str = ""

    def __str__(self) -> str:
        prefix = "错误" if self.severity == "error" else "警告"
        code_str = f" [{self.code}]" if self.code else ""
        return f"{prefix} [{self.path}]{code_str} {self.message}"


@dataclass
class ValidationReport:
    """校验报告。

    Attributes:
        errors:   错误列表(阻止执行)。
        warnings: 警告列表(不阻止执行)。
    """

    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """是否通过校验(无错误)。"""
        return len(self.errors) == 0

    @property
    def diagnostics(self) -> list[ValidationError]:
        """所有诊断项(errors 在前,warnings 在后)。

        供前端统一渲染:每条自带 ``severity`` 字段,无需分别处理 errors/warnings。
        """
        return list(self.errors) + list(self.warnings)

    def to_api_response(self) -> dict:
        """序列化为 API 响应格式。

        Returns:
            dict: ``{"is_valid": bool, "diagnostics": [...]}``,
            每条 diagnostic 含 ``path`` / ``message`` / ``severity`` / ``code``。
        """
        return {
            "is_valid": self.is_valid,
            "diagnostics": [
                {
                    "path": d.path,
                    "message": d.message,
                    "severity": d.severity,
                    "code": d.code,
                }
                for d in self.diagnostics
            ],
        }

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
        report.errors.append(
            ValidationError("[root]", "缺少必填字段 name", code="root.name_missing")
        )
    if not config.get("steps"):
        report.errors.append(
            ValidationError(
                "[root]", "缺少必填字段 steps 或为空", code="root.steps_missing"
            )
        )

    # 2a. 收集已声明的 provider 名(预设 + YAML providers 段)
    from agentkit.llm.provider import PRESET_PROVIDERS

    provider_names: set[str] = set(PRESET_PROVIDERS.keys())
    for prov_dict in config.get("providers", []):
        prov_name = prov_dict.get("name", "")
        if prov_name:
            provider_names.add(prov_name)

    # 2b. 收集 agent 名 + 校验 agent.provider 引用
    agent_names = set()
    for agent_dict in config.get("agents", []):
        name = agent_dict.get("name", "")
        if name:
            agent_names.add(name)
        # 校验 provider 引用
        prov_ref = agent_dict.get("provider")
        if prov_ref and prov_ref not in provider_names:
            report.errors.append(
                ValidationError(
                    f"agents[{name}]",
                    f"agent {name!r} 的 provider {prov_ref!r} 未在 providers "
                    f"段声明,也不是内置预设。可用: {sorted(provider_names)}",
                    code="agent.provider_unknown",
                )
            )

    # 2c. 收集工作流级输入名（供 from 来源检查）
    wf_inputs: set[str] = set()
    for inp in config.get("inputs", []):
        if isinstance(inp, str):
            wf_inputs.add(inp)
        elif isinstance(inp, dict) and "name" in inp:
            wf_inputs.add(inp["name"])

    # 3. 校验 steps
    steps = config.get("steps", [])
    if isinstance(steps, list):
        _validate_steps(
            steps, "steps", agent_names, report, wf_inputs, provider_names
        )

    return report


def _validate_steps(
    steps: list[dict],
    path_prefix: str,
    agent_names: set[str],
    report: ValidationReport,
    wf_inputs: set[str] | None = None,
    provider_names: set[str] | None = None,
) -> None:
    """递归校验 Step 列表。

    Args:
        steps:          Step dict 列表。
        path_prefix:    路径前缀(如 ``steps`` / ``steps[0].then``)。
        agent_names:    已声明的 agent 名集合。
        report:         校验报告。
        wf_inputs:      工作流级输入名集合（供端口 from 来源检查）。
        provider_names: 已声明的 provider 名集合（供 agent.provider 校验）。
    """
    seen_ids: set[str] = set()
    seen_outputs: set[str] = set()
    # 收集前序 Step 的输出端口名（含 output 字段），供 from 来源检查
    prior_output_names: set[str] = set()
    if wf_inputs:
        prior_output_names |= wf_inputs
    registered_tools = set(list_tools())
    registered_skills = set(list_skills())
    registered_types = set(_GLOBAL_STEP_REGISTRY.list())

    for i, step_dict in enumerate(steps):
        if not isinstance(step_dict, dict):
            report.errors.append(
                ValidationError(
                    f"{path_prefix}[{i}]",
                    "Step 定义应为 dict",
                    code="step.not_dict",
                )
            )
            continue

        path = f"{path_prefix}[{i}]"
        step_type = step_dict.get("type", "")
        step_id = step_dict.get("id", "")

        # 3a. type 必填且已注册
        if not step_type:
            report.errors.append(
                ValidationError(path, "缺少 type 字段", code="step.type_missing")
            )
            continue
        if step_type not in registered_types:
            report.errors.append(
                ValidationError(
                    path,
                    f"未注册的 Step type: {step_type!r}。"
                    f"已注册: {sorted(registered_types)}",
                    code="step.type_unknown",
                )
            )

        # 3b. id 唯一性
        if step_id:
            if step_id in seen_ids:
                report.errors.append(
                    ValidationError(
                        path,
                        f"重复的 Step id: {step_id!r}",
                        code="step.id_duplicate",
                    )
                )
            else:
                seen_ids.add(step_id)

        # 3c. output 唯一性(同级)
        output = step_dict.get("output")
        if output:
            # append 模式的 loop 可复用前序步骤的 output 作为累加种子,合法
            is_append_seed = (
                step_type == "loop"
                and step_dict.get("output_mode", "collect") == "append"
            )
            if output in seen_outputs and not is_append_seed:
                report.errors.append(
                    ValidationError(
                        path,
                        f"同级 output key 重复: {output!r}",
                        code="step.output_duplicate",
                    )
                )
            else:
                seen_outputs.add(output)

        # 3c-bis. 端口校验（llm / tool / skill）
        if step_type in ("llm", "tool", "skill"):
            _validate_ports(step_dict, path, prior_output_names, report)

        # 3d. 按类型校验
        if step_type == "llm":
            agent_ref = step_dict.get("agent", "")
            if isinstance(agent_ref, str) and agent_ref and agent_names:
                if agent_ref not in agent_names:
                    report.errors.append(
                        ValidationError(
                            path,
                            f"agent 引用 {agent_ref!r} 未在 agents 段声明",
                            code="llm.agent_unknown",
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
                        code="llm.output_format_invalid",
                    )
                )
            # stream 必须为 bool
            stream = step_dict.get("stream", False)
            if not isinstance(stream, bool):
                report.errors.append(
                    ValidationError(
                        path,
                        f"stream 必须为布尔值(true/false),当前: {stream!r}",
                        code="llm.stream_invalid",
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
                            severity="warning",
                            code="tool.unknown",
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
                            severity="warning",
                            code="skill.unknown",
                        )
                    )
            _check_deprecated_prompt_field(step_dict, path, report)

        elif step_type == "condition":
            if not step_dict.get("when"):
                report.errors.append(
                    ValidationError(
                        path,
                        "ConditionStep 缺少 when 字段",
                        code="condition.when_missing",
                    )
                )
            # 递归校验 then / else
            then_steps = step_dict.get("then", [])
            if then_steps:
                _validate_steps(then_steps, f"{path}.then", agent_names, report, prior_output_names, provider_names)
            else_steps = step_dict.get("else", [])
            if else_steps:
                _validate_steps(else_steps, f"{path}.else", agent_names, report, prior_output_names, provider_names)

        elif step_type == "loop":
            has_iter = bool(step_dict.get("iter"))
            has_until = bool(step_dict.get("until"))
            if not has_iter and not has_until:
                report.errors.append(
                    ValidationError(
                        path,
                        "LoopStep 必须有 iter 或 until 之一",
                        code="loop.iter_until_missing",
                    )
                )
            if has_iter and has_until:
                report.warnings.append(
                    ValidationError(
                        path,
                        "LoopStep 同时有 iter 和 until,iter 优先生效",
                        severity="warning",
                        code="loop.iter_until_conflict",
                    )
                )
            # output_mode 枚举校验
            output_mode = step_dict.get("output_mode", "collect")
            if output_mode not in ("collect", "append", "last"):
                report.errors.append(
                    ValidationError(
                        path,
                        f"LoopStep output_mode 必须为 collect/append/last,当前为 {output_mode!r}",
                        code="loop.output_mode_invalid",
                    )
                )
            # separator 类型校验
            separator = step_dict.get("separator", "")
            if not isinstance(separator, str):
                report.errors.append(
                    ValidationError(
                        path,
                        f"LoopStep separator 必须为字符串,当前为 {type(separator).__name__}",
                        code="loop.separator_invalid",
                    )
                )
            # append 模式需显式声明 output(累加目标)
            if output_mode == "append" and not step_dict.get("output"):
                report.errors.append(
                    ValidationError(
                        path,
                        "LoopStep output_mode=append 需要显式声明 output(累加目标)",
                        code="loop.append_output_missing",
                    )
                )
            # 递归校验 step(循环体)
            body = step_dict.get("step")
            if body and isinstance(body, dict):
                _validate_steps([body], f"{path}.step", agent_names, report, prior_output_names, provider_names)
            elif not body:
                report.errors.append(
                    ValidationError(
                        path,
                        "LoopStep 缺少 step(循环体)",
                        code="loop.body_missing",
                    )
                )

        elif step_type == "parallel":
            branches = step_dict.get("branches", [])
            if not branches:
                report.errors.append(
                    ValidationError(
                        path,
                        "ParallelStep 缺少 branches",
                        code="parallel.branches_empty",
                    )
                )
            else:
                _validate_steps(branches, f"{path}.branches", agent_names, report, prior_output_names, provider_names)
            # output 唯一性(branches 内)：含 output 字段和 outputs 端口名
            branch_out_keys: list[str] = []
            for b in branches:
                if not isinstance(b, dict):
                    continue
                if b.get("output"):
                    branch_out_keys.append(b["output"])
                for op in _extract_port_names(b.get("outputs")):
                    branch_out_keys.append(op)
            dupes = [k for k in branch_out_keys if branch_out_keys.count(k) > 1]
            if dupes:
                report.errors.append(
                    ValidationError(
                        path,
                        f"ParallelStep branches 的 output/outputs 端口名重复: {set(dupes)}。"
                        f"请为各分支指定不同 name",
                        code="parallel.branch_output_duplicate",
                    )
                )

        # 3e. 收集本 Step 的输出端口名，供后续 Step 的 from 来源检查
        if output:
            prior_output_names.add(output)
        for op_name in _extract_port_names(step_dict.get("outputs")):
            prior_output_names.add(op_name)


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
                severity="warning",
                code="prompt.input_deprecated_conflict",
            )
        )
    elif has_input:
        report.warnings.append(
            ValidationError(
                path,
                "input 字段已废弃,请改用 prompt",
                severity="warning",
                code="prompt.input_deprecated",
            )
        )


# ---------------------------------------------------------------------------
# 端口校验辅助
# ---------------------------------------------------------------------------
def _extract_port_names(spec: Any) -> list[str]:
    """从 inputs/outputs 声明中提取端口名列表。"""
    names: list[str] = []
    if spec is None:
        return names
    if isinstance(spec, dict):
        return list(spec.keys())
    if isinstance(spec, list):
        for item in spec:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and "name" in item:
                names.append(item["name"])
    return names


def _validate_ports(
    step_dict: dict,
    path: str,
    prior_output_names: set[str],
    report: ValidationReport,
) -> None:
    """校验 Step 的端口声明。

    检查项:
        - output 与 outputs 不可同时声明。
        - 端口名唯一（输入端口互斥、输出端口互斥）。
        - type 与 schema 互斥。
        - 输入 from 来源存在（工作流输入或前序输出），不确定时降级 warning。
        - 模板变量引用扫描：prompt/params 中引用的 {{var}} 未在 inputs 声明
          且非已知来源时记 warning（strict_scope 时升级为 error）。
    """
    # output 与 outputs 互斥
    if step_dict.get("output") and step_dict.get("outputs") is not None:
        report.errors.append(
            ValidationError(
                path,
                "output 与 outputs 不可同时声明",
                code="port.output_outputs_conflict",
            )
        )

    inputs_spec = step_dict.get("inputs")
    outputs_spec = step_dict.get("outputs")

    # 端口名唯一性
    input_names = _extract_port_names(inputs_spec)
    output_names = _extract_port_names(outputs_spec)
    _check_dup_names(input_names, "inputs", path, report)
    _check_dup_names(output_names, "outputs", path, report)

    # type / schema 互斥 + from 来源检查（逐端口）
    for item in _iter_port_dicts(inputs_spec):
        _check_port_fields(item, path, "inputs", report)
        from_key = item.get("from", item.get("name"))
        if from_key and prior_output_names and from_key not in prior_output_names:
            report.warnings.append(
                ValidationError(
                    path,
                    f"输入端口 {item['name']!r} 的 from 来源 {from_key!r} "
                    f"未在已知来源(workflow inputs / 前序 outputs)中找到,"
                    f"可能是动态写入的 key",
                    severity="warning",
                    code="port.from_unknown",
                )
            )
    for item in _iter_port_dicts(outputs_spec):
        _check_port_fields(item, path, "outputs", report)

    # 模板变量引用扫描
    strict_scope = bool(step_dict.get("strict_scope", False))
    declared_input_names = set(input_names)
    # 收集模板中引用的变量名
    referenced: set[str] = set()
    prompt = step_dict.get("prompt") or step_dict.get("input")
    if isinstance(prompt, str):
        referenced |= _extract_template_vars(prompt)
    params = step_dict.get("params")
    if isinstance(params, dict):
        for v in params.values():
            if isinstance(v, str):
                referenced |= _extract_template_vars(v)
    # 检查未声明引用
    known = declared_input_names | prior_output_names
    for var_name in referenced:
        # 跳过环境变量 ${ENV} 和已知来源
        if var_name in known:
            continue
        msg = (
            f"模板引用了变量 {var_name!r},但未在 inputs 中声明,"
            f"且不在已知来源中(幽灵依赖)"
        )
        if strict_scope:
            report.errors.append(
                ValidationError(
                    path,
                    msg + "(strict_scope=True)",
                    code="template.ghost_dependency",
                )
            )
        else:
            report.warnings.append(
                ValidationError(
                    path,
                    msg,
                    severity="warning",
                    code="template.ghost_dependency",
                )
            )


def _iter_port_dicts(spec: Any):
    """迭代端口声明中的 dict 项（跳过纯字符串简写）。"""
    if isinstance(spec, list):
        for item in spec:
            if isinstance(item, dict):
                yield item


def _check_dup_names(
    names: list[str], label: str, path: str, report: ValidationReport
) -> None:
    """检查端口名是否重复。"""
    seen: set[str] = set()
    for name in names:
        if name in seen:
            report.errors.append(
                ValidationError(
                    path,
                    f"{label} 端口名重复: {name!r}",
                    code="port.name_duplicate",
                )
            )
        seen.add(name)


def _check_port_fields(
    item: dict, path: str, label: str, report: ValidationReport
) -> None:
    """校验单个端口 dict 的字段约束。"""
    if "name" not in item:
        report.errors.append(
            ValidationError(
                path,
                f"{label} 端口声明缺少 name 字段: {item!r}",
                code="port.name_missing",
            )
        )
        return
    if "type" in item and "schema" in item:
        report.errors.append(
            ValidationError(
                path,
                f"{label} 端口 {item['name']!r} 的 type 与 schema 不可同时声明",
                code="port.type_schema_conflict",
            )
        )


# 匹配 {{var}} 或 {{ var }} 中的变量名（不匹配 {{#each}}/{{/each}} 等块语法）
_VAR_PATTERN = re.compile(r"\{\{\s*(?![#/])([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*)\s*\}\}")


def _extract_template_vars(text: str) -> set[str]:
    """从模板字符串中提取 ``{{var}}`` 引用的顶层变量名。

    跳过块语法（``{{#each}}`` / ``{{/each}}`` 等）。
    对于 ``{{obj.field}}`` 形式，取顶层名 ``obj``。
    """
    names: set[str] = set()
    for match in _VAR_PATTERN.finditer(text):
        name = match.group(1).split(".")[0]
        names.add(name)
    return names


# ---------------------------------------------------------------------------
# 会话静态校验（基于 walk_all_steps 通用遍历,不依赖手写类型枚举）
# ---------------------------------------------------------------------------
def _validate_conversation_keys(steps: list[BaseStep]) -> None:
    """校验 conversation.key 不与任何 Step 的 output 重名。

    含 ``{{`` 的模板 key 跳过字面碰撞检查(运行期才确定,且与静态 output 名
    同名的概率极低),改由运行时 ``_type`` 标记兜底(unpack_conversation
    失败即报错)。

    Args:
        steps: 顶层 Step 列表(已编译的 BaseStep 实例树)。

    Raises:
        ValueError: conversation.key 与某 Step output 重名。
    """
    from agentkit.steps.base import walk_all_steps

    all_steps = walk_all_steps(steps)
    output_keys: set[str] = set()
    conv_keys: set[str] = set()

    for step in all_steps:
        # 收集所有 output 端口名
        for port in getattr(step, "outputs", []):
            output_keys.add(port.name)
        # 也收集单 output 的名字(step.output)
        if getattr(step, "output", None):
            output_keys.add(step.output)
        # 收集 conversation_key(跳过模板)
        if hasattr(step, "conversation_key") and step.conversation_key:
            k = step.conversation_key
            if "{{" in k:
                continue  # 模板 key:跳过字面碰撞检查
            conv_keys.add(k)

    collisions = output_keys & conv_keys
    if collisions:
        raise ValueError(
            f"conversation.key 与 Step output 重名: {collisions}。"
            f"请使用不同的键名避免数据覆盖。"
        )


def _validate_condition_branch_consistency(steps: list[BaseStep]) -> None:
    """校验 condition 分支中 conversation 配置的一致性约束。

    当 then/else 两侧均含 conversation 配置时,校验:
        - conversation.key 必须相同(操作同一会话)。
        - output 必须相同(写入同一变量)。

    这约束了"start/continue 分支必须保持等价"这个语义不变量,防止只改
    一个分支导致首轮与后续轮次行为不一致。prompt 和 agent 允许不同
    (start 是"创作第1章",continue 是"续写")。

    Args:
        steps: 顶层 Step 列表(已编译的 BaseStep 实例树)。

    Raises:
        ValueError: then/else 分支 conversation.key 或 output 不一致。
    """
    from agentkit.steps.base import walk_all_steps
    from agentkit.steps.condition_step import ConditionStep

    for step in walk_all_steps(steps):
        if not isinstance(step, ConditionStep):
            continue
        then_convs = [s for s in step.then_steps
                      if hasattr(s, "conversation_key") and s.conversation_key]
        else_convs = [s for s in step.else_steps
                      if hasattr(s, "conversation_key") and s.conversation_key]
        if not then_convs or not else_convs:
            continue  # 只有一侧有 conversation,不约束

        then_keys = {s.conversation_key for s in then_convs}
        else_keys = {s.conversation_key for s in else_convs}
        if then_keys != else_keys:
            raise ValueError(
                f"ConditionStep {step.id!r} 的 then/else 分支 "
                f"conversation.key 不一致: then={then_keys}, else={else_keys}。"
                f"分支两侧必须操作同一会话。"
            )

        then_outs = {s.output for s in then_convs if getattr(s, "output", None)}
        else_outs = {s.output for s in else_convs if getattr(s, "output", None)}
        if then_outs and else_outs and then_outs != else_outs:
            raise ValueError(
                f"ConditionStep {step.id!r} 的 then/else 分支 "
                f"output 不一致: then={then_outs}, else={else_outs}。"
                f"分支两侧必须写入同一 output 变量。"
            )
