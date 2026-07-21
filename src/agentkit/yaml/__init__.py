"""yaml —— YAML 声明式工作流子包。

提供 YAML 工作流定义的加载与校验:

    - loader:    YAML → SDK 对象(Workflow / Step / Agent)编译器
    - validator: YAML 静态校验器(字段 / 引用 / 唯一性检查)

典型用法::

    from agentkit.yaml import load_workflow, validate_workflow

    report = validate_workflow(yaml_dict)
    if report.is_valid:
        wf = load_workflow("workflow.yaml")
        result = await wf.run(inputs={"date": "2024-01-01"})
"""

from agentkit.yaml.loader import load_workflow, load_workflow_from_dict
from agentkit.yaml.validator import (
    ValidationError,
    ValidationReport,
    validate_workflow,
)

__all__ = [
    "load_workflow",
    "load_workflow_from_dict",
    "validate_workflow",
    "ValidationError",
    "ValidationReport",
]
