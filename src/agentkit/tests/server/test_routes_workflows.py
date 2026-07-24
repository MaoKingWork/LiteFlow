"""server.routes.workflows —— E1 路由层测试。

出口标准（对齐 P1 §E1）:
    - PUT 有效 YAML → 200,文件创建
    - PUT 无效 YAML → 400 + diagnostics
    - PUT 含 ${ENV} → 保存后占位符保留
    - POST validate → is_valid + diagnostics
    - GET meta/step-types → 类型 + 字段 schema + 容器字段
    - GET meta/tools → 工具 + role/execution/param_model_schema
    - GET meta/agents → ${ENV} 保留
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.server.routes.workflows import create_workflow_routes


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _make_app(workflow_dir: str) -> TestClient:
    app = FastAPI()
    app.include_router(create_workflow_routes(workflow_dir))
    return TestClient(app)


_VALID_YAML = (
    "name: test_wf\n"
    "steps:\n"
    "  - type: llm\n"
    "    agent: a\n"
    "    id: s1\n"
    "agents:\n"
    "  - name: a\n"
    "    provider: deepseek\n"
)


# ---------------------------------------------------------------------------
# PUT /workflows/{name}
# ---------------------------------------------------------------------------
def test_put_workflow_valid(tmp_path):
    """有效 YAML → 200,文件创建。"""
    client = _make_app(str(tmp_path))
    resp = client.put(
        "/api/workflows/test_wf",
        content=_VALID_YAML,
        headers={"content-type": "text/yaml"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "test_wf"
    assert (tmp_path / "test_wf.yaml").exists()


def test_put_workflow_valid_json(tmp_path):
    """JSON {"yaml": "..."} 格式 → 200。"""
    client = _make_app(str(tmp_path))
    resp = client.put("/api/workflows/wf2", json={"yaml": _VALID_YAML})
    assert resp.status_code == 200
    assert (tmp_path / "wf2.yaml").exists()


def test_put_workflow_invalid(tmp_path):
    """无效 YAML → 400 + diagnostics 含 path/severity/code。"""
    client = _make_app(str(tmp_path))
    invalid_yaml = "steps:\n  - id: s1\n"  # 缺 name, 缺 type
    resp = client.put(
        "/api/workflows/bad_wf",
        content=invalid_yaml,
        headers={"content-type": "text/yaml"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["is_valid"] is False
    codes = [d["code"] for d in data["diagnostics"]]
    assert "root.name_missing" in codes
    assert "step.type_missing" in codes
    # 每条 diagnostic 含 path/severity/code
    for d in data["diagnostics"]:
        assert "path" in d
        assert "severity" in d
        assert "code" in d


def test_put_workflow_env_preserved(tmp_path):
    """含 ${API_KEY} 的 YAML → 保存后占位符原样保留。"""
    client = _make_app(str(tmp_path))
    yaml_with_env = (
        "name: wf_env\n"
        "steps:\n"
        "  - type: llm\n"
        "    agent: a\n"
        "    id: s1\n"
        "agents:\n"
        "  - name: a\n"
        "    provider: deepseek\n"
        "    system: 'Key: ${API_KEY}'\n"
    )
    resp = client.put(
        "/api/workflows/wf_env",
        content=yaml_with_env,
        headers={"content-type": "text/yaml"},
    )
    assert resp.status_code == 200
    saved = (tmp_path / "wf_env.yaml").read_text(encoding="utf-8")
    assert "${API_KEY}" in saved


def test_put_workflow_bad_name(tmp_path):
    """name 含非法字符(如路径遍历点) → 400。"""
    client = _make_app(str(tmp_path))
    # 用单段非法名(含 .)测试服务端正则校验
    # ../etc 等多段路径会被路由层拦截返回 404,不到 handler
    resp = client.put(
        "/api/workflows/bad.name",
        content=_VALID_YAML,
        headers={"content-type": "text/yaml"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /workflows/validate
# ---------------------------------------------------------------------------
def test_validate_returns_diagnostics(tmp_path):
    """校验结果含 is_valid + diagnostics 列表。"""
    client = _make_app(str(tmp_path))
    resp = client.post(
        "/api/workflows/validate",
        content=_VALID_YAML,
        headers={"content-type": "text/yaml"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is True
    assert isinstance(data["diagnostics"], list)


def test_validate_returns_diagnostics_invalid(tmp_path):
    """无效 workflow → is_valid=False + diagnostics 含 code。"""
    client = _make_app(str(tmp_path))
    resp = client.post(
        "/api/workflows/validate",
        content="steps:\n  - id: s1\n",
        headers={"content-type": "text/yaml"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is False
    assert len(data["diagnostics"]) > 0


# ---------------------------------------------------------------------------
# GET /meta/step-types
# ---------------------------------------------------------------------------
def test_meta_step_types(tmp_path):
    """返回所有已注册 Step 类型,含字段 schema。"""
    client = _make_app(str(tmp_path))
    resp = client.get("/api/meta/step-types")
    assert resp.status_code == 200
    data = resp.json()
    names = [t["name"] for t in data["types"]]
    assert "llm" in names
    for t in data["types"]:
        assert "fields" in t
        assert "container_fields" in t
        for f in t["fields"]:
            assert "name" in f
            assert "type" in f
            assert "required" in f


def test_meta_step_types_container(tmp_path):
    """ConditionStep 含 then/else 容器字段。"""
    client = _make_app(str(tmp_path))
    resp = client.get("/api/meta/step-types")
    data = resp.json()
    cond = next(t for t in data["types"] if t["name"] == "condition")
    container_names = [c["name"] for c in cond["container_fields"]]
    assert "then" in container_names
    assert "else" in container_names

    loop = next(t for t in data["types"] if t["name"] == "loop")
    loop_containers = [c["name"] for c in loop["container_fields"]]
    assert "step" in loop_containers

    parallel = next(t for t in data["types"] if t["name"] == "parallel")
    par_containers = [c["name"] for c in parallel["container_fields"]]
    assert "branches" in par_containers


# ---------------------------------------------------------------------------
# GET /meta/tools
# ---------------------------------------------------------------------------
def test_meta_tools(tmp_path):
    """返回所有工具,含 role/execution/param_model_schema。"""
    from agentkit.tools.base import Tool, register

    class _Params(__import__("pydantic").BaseModel):
        x: int

    class _SchemaTool(Tool):
        name = "test.schema_tool"
        description = "test tool with schema"
        role = "action"

        @property
        def param_model(self):
            return _Params

        async def call(self, params, ctx):
            return {}

    register(_SchemaTool())
    try:
        client = _make_app(str(tmp_path))
        resp = client.get("/api/meta/tools")
        assert resp.status_code == 200
        data = resp.json()
        names = [t["name"] for t in data["tools"]]
        assert "test.schema_tool" in names
        tool = next(t for t in data["tools"] if t["name"] == "test.schema_tool")
        assert tool["role"] == "action"
        assert tool["execution"] == "inline"
        assert tool["param_model_schema"] is not None
        assert "properties" in tool["param_model_schema"]
    finally:
        pass


def test_meta_tools_empty(tmp_path):
    """无工具注册时返回空列表。"""
    client = _make_app(str(tmp_path))
    resp = client.get("/api/meta/tools")
    assert resp.status_code == 200
    # 可能有内置工具,只验证结构
    assert isinstance(resp.json()["tools"], list)


# ---------------------------------------------------------------------------
# GET /meta/agents
# ---------------------------------------------------------------------------
def test_meta_agents_env_preserved(tmp_path):
    """agents 段含 ${ENV} → 返回值保留原文。"""
    yaml_file = tmp_path / "wf.yaml"
    yaml_file.write_text(
        "name: wf\n"
        "steps:\n"
        "  - type: llm\n"
        "    agent: a\n"
        "    id: s1\n"
        "agents:\n"
        "  - name: a\n"
        "    provider: deepseek\n"
        "    system: 'Token: ${SECRET_TOKEN}'\n",
        encoding="utf-8",
    )
    client = _make_app(str(tmp_path))
    resp = client.get("/api/meta/agents")
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert len(agents) >= 1
    a = next(a for a in agents if a["name"] == "a")
    assert "${SECRET_TOKEN}" in a["system"]


def test_meta_agents_empty(tmp_path):
    """无 YAML 文件时返回空列表。"""
    client = _make_app(str(tmp_path))
    resp = client.get("/api/meta/agents")
    assert resp.status_code == 200
    assert resp.json()["agents"] == []
