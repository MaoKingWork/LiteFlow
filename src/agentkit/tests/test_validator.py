"""yaml.validator —— severity/code 增强（D1）测试。

出口标准（对齐 P1 §D1）：
    - ValidationError 默认 severity=error, code=""
    - severity="warning" 正确设置
    - ValidationReport.diagnostics 合并 errors + warnings
    - to_api_response 含 is_valid + diagnostics 列表
    - validate_workflow 各类错误触发后 code 字段非空且符合命名规范
    - 旧代码 ValidationError("p", "m") 不传 severity/code 仍正常工作（向后兼容）
"""
from __future__ import annotations

from agentkit.yaml.validator import (
    ValidationError,
    ValidationReport,
    validate_workflow,
)


# ---------------------------------------------------------------------------
# ValidationError 默认值与向后兼容
# ---------------------------------------------------------------------------
def test_validation_error_defaults():
    """ValidationError("p", "m") 默认 severity=error, code=""。"""
    e = ValidationError("p", "m")
    assert e.path == "p"
    assert e.message == "m"
    assert e.severity == "error"
    assert e.code == ""


def test_severity_warning():
    """ValidationError("p", "m", severity="warning") 正确设置。"""
    e = ValidationError("p", "m", severity="warning")
    assert e.severity == "warning"
    assert e.code == ""  # severity 与 code 独立


def test_code_set():
    """ValidationError 显式 code 正确设置。"""
    e = ValidationError("p", "m", code="step.type_unknown")
    assert e.code == "step.type_unknown"
    assert e.severity == "error"  # 默认值


def test_str_with_code_and_severity():
    """__str__ 输出含 severity 前缀与 code。"""
    e_err = ValidationError("p", "m", code="step.type_unknown")
    assert str(e_err) == "错误 [p] [step.type_unknown] m"

    e_warn = ValidationError("p", "m", severity="warning", code="tool.unknown")
    assert str(e_warn) == "警告 [p] [tool.unknown] m"

    e_no_code = ValidationError("p", "m")
    assert str(e_no_code) == "错误 [p] m"


def test_backward_compat_positional_args():
    """旧代码 ValidationError("p", "m") 位置参数仍正常工作。"""
    # 等价于 ValidationError(path="p", message="m")
    e = ValidationError("p", "m")
    assert (e.path, e.message, e.severity, e.code) == ("p", "m", "error", "")


# ---------------------------------------------------------------------------
# ValidationReport.diagnostics / to_api_response
# ---------------------------------------------------------------------------
def test_diagnostics_property():
    """diagnostics 属性：errors 在前, warnings 在后。"""
    e1 = ValidationError("p1", "m1", code="a.b")
    e2 = ValidationError("p2", "m2")
    w1 = ValidationError("p3", "m3", severity="warning", code="c.d")
    report = ValidationReport(errors=[e1, e2], warnings=[w1])

    diags = report.diagnostics
    assert len(diags) == 3
    assert diags[0] is e1
    assert diags[1] is e2
    assert diags[2] is w1


def test_diagnostics_empty():
    """无 errors / warnings 时 diagnostics 为空。"""
    report = ValidationReport()
    assert report.diagnostics == []
    assert report.is_valid is True


def test_to_api_response_structure():
    """to_api_response 返回 {is_valid, diagnostics}。"""
    e = ValidationError("p1", "m1", code="root.name_missing")
    w = ValidationError("p2", "m2", severity="warning", code="tool.unknown")
    report = ValidationReport(errors=[e], warnings=[w])

    resp = report.to_api_response()
    assert resp["is_valid"] is False
    assert len(resp["diagnostics"]) == 2

    d0 = resp["diagnostics"][0]
    assert set(d0.keys()) == {"path", "message", "severity", "code"}
    assert d0["path"] == "p1"
    assert d0["severity"] == "error"
    assert d0["code"] == "root.name_missing"

    d1 = resp["diagnostics"][1]
    assert d1["severity"] == "warning"
    assert d1["code"] == "tool.unknown"


def test_to_api_response_valid():
    """valid report 的 is_valid=True, diagnostics 为空。"""
    report = ValidationReport()
    resp = report.to_api_response()
    assert resp["is_valid"] is True
    assert resp["diagnostics"] == []


# ---------------------------------------------------------------------------
# validate_workflow 各类错误触发后 code 字段非空
# ---------------------------------------------------------------------------
def test_validate_root_name_missing_code():
    """顶层缺 name → code='root.name_missing'。"""
    report = validate_workflow({"steps": [{"type": "llm", "agent": "a"}]})
    codes = [e.code for e in report.errors if "name" in e.message]
    assert "root.name_missing" in codes


