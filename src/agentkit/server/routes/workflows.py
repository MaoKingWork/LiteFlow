"""server.routes.workflows —— 工作流 CRUD + 校验 + 内省接口（E1）。

端点:
    GET    /api/workflows             工作流列表（名称 + 更新时间）
    GET    /api/workflows/{name}      读取定义（config JSON + raw YAML 原文）
    PUT    /api/workflows/{name}      保存定义（YAML 文本 → 文件）
    DELETE /api/workflows/{name}      删除定义文件
    POST   /api/workflows/validate    校验,返回 diagnostics[]
    GET    /api/meta/step-types       StepRegistry 全类型 + schema
    GET    /api/meta/tools            ToolRegistry 全工具 + schema
    GET    /api/meta/agents           YAML agents 段原文（${ENV} 占位符保留）

设计原则:
    - 懒加载 fastapi:模块顶层不 import fastapi,仅在工厂函数内导入
    - ENV 占位符保留:PUT 只写文件不解析 ${ENV};meta/agents 返回 safe_load 原文
    - 路径安全:name 仅允许 [A-Za-z0-9_-],防路径遍历

注意:本模块 **不用** ``from __future__ import annotations``
（Pydantic + FastAPI 局部类解析陷阱）。
"""

import glob
import inspect
import json
import os
import re

from agentkit.steps.base import _GLOBAL_STEP_REGISTRY
from agentkit.tools.base import get_tool, list_tools
from agentkit.yaml.validator import validate_workflow

__all__ = ["create_workflow_routes"]


# name 合法字符校验（防路径遍历）
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


# 容器型 Step 的嵌套字段（YAML key → 容器类型）
_CONTAINER_FIELDS = {
    "condition": [
        {"name": "then", "kind": "steps"},
        {"name": "else", "kind": "steps"},
    ],
    "loop": [
        {"name": "step", "kind": "step"},
    ],
    "parallel": [
        {"name": "branches", "kind": "steps"},
    ],
}


def _type_name(annotation) -> str:
    """将参数注解转为可读字符串。"""
    if annotation is inspect.Parameter.empty:
        return "any"
    if isinstance(annotation, type):
        return annotation.__name__
    # typing 特殊形式（Optional[str] 等）
    return str(annotation).replace("typing.", "")


def _serialize_default(value) -> object:
    """序列化默认值为 JSON 兼容值。"""
    if value is inspect.Parameter.empty:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # 复杂对象用字符串表示
    return repr(value)


def _extract_step_fields(cls) -> list:
    """从 Step 类 __init__ 签名提取字段 schema。"""
    sig = inspect.signature(cls.__init__)
    fields = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        required = param.default is inspect.Parameter.empty
        fields.append({
            "name": name,
            "type": _type_name(param.annotation),
            "required": required,
            "default": _serialize_default(param.default),
        })
    return fields


def _parse_workflow_body(text: str):
    """解析请求体为 (config_dict, raw_yaml_text)。

    支持两种格式:
        - JSON: {"yaml": "..."} 或直接 workflow dict
        - YAML 文本

    Returns:
        tuple: (config, raw_yaml)。config 为解析后的 dict;
               raw_yaml 为写入文件的 YAML 文本（保留原文/注释）。
    """
    # 先尝试 JSON
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = None

    if isinstance(data, dict):
        if "yaml" in data and isinstance(data["yaml"], str):
            raw_yaml = data["yaml"]
            import yaml
            config = yaml.safe_load(raw_yaml)
        else:
            # 直接是 workflow dict
            config = data
            import yaml
            raw_yaml = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    else:
        # 非JSON,当YAML文本
        import yaml
        config = yaml.safe_load(text)
        raw_yaml = text

    if not isinstance(config, dict):
        raise ValueError("请求体必须解析为 YAML/JSON dict")
    return config, raw_yaml


