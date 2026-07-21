"""AgentKit —— 轻量化智能体框架。

AgentKit 采用双层架构（YAML 配置层 + Python SDK 层），围绕 8 个核心概念构建：
Agent / Tool / Skill / MCP / Step / Context / Workflow / Hooks。

核心特性：
    - 高度模块化：core/steps/skill/mcp/llm/parsers/tools/yaml 各司其职
    - 易于配置：所有默认值集中在 ``agentkit.config`` 统一管理
    - 可拓展：新增 Step 类型、Tool、Skill 等均通过注册机制接入
    - 6 种 Step 类型覆盖常见编排场景

为避免循环依赖，顶层包仅导出版本号与便捷入口，子模块按需在各自位置显式导入。

快速入门::

    from agentkit.yaml import load_workflow
    wf = load_workflow("workflow.yaml")
    result = await wf.run(inputs={"date": "2024-01-01"})
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
