"""yaml.loader —— YAML 工作流定义编译器。

本模块将 YAML 工作流定义文件编译为 SDK 对象(``Workflow`` / ``BaseStep`` /
``AgentConfig`` / ``MCPManager`` 等),是 YAML 声明式定义与 SDK 编程式 API
之间的桥梁。

编译流程:
    1. 解析 YAML 文件为 dict。
    2. 递归解析 ``${ENV}`` 环境变量。
    3. 注册 Agent(从 ``agents`` 段构造 ``AgentConfig`` 实例)。
    4. 加载 Skill(从 ``skills`` 段调用 ``SkillLoader``)。
    5. 配置 MCP Server(从 ``mcp_servers`` 段构造 ``MCPManager``)。
    6. 递归编译 Step(从 ``steps`` 段构造 ``BaseStep`` 实例树)。
    7. 组装 ``Workflow`` 对象。

设计原则:
    - 高度模块化:依赖 ``steps`` / ``core`` / ``skill`` / ``mcp`` / ``config``。
    - 递归编译:condition / loop / parallel 的子 Step 递归处理。
    - ``${ENV}`` 在加载期解析;``{{var}}`` 保留到运行期由 Step 解析。
    - 类型注解完整,中文 docstring。

公开 API:
    - load_workflow:         从 YAML 文件加载 Workflow
    - load_workflow_from_dict: 从 dict 加载 Workflow
"""

from __future__ import annotations

import os
import re
from typing import Any

from agentkit.config import RetryPolicy
from agentkit.core.agent import AgentConfig
from agentkit.core.hooks import LifecycleHooks
from agentkit.core.ports import InputPort, OutputPort, PortType
from agentkit.core.workflow import Workflow
from agentkit.mcp.manager import MCPManager, MCPServerConfig
from agentkit.skill.loader import SkillLoader
from agentkit.steps.base import BaseStep, get_step_type

# 可选依赖 PyYAML:延迟导入,未安装时抛带提示的 ImportError
try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore[assignment]

# 可选依赖 pydantic:用于 output_model 动态构造
try:
    from pydantic import BaseModel as _BaseModel
    from pydantic import create_model as _create_model
except ImportError:
    _BaseModel = None  # type: ignore[assignment]
    _create_model = None  # type: ignore[assignment]

__all__ = ["load_workflow", "load_workflow_from_dict"]


# ---------------------------------------------------------------------------
# ${ENV} 环境变量解析
# ---------------------------------------------------------------------------
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_env(value: Any) -> Any:
    """递归替换 ``${ENV_VAR}`` 为环境变量值。

    对 str / dict / list 递归处理;其他类型原样返回。
    未设置的环境变量替换为空字符串。
    """
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), ""), value
        )
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# JSON Schema → Pydantic Model
# ---------------------------------------------------------------------------
def _json_type_to_python(schema: dict) -> type:
    """将 JSON Schema type 映射为 Python 类型。"""
    json_type = schema.get("type", "string")
    mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    return mapping.get(json_type, Any)