def create_workflow_routes(workflow_dir: str, prefix: str = "/api"):
    """创建工作流路由。

    懒加载 fastapi。返回 APIRouter。

    Args:
        workflow_dir: 工作流 YAML 文件目录。
        prefix:       URL 前缀,默认 ``/api``。
    """
    try:
        from fastapi import APIRouter, HTTPException, Request
    except ImportError as e:
        raise ImportError(
            "Server 需要 fastapi: pip install agentkit[server]"
        ) from e

    router = APIRouter(prefix=prefix, tags=["workflows"])

    # ------------------------------------------------------------------
    # GET /workflows —— 工作流列表
    # ------------------------------------------------------------------
    @router.get("/workflows")
    async def list_workflows():
        """列出 workflow_dir 下全部工作流 YAML。

        返回 ``{"workflows": [{name, path, updated_at}]}``,
        ``updated_at`` 为文件 mtime（秒）。``${ENV}`` 占位符不在此解析。
        """
        items = []
        pattern = os.path.join(workflow_dir, "*.yaml")
        for filepath in sorted(glob.glob(pattern)):
            name = os.path.splitext(os.path.basename(filepath))[0]
            if not _NAME_PATTERN.match(name):
                continue
            try:
                mtime = os.path.getmtime(filepath)
            except OSError:
                continue
            items.append({"name": name, "path": filepath, "updated_at": mtime})
        return {"workflows": items}

    # ------------------------------------------------------------------
    # GET /workflows/{name} —— 读取工作流定义
    # ------------------------------------------------------------------
    @router.get("/workflows/{name}")
    async def get_workflow(name: str):
        """读取指定工作流定义。

        返回 ``{name, config, yaml}``:``config`` 为 ``safe_load`` 后的
        JSON 同构结构（前端文档模型）,``yaml`` 为文件原文（导出用）。
        ``${ENV}`` 占位符保留原文,永不回传解析值。
        """
        if not _NAME_PATTERN.match(name):
            raise HTTPException(
                status_code=400,
                detail="name 仅允许字母、数字、下划线、横线",
            )
        filepath = os.path.join(workflow_dir, f"{name}.yaml")
        if not os.path.exists(filepath):
            raise HTTPException(
                status_code=404,
                detail=f"工作流 {name!r} 不存在",
            )
        import yaml

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                raw_yaml = f.read()
            config = yaml.safe_load(raw_yaml)
        except (OSError, yaml.YAMLError) as e:
            raise HTTPException(
                status_code=400,
                detail=f"工作流文件解析失败: {e}",
            )
        if not isinstance(config, dict):
            raise HTTPException(
                status_code=400,
                detail="工作流文件顶层应为 dict",
            )
        return {"name": name, "config": config, "yaml": raw_yaml}

    # ------------------------------------------------------------------
    # DELETE /workflows/{name} —— 删除工作流定义
    # ------------------------------------------------------------------
    @router.delete("/workflows/{name}")
    async def delete_workflow(name: str):
        """删除指定工作流 YAML 文件。"""
        if not _NAME_PATTERN.match(name):
            raise HTTPException(
                status_code=400,
                detail="name 仅允许字母、数字、下划线、横线",
            )
        filepath = os.path.join(workflow_dir, f"{name}.yaml")
        if not os.path.exists(filepath):
            raise HTTPException(
                status_code=404,
                detail=f"工作流 {name!r} 不存在",
            )
        os.remove(filepath)
        return {"name": name, "deleted": True}

    # ------------------------------------------------------------------
    # PUT /workflows/{name} —— 保存工作流定义
    # ------------------------------------------------------------------
    @router.put("/workflows/{name}")
    async def put_workflow(name: str, request: Request):
        """保存工作流 YAML 定义到文件。

        先校验,有 errors 返回 400 + diagnostics;通过则写文件。
        保留 ${ENV} 占位符原文,不在此解析。
        """
        if not _NAME_PATTERN.match(name):
            raise HTTPException(
                status_code=400,
                detail="name 仅允许字母、数字、下划线、横线",
            )

        body = await request.body()
        text = body.decode("utf-8")
        try:
            config, raw_yaml = _parse_workflow_body(text)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        report = validate_workflow(config)
        if not report.is_valid:
            return _json_response(
                status_code=400,
                content=report.to_api_response(),
            )

        # 写文件
        os.makedirs(workflow_dir, exist_ok=True)
        filepath = os.path.join(workflow_dir, f"{name}.yaml")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(raw_yaml)

        return {"name": name, "path": filepath}

    # ------------------------------------------------------------------
    # POST /workflows/validate —— 校验工作流
    # ------------------------------------------------------------------
    @router.post("/workflows/validate")
    async def validate_workflow_endpoint(request: Request):
        """校验 YAML 工作流定义,返回 diagnostics。"""
        body = await request.body()
        text = body.decode("utf-8")
        try:
            config, _ = _parse_workflow_body(text)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        report = validate_workflow(config)
        return report.to_api_response()

    # ------------------------------------------------------------------
    # GET /meta/step-types —— Step 类型内省
    # ------------------------------------------------------------------
    @router.get("/meta/step-types")
    async def get_step_types():
        """返回所有已注册 Step 类型及其字段 schema。"""
        types = []
        for type_name in _GLOBAL_STEP_REGISTRY.list():
            try:
                cls = _GLOBAL_STEP_REGISTRY.get(type_name)
            except KeyError:
                continue
            types.append({
                "name": type_name,
                "fields": _extract_step_fields(cls),
                "container_fields": _CONTAINER_FIELDS.get(type_name, []),
            })
        return {"types": types}

    # ------------------------------------------------------------------
    # GET /meta/tools —— 工具内省
    # ------------------------------------------------------------------
    @router.get("/meta/tools")
    async def get_tools():
        """返回所有已注册工具及其 schema。"""
        tools = []
        for name in list_tools():
            try:
                tool = get_tool(name)
            except KeyError:
                continue
            param_schema = None
            pm = tool.param_model
            if pm is not None:
                try:
                    param_schema = pm.model_json_schema()
                except Exception:
                    param_schema = None
            tools.append({
                "name": name,
                "role": getattr(tool, "role", "action"),
                "execution": getattr(tool, "execution", "inline"),
                "description": getattr(tool, "description", ""),
                "param_model_schema": param_schema,
            })
        return {"tools": tools}

    # ------------------------------------------------------------------
    # GET /meta/agents —— YAML agents 段原文（ENV 占位符保留）
    # ------------------------------------------------------------------
    @router.get("/meta/agents")
    async def get_agents():
        """读取 workflow_dir 下所有 YAML 的 agents 段。

        ${ENV} 占位符保留原文（safe_load 不解析 ${ENV}）。
        """
        import yaml

        agents = []
        pattern = os.path.join(workflow_dir, "*.yaml")
        for filepath in sorted(glob.glob(pattern)):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(config, dict):
                continue
            wf_agents = config.get("agents", [])
            if isinstance(wf_agents, list):
                for a in wf_agents:
                    if isinstance(a, dict):
                        agents.append(a)
        return {"agents": agents}

    return router


def _json_response(status_code: int, content: dict):
    """构造 JSON 响应（懒加载 starlette）。"""
    from starlette.responses import JSONResponse
    return JSONResponse(status_code=status_code, content=content)