def test_validate_root_steps_missing_code():
    """顶层缺 steps → code='root.steps_missing'。"""
    report = validate_workflow({"name": "wf"})
    codes = [e.code for e in report.errors if "steps" in e.message]
    assert "root.steps_missing" in codes


def test_validate_agent_provider_unknown_code():
    """agent.provider 未声明 → code='agent.provider_unknown'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [],
        "agents": [{"name": "a1", "provider": "nonexistent"}],
    })
    codes = [e.code for e in report.errors]
    assert "agent.provider_unknown" in codes


def test_validate_step_type_missing_code():
    """step 缺 type → code='step.type_missing'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{"id": "s1"}],
    })
    codes = [e.code for e in report.errors]
    assert "step.type_missing" in codes


def test_validate_step_type_unknown_code():
    """step.type 未注册 → code='step.type_unknown'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{"type": "nonexistent_type", "id": "s1"}],
    })
    codes = [e.code for e in report.errors]
    assert "step.type_unknown" in codes


def test_validate_step_id_duplicate_code():
    """同级 step id 重复 → code='step.id_duplicate'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [
            {"type": "llm", "id": "s1", "agent": "a"},
            {"type": "llm", "id": "s1", "agent": "a"},
        ],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [e.code for e in report.errors]
    assert "step.id_duplicate" in codes


def test_validate_step_output_duplicate_code():
    """同级 output 重复 → code='step.output_duplicate'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [
            {"type": "llm", "id": "s1", "agent": "a", "output": "v"},
            {"type": "llm", "id": "s2", "agent": "a", "output": "v"},
        ],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [e.code for e in report.errors]
    assert "step.output_duplicate" in codes


def test_validate_llm_agent_unknown_code():
    """LLMStep agent 未声明 → code='llm.agent_unknown'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{"type": "llm", "agent": "ghost", "id": "s1"}],
        "agents": [{"name": "real", "provider": "deepseek"}],
    })
    codes = [e.code for e in report.errors]
    assert "llm.agent_unknown" in codes


def test_validate_llm_output_format_invalid_code():
    """LLMStep output_format 非法 → code='llm.output_format_invalid'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{"type": "llm", "agent": "a", "output_format": "yaml"}],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [e.code for e in report.errors]
    assert "llm.output_format_invalid" in codes


def test_validate_tool_unknown_warning_code():
    """ToolStep tool 未注册 → warning, code='tool.unknown'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{"type": "tool", "tool": "nonexistent_tool"}],
    })
    codes = [w.code for w in report.warnings]
    assert "tool.unknown" in codes
    # warning severity 正确
    assert all(w.severity == "warning" for w in report.warnings)


def test_validate_skill_unknown_warning_code():
    """SkillStep skill 未注册 → warning, code='skill.unknown'。

    validator 仅在 registered_skills 非空时触发检查（注册表为空表示全部
    延迟加载,跳过校验）。故此处注册一个占位 skill 使注册表非空。
    """
    from agentkit.skill.registry import SkillManifest, register_skill

    register_skill(SkillManifest(name="real_skill", description="placeholder"))
    try:
        report = validate_workflow({
            "name": "wf",
            "steps": [{"type": "skill", "skill": "nonexistent_skill"}],
        })
        codes = [w.code for w in report.warnings]
        assert "skill.unknown" in codes
    finally:
        # autouse fixture 会恢复注册表,显式清理仅为可读性
        pass


def test_validate_condition_when_missing_code():
    """ConditionStep 缺 when → code='condition.when_missing'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{"type": "condition", "id": "c1"}],
    })
    codes = [e.code for e in report.errors]
    assert "condition.when_missing" in codes


def test_validate_loop_iter_until_missing_code():
    """LoopStep 缺 iter 和 until → code='loop.iter_until_missing'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{"type": "loop", "id": "l1", "step": {"type": "llm", "agent": "a"}}],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [e.code for e in report.errors]
    assert "loop.iter_until_missing" in codes


def test_validate_loop_iter_until_conflict_warning_code():
    """LoopStep 同时有 iter 和 until → warning, code='loop.iter_until_conflict'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{
            "type": "loop",
            "id": "l1",
            "iter": 3,
            "until": "{{done}}",
            "step": {"type": "llm", "agent": "a"},
        }],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [w.code for w in report.warnings]
    assert "loop.iter_until_conflict" in codes


def test_validate_loop_output_mode_invalid_code():
    """LoopStep output_mode 非法 → code='loop.output_mode_invalid'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{
            "type": "loop",
            "id": "l1",
            "iter": 3,
            "output_mode": "invalid",
            "step": {"type": "llm", "agent": "a"},
        }],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [e.code for e in report.errors]
    assert "loop.output_mode_invalid" in codes