def _schema_to_model(name: str, schema: dict) -> type | None:
    """将 JSON Schema 转为 Pydantic BaseModel 子类。

    简化实现:仅处理一层 properties;嵌套 object 退化为 dict。
    pydantic 不可用时返回 None。
    """
    if _create_model is None:
        return None
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        py_type = _json_type_to_python(prop_schema)
        if prop_name in required:
            fields[prop_name] = (py_type, ...)
        else:
            fields[prop_name] = (py_type | None, None)
    return _create_model(name, **fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RetryPolicy 解析
# ---------------------------------------------------------------------------
def _parse_retry(retry_dict: dict | None) -> RetryPolicy | None:
    """从 YAML dict 解析 RetryPolicy。"""
    if retry_dict is None:
        return None
    return RetryPolicy(
        count=int(retry_dict.get("count", 1)),
        backoff=retry_dict.get("backoff", "fixed"),
        base_seconds=float(retry_dict.get("base_seconds", 5.0)),
    )


# ---------------------------------------------------------------------------
# Step 编译(递归)
# ---------------------------------------------------------------------------
def _step_prompt(step_dict: dict) -> str | None:
    """提取 Step 的提示词模板字段。

    优先取规范字段 ``prompt``;为向后兼容回退到已废弃的 ``input``。
    两者同时存在时以 ``prompt`` 为准(与 SkillStep 构造函数的互斥语义一致)。

    Args:
        step_dict: YAML 中的 Step 定义 dict。

    Returns:
        str | None: 提示词模板;未配置时为 None。
    """
    prompt = step_dict.get("prompt")
    if prompt is not None:
        return prompt
    return step_dict.get("input")


# ---------------------------------------------------------------------------
# 端口声明编译
# ---------------------------------------------------------------------------
def _compile_port_spec(
    spec: Any, port_cls: type
) -> list:
    """把 YAML 端口声明编译为 InputPort/OutputPort 列表。

    支持简写阶梯:
        - ``[a, b]``                 → ``[{name: a}, {name: b}]``
        - ``{a: str, b: int}``       → ``[{name: a, type: str}, {name: b, type: int}]``
        - ``[{name: a, type: str}]`` → 原样

    Args:
        spec:     YAML 中的 inputs/outputs 声明。
        port_cls: InputPort 或 OutputPort。

    Returns:
        list[InputPort | OutputPort]: 编译后的端口列表。
    """
    if spec is None:
        return []
    ports: list = []
    if isinstance(spec, dict):
        # dict 简写：{name: type_str} 或 {name: schema_dict}
        for name, type_spec in spec.items():
            kwargs: dict[str, Any] = {"name": name}
            if isinstance(type_spec, str):
                kwargs["type"] = PortType.parse(type_spec)
            elif isinstance(type_spec, dict) and "type" not in type_spec:
                # 纯 schema dict（无 name/type/required 等端口字段）
                kwargs["type"] = PortType.parse(type_spec)
            ports.append(port_cls(**kwargs))
    elif isinstance(spec, list):
        for item in spec:
            if isinstance(item, str):
                ports.append(port_cls(name=item))
            elif isinstance(item, dict):
                kwargs = _compile_port_fields(item)
                ports.append(port_cls(**kwargs))
            else:
                raise ValueError(f"端口声明列表项不支持 {type(item).__name__}: {item!r}")
    else:
        raise ValueError(f"端口声明不支持 {type(spec).__name__}: {spec!r}")
    return ports


def _compile_port_fields(item: dict) -> dict[str, Any]:
    """把单个端口的 dict 声明编译为构造参数。

    处理 name / from / type / schema / required / strict / default / description。
    ``type`` 与 ``schema`` 互斥。
    """
    kwargs: dict[str, Any] = {}
    if "name" not in item:
        raise ValueError(f"端口声明缺少 name 字段: {item!r}")
    kwargs["name"] = item["name"]
    if "from" in item:
        kwargs["from_"] = item["from"]
    if "type" in item and "schema" in item:
        raise ValueError(
            f"端口 {item['name']!r} 的 type 与 schema 不可同时声明"
        )
    if "type" in item:
        kwargs["type"] = PortType.parse(item["type"])
    elif "schema" in item:
        kwargs["type"] = PortType.parse(item["schema"])
    if "required" in item:
        kwargs["required"] = bool(item["required"])
    if "strict" in item:
        kwargs["strict"] = bool(item["strict"])
    if "default" in item:
        kwargs["default"] = item["default"]
    if "description" in item:
        kwargs["description"] = str(item["description"])
    return kwargs


def _compile_step_ports(step_dict: dict) -> tuple[list, list, bool]:
    """从 step_dict 编译 inputs/outputs/strict_scope。

    Returns:
        tuple: (inputs, outputs, strict_scope)
    """
    inputs = _compile_port_spec(step_dict.get("inputs"), InputPort)
    outputs = _compile_port_spec(step_dict.get("outputs"), OutputPort)
    strict_scope = bool(step_dict.get("strict_scope", False))
    return inputs, outputs, strict_scope


def _compile_step(
    step_dict: dict,
    agent_configs: dict[str, AgentConfig],
) -> BaseStep:
    """递归编译单个 Step 定义为 ``BaseStep`` 实例。

    Args:
        step_dict:      YAML 中的 Step 定义 dict。
        agent_configs:  已解析的 Agent 配置(name → AgentConfig)。

    Returns:
        BaseStep: 编译后的 Step 实例。

    Raises:
        KeyError: Step type 未注册。
        ValueError: 必填字段缺失。
    """
    step_type = step_dict.get("type", "")
    if not step_type:
        raise ValueError(f"Step 缺少 type 字段: {step_dict}")

    step_cls = get_step_type(step_type)
    step_id = step_dict.get("id", "")
    output = step_dict.get("output")
    retry = _parse_retry(step_dict.get("retry"))
    timeout = step_dict.get("timeout")

    # 端口声明编译（llm / tool / skill 支持显式端口）
    if step_type in ("llm", "tool", "skill"):
        inputs, outputs_list, strict_scope = _compile_step_ports(step_dict)
    else:
        inputs, outputs_list, strict_scope = [], [], False

    # 按类型分发
    if step_type == "llm":
        agent_ref = step_dict.get("agent", "")
        if isinstance(agent_ref, str) and agent_ref in agent_configs:
            agent = agent_configs[agent_ref]
        else:
            agent = agent_ref

        # 解析 conversation 配置块（v0.5 新增）
        # 不配置 conversation 时全部走默认值，行为与旧版完全一致（向后兼容）。
        # key/from 支持 {{var}} 模板，此处原样保留字符串，运行期由 LLMStep 渲染。
        conv = step_dict.get("conversation")
        if conv:
            conv_mode = conv.get("mode")
            conv_key = conv.get("key")
            conv_from = conv.get("from")
            conv_fork_at = conv.get("fork_at", "last")
            conv_compat = conv.get("compat", "strict")
        else:
            conv_mode = None
            conv_key = None
            conv_from = None
            conv_fork_at = "last"
            conv_compat = "strict"

        # 校验 compat 合法取值（strict / passthrough）
        if conv_compat not in ("strict", "passthrough"):
            raise ValueError(
                f"Step {step_id!r} 的 conversation.compat 仅支持 strict|passthrough，"
                f"得到 {conv_compat!r}"
            )

        return step_cls(
            id=step_id,
            agent=agent,
            prompt=_step_prompt(step_dict),
            output=output,
            output_format=step_dict.get("output_format", "text"),
            stream=bool(step_dict.get("stream", False)),
            retry=retry,
            timeout=timeout,
            inputs=inputs,
            outputs=outputs_list,
            strict_scope=strict_scope,
            # 会话参数（v0.5 新增）
            conversation_mode=conv_mode,
            conversation_key=conv_key,
            conversation_from=conv_from,
            conversation_fork_at=conv_fork_at,
            conversation_compat=conv_compat,
        )

    elif step_type == "tool":
        return step_cls(
            id=step_id,
            tool=step_dict.get("tool", ""),
            params=step_dict.get("params", {}),
            output=output,
            role=step_dict.get("role"),
            retry=retry,
            timeout=timeout,
            inputs=inputs,
            outputs=outputs_list,
            strict_scope=strict_scope,
        )

    elif step_type == "skill":
        return step_cls(
            id=step_id,
            skill=step_dict.get("skill", ""),
            prompt=_step_prompt(step_dict),
            output=output,
            model=step_dict.get("model"),
            retry=retry,
            timeout=timeout,
            inputs=inputs,
            outputs=outputs_list,
            strict_scope=strict_scope,
        )

    elif step_type == "condition":
        then_steps = [
            _compile_step(s, agent_configs)
            for s in step_dict.get("then", [])
        ]
        else_steps = [
            _compile_step(s, agent_configs)
            for s in step_dict.get("else", [])
        ]
        return step_cls(
            id=step_id,
            when=step_dict.get("when", ""),
            then_steps=then_steps,
            else_steps=else_steps,
            output=output,
            retry=retry,
            timeout=timeout,
        )

    elif step_type == "loop":
        body_dict = step_dict.get("step")
        body = _compile_step(body_dict, agent_configs) if body_dict else None
        return step_cls(
            id=step_id,
            iter=step_dict.get("iter"),
            item_var=step_dict.get("as", "item"),
            until=step_dict.get("until"),
            step=body,
            max=step_dict.get("max"),
            on_max=step_dict.get("on_max", "fail"),
            output=output,
            output_mode=step_dict.get("output_mode", "collect"),
            separator=step_dict.get("separator", ""),
            retry=retry,
            timeout=timeout,
        )

    elif step_type == "parallel":
        branches = [
            _compile_step(s, agent_configs)
            for s in step_dict.get("branches", [])
        ]
        return step_cls(
            id=step_id,
            branches=branches,
            max_concurrency=step_dict.get("max_concurrency"),
            timeout=timeout,
            on_error=step_dict.get("on_error", "fail_fast"),
            output=output,
            retry=retry,
        )

    else:
        # 未知类型:尝试通用构造(传所有可识别字段)
        return step_cls(
            id=step_id,
            output=output,
            retry=retry,
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Agent 配置解析
# ---------------------------------------------------------------------------
def _compile_agents(agents_list: list[dict]) -> dict[str, AgentConfig]:
    """从 YAML ``agents`` 段构造 AgentConfig 实例字典。

    Args:
        agents_list: Agent 配置 dict 列表。

    Returns:
        dict[str, AgentConfig]: agent name → AgentConfig 实例。
    """
    configs: dict[str, AgentConfig] = {}
    for agent_dict in agents_list:
        name = agent_dict.get("name", "")
        if not name:
            continue
        output_model = None
        om_schema = agent_dict.get("output_model")
        if om_schema and isinstance(om_schema, dict):
            output_model = _schema_to_model(f"{name}_output", om_schema)
        configs[name] = AgentConfig(
            name=name,
            model=agent_dict.get("model", "gpt-4o-mini"),
            provider=agent_dict.get("provider"),
            system=agent_dict.get("system", ""),
            output_model=output_model,
            temperature=float(agent_dict.get("temperature", 0.2)),
            tools=agent_dict.get("tools", []),
            mcp=agent_dict.get("mcp", []),
            max_tool_iterations=int(agent_dict.get("max_tool_iterations", 0)),
        )
    return configs


# ---------------------------------------------------------------------------
# Provider 配置解析
# ---------------------------------------------------------------------------
def _compile_providers(providers_list: list[dict]) -> None:
    """从 YAML ``providers`` 段注册自定义 LLM 提供商。

    预设提供商(deepseek / mimo / mimo-omni)只需提供 name + api_key 即可覆盖;
    自定义提供商需提供 name + base_url + api_key + model。

    options 段按提供商类型解析:
        - deepseek / deepseek-flash: ``DeepSeekOptions``(thinking / reasoning_effort / json_output)
        - mimo / mimo-omni:          ``ThinkingOptions``(thinking / json_output)
        - 其他:                       原样存为 dict,不解析

    Args:
        providers_list: Provider 配置 dict 列表。
    """
    from agentkit.llm.provider import (
        LLMProvider,
        DeepSeekOptions,
        register_provider,
        get_provider,
        PRESET_PROVIDERS,
    )
    from agentkit.llm.thinking import ThinkingOptions

    for prov_dict in providers_list:
        name = prov_dict.get("name", "")
        if not name:
            continue

        # 解析 options(按提供商类型)
        options = None
        opts_dict = prov_dict.get("options")
        if opts_dict:
            if name in ("deepseek", "deepseek-flash"):
                options = DeepSeekOptions(
                    thinking=opts_dict.get("thinking"),
                    reasoning_effort=opts_dict.get("reasoning_effort"),
                    json_output=bool(opts_dict.get("json_output", False)),
                    max_completion_tokens=opts_dict.get("max_completion_tokens"),
                )
            elif name in ("mimo", "mimo-omni"):
                options = ThinkingOptions(
                    thinking=opts_dict.get("thinking"),
                    json_output=bool(opts_dict.get("json_output", False)),
                    max_completion_tokens=opts_dict.get("max_completion_tokens"),
                )

        # 如果是预设提供商,取预设为底,用 YAML 配置覆盖
        if name in PRESET_PROVIDERS:
            import dataclasses as _dc

            base = PRESET_PROVIDERS[name]
            overrides: dict[str, Any] = {}
            if "base_url" in prov_dict:
                overrides["base_url"] = prov_dict["base_url"]
            if "api_key" in prov_dict:
                overrides["api_key"] = prov_dict["api_key"]
            if "model" in prov_dict:
                overrides["model"] = prov_dict["model"]
            if options is not None:
                overrides["options"] = options
            if overrides:
                provider = _dc.replace(base, **overrides)
            else:
                provider = base
        else:
            # 自定义提供商
            provider = LLMProvider(
                name=name,
                base_url=prov_dict.get("base_url", ""),
                api_key=prov_dict.get("api_key"),
                api_key_env=prov_dict.get("api_key_env", ""),
                model=prov_dict.get("model", ""),
                provider_type=prov_dict.get("provider_type", "openai"),
                options=options,
            )

        register_provider(provider)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def load_workflow_from_dict(
    config: dict,
    base_dir: str = ".",
    hooks: LifecycleHooks | None = None,
) -> Workflow:
    """从已解析的 config dict 编译 Workflow。

    Args:
        config:  YAML 解析后的 dict(已递归解析 ``${ENV}``)。
        base_dir: Skill 包的基准目录。
        hooks:   生命周期钩子(可选)。

    Returns:
        Workflow: 可执行的 Workflow 对象。
    """
    # 1. 解析 ${ENV}
    config = _resolve_env(config)

    wf_name = config.get("name", "workflow")

    # 1b. 注册 LLM 提供商(在编译 Agent 之前,确保 Agent.model 可路由)
    providers_section = config.get("providers", [])
    if providers_section:
        _compile_providers(providers_section)

    # 2. 编译 Agent
    agent_configs = _compile_agents(config.get("agents", []))

    # 3. 加载 Skill
    skills_section = config.get("skills", [])
    if skills_section:
        from pathlib import Path
        for skill_spec in skills_section:
            skill_path = (
                skill_spec if isinstance(skill_spec, str)
                else skill_spec.get("path", "")
            )
            if not skill_path:
                continue
            # 解析路径:相对 base_dir
            skill_full_path = Path(base_dir) / skill_path
            if skill_full_path.is_dir():
                parent = str(skill_full_path.parent)
                name = skill_full_path.name
                SkillLoader(parent).load(name)

    # 4. 配置 MCP
    mcp_manager = None
    mcp_configs_raw = config.get("mcp_servers", [])
    if mcp_configs_raw:
        mcp_configs = [MCPServerConfig.from_dict(c) for c in mcp_configs_raw]
        mcp_manager = MCPManager(configs=mcp_configs)

    # 5. 编译 Step
    steps_config = config.get("steps", [])
    steps = [
        _compile_step(s, agent_configs) for s in steps_config
    ]

    # 6. 组装 Workflow
    return Workflow(
        name=wf_name,
        steps=steps,
        hooks=hooks,
        mcp_manager=mcp_manager,
    )


def load_workflow(
    yaml_path: str,
    hooks: LifecycleHooks | None = None,
) -> Workflow:
    """从 YAML 文件加载 Workflow。

    Args:
        yaml_path: YAML 文件路径。
        hooks:     生命周期钩子(可选)。

    Returns:
        Workflow: 可执行的 Workflow 对象。

    Raises:
        ImportError: PyYAML 未安装。
        FileNotFoundError: 文件不存在。
    """
    if _yaml is None:
        raise ImportError(
            "YAML 加载需要 PyYAML: pip install pyyaml"
        )

    with open(yaml_path, "r", encoding="utf-8") as f:
        config = _yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError(f"YAML 文件 {yaml_path} 顶层应为 dict")

    base_dir = os.path.dirname(os.path.abspath(yaml_path))
    return load_workflow_from_dict(config, base_dir=base_dir, hooks=hooks)