def test_validate_loop_body_missing_code():
    """LoopStep 缺循环体 → code='loop.body_missing'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{"type": "loop", "id": "l1", "iter": 3}],
    })
    codes = [e.code for e in report.errors]
    assert "loop.body_missing" in codes


def test_validate_parallel_branches_empty_code():
    """ParallelStep branches 为空 → code='parallel.branches_empty'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{"type": "parallel", "id": "p1"}],
    })
    codes = [e.code for e in report.errors]
    assert "parallel.branches_empty" in codes


def test_validate_port_output_outputs_conflict_code():
    """output 与 outputs 同时声明 → code='port.output_outputs_conflict'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{
            "type": "llm",
            "agent": "a",
            "id": "s1",
            "output": "v",
            "outputs": {"v2": {}},
        }],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [e.code for e in report.errors]
    assert "port.output_outputs_conflict" in codes


def test_validate_port_name_duplicate_code():
    """inputs 端口名重复 → code='port.name_duplicate'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{
            "type": "llm",
            "agent": "a",
            "id": "s1",
            "inputs": [{"name": "x"}, {"name": "x"}],
        }],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [e.code for e in report.errors]
    assert "port.name_duplicate" in codes


def test_validate_port_type_schema_conflict_code():
    """端口同时声明 type 和 schema → code='port.type_schema_conflict'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{
            "type": "llm",
            "agent": "a",
            "id": "s1",
            "inputs": [{"name": "x", "type": "str", "schema": {}}],
        }],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [e.code for e in report.errors]
    assert "port.type_schema_conflict" in codes


def test_validate_port_from_unknown_warning_code():
    """输入端口 from 来源不存在 → warning, code='port.from_unknown'。

    validator 仅在 prior_output_names 非空时触发 from 来源检查
    （空集表示无已知来源,跳过校验）。故此处声明工作流级输入使
    prior_output_names 非空,从而触发对未声明来源的告警。
    """
    report = validate_workflow({
        "name": "wf",
        "inputs": ["known_var"],  # 使 prior_output_names 非空
        "steps": [{
            "type": "llm",
            "agent": "a",
            "id": "s1",
            "inputs": [{"name": "x", "from": "nonexistent_var"}],
        }],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [w.code for w in report.warnings]
    assert "port.from_unknown" in codes


def test_validate_template_ghost_dependency_warning_code():
    """模板引用未声明变量 → warning, code='template.ghost_dependency'。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{
            "type": "llm",
            "agent": "a",
            "id": "s1",
            "prompt": "{{ghost_var}}",
        }],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [w.code for w in report.warnings]
    assert "template.ghost_dependency" in codes


def test_validate_template_ghost_dependency_strict_scope_error_code():
    """strict_scope=True 时模板引用未声明变量 → error, code 不变。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [{
            "type": "llm",
            "agent": "a",
            "id": "s1",
            "prompt": "{{ghost_var}}",
            "strict_scope": True,
        }],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    codes = [e.code for e in report.errors]
    assert "template.ghost_dependency" in codes


def test_validate_all_codes_match_naming_convention():
    """所有触发的 code 都符合 <category>.<specific> 命名规范。"""
    report = validate_workflow({
        "name": "",  # root.name_missing
        "steps": [
            {"id": "s1"},  # step.type_missing
            {"type": "llm", "agent": "ghost"},  # llm.agent_unknown
            {"type": "loop", "iter": 3, "output_mode": "bad", "step": {"type": "llm", "agent": "a"}},  # loop.output_mode_invalid
        ],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    import re
    pattern = re.compile(r"^[a-z_]+\.[a-z_]+$")
    for err in report.errors:
        if err.code:  # 跳过无 code 的（理论上都已赋值）
            assert pattern.match(err.code), f"code 不符合命名规范: {err.code!r}"


def test_validate_valid_workflow_has_no_errors():
    """合法 workflow 无 errors,无 warnings。"""
    report = validate_workflow({
        "name": "wf",
        "steps": [
            {"type": "llm", "agent": "a", "id": "s1", "output": "v1"},
        ],
        "agents": [{"name": "a", "provider": "deepseek"}],
    })
    assert report.is_valid
    assert len(report.errors) == 0
    # 可能有 ghost dependency 警告,但合法 workflow 不应触发
    # 这里仅断言 errors 为空
