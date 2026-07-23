"""server.routes.runs —— E2 路由层测试。

出口标准（对齐 P1 §E2）:
    - POST /workflows/{name}/runs → 返回 run_id
    - GET /runs → 列表,含 is_active
    - GET /runs/{run_id} → status + traces + artifacts
    - POST /runs/{run_id}/cancel → cancelled
    - POST /runs/{run_id}/resume → 新 run_id / 400
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agentkit.core.checkpoint import LocalCheckpointStore
from agentkit.runtime.run_manager import RunManager
from agentkit.server.routes.runs import create_run_routes
from agentkit.tools.base import Tool, register


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------
class _EchoTool(Tool):
    name = "test.echo"
    description = "echo params"

    async def call(self, params, ctx):
        return {"echo": params}


class _MediumTool(Tool):
    name = "test.medium"
    description = "sleep 1s"

    async def call(self, params, ctx):
        await asyncio.sleep(1.0)
        return {"done": True}


_ECHO_WF = (
    "name: test_wf\n"
    "steps:\n"
    "  - type: tool\n"
    "    tool: test.echo\n"
    "    id: s1\n"
    "    output: result\n"
)

_INPUTS_WF = (
    "name: inputs_wf\n"
    "inputs:\n"
    "  - input_value\n"
    "steps:\n"
    "  - type: tool\n"
    "    tool: test.echo\n"
    "    id: s1\n"
    "    output: result\n"
    "    params:\n"
    "      key: '{{input_value}}'\n"
)

_CANCEL_WF = (
    "name: cancel_wf\n"
    "steps:\n"
    "  - type: tool\n"
    "    tool: test.medium\n"
    "    id: s1\n"
    "    output: result\n"
)


def _setup(tmp_path):
    """注册工具 + 创建 app/run_manager/workflow 文件。"""
    register(_EchoTool())
    register(_MediumTool())

    workflow_dir = tmp_path / "workflows"
    workflow_dir.mkdir()
    (workflow_dir / "test_wf.yaml").write_text(_ECHO_WF, encoding="utf-8")
    (workflow_dir / "inputs_wf.yaml").write_text(_INPUTS_WF, encoding="utf-8")
    (workflow_dir / "cancel_wf.yaml").write_text(_CANCEL_WF, encoding="utf-8")

    cp_store = LocalCheckpointStore(base_dir=str(tmp_path / "cp"))
    rm = RunManager(checkpoint_store=cp_store, base_dir=str(tmp_path / "runs"))

    app = FastAPI()
    app.include_router(create_run_routes(rm, str(workflow_dir)))
    return app, rm


async def _wait_for_status(client, run_id, target, timeout=5.0):
    """轮询直到 run 达到目标状态。"""
    start = time.time()
    while time.time() - start < timeout:
        resp = await client.get(f"/api/runs/{run_id}")
        if resp.status_code == 404:
            await asyncio.sleep(0.02)
            continue
        status = resp.json().get("status")
        if status == target:
            return resp.json()
        if status in ("failed", "cancelled"):
            return resp.json()
        await asyncio.sleep(0.02)
    raise TimeoutError(f"run {run_id} 未在 {timeout}s 内达到 {target}")


async def _wait_for_run_listed(client, run_id, timeout=5.0):
    """轮询直到 run 出现在列表中（checkpoint 持久化完成）。"""
    start = time.time()
    while time.time() - start < timeout:
        resp = await client.get("/api/runs")
        runs = resp.json().get("runs", [])
        if any(r["run_id"] == run_id for r in runs):
            return runs
        await asyncio.sleep(0.02)
    raise TimeoutError(f"run {run_id} 未在 {timeout}s 内出现在列表")


# ---------------------------------------------------------------------------
# POST /workflows/{name}/runs
# ---------------------------------------------------------------------------
async def test_start_run(tmp_path):
    """POST 后返回 run_id。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/workflows/test_wf/runs")
            assert resp.status_code == 200
            data = resp.json()
            assert "run_id" in data
            assert isinstance(data["run_id"], str)
    finally:
        await rm.shutdown()


async def test_start_run_with_inputs(tmp_path):
    """inputs 正确传入 → run 成功完成。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/workflows/inputs_wf/runs",
                json={"inputs": {"input_value": "hello"}},
            )
            assert resp.status_code == 200
            run_id = resp.json()["run_id"]
            data = await _wait_for_status(client, run_id, "completed")
            assert data["status"] == "completed"
    finally:
        await rm.shutdown()


async def test_start_run_custom_id(tmp_path):
    """指定 run_id → 返回同一 run_id。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/workflows/test_wf/runs",
                json={"run_id": "my_custom_run"},
            )
            assert resp.status_code == 200
            assert resp.json()["run_id"] == "my_custom_run"
    finally:
        await rm.shutdown()


async def test_start_run_workflow_not_found(tmp_path):
    """工作流不存在 → 404。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/workflows/nonexistent/runs")
            assert resp.status_code == 404
    finally:
        await rm.shutdown()


# ---------------------------------------------------------------------------
# GET /runs
# ---------------------------------------------------------------------------
async def test_list_runs(tmp_path):
    """多个 run → 返回全量,含 is_active 标记。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r1 = await client.post("/api/workflows/test_wf/runs")
            r2 = await client.post("/api/workflows/test_wf/runs")
            run_id1 = r1.json()["run_id"]
            run_id2 = r2.json()["run_id"]
            # 轮询等待 checkpoint 持久化
            await _wait_for_run_listed(client, run_id1)
            await _wait_for_run_listed(client, run_id2)
            resp = await client.get("/api/runs")
            assert resp.status_code == 200
            runs = resp.json()["runs"]
            assert len(runs) >= 2
            for r in runs:
                assert "run_id" in r
                assert "is_active" in r
                assert "status" in r
    finally:
        await rm.shutdown()


async def test_list_runs_filter_workflow(tmp_path):
    """?workflow= 过滤正确。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r1 = await client.post("/api/workflows/test_wf/runs")
            r2 = await client.post("/api/workflows/cancel_wf/runs")
            await _wait_for_run_listed(client, r1.json()["run_id"])
            await _wait_for_run_listed(client, r2.json()["run_id"])
            resp = await client.get("/api/runs?workflow=test_wf")
            assert resp.status_code == 200
            runs = resp.json()["runs"]
            assert all(r["workflow_name"] == "test_wf" for r in runs)
            assert len(runs) >= 1
    finally:
        await rm.shutdown()


# ---------------------------------------------------------------------------
# GET /runs/{run_id}
# ---------------------------------------------------------------------------
async def test_get_run_detail(tmp_path):
    """返回 status + completed_steps + traces + artifacts。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/workflows/test_wf/runs")
            run_id = resp.json()["run_id"]
            data = await _wait_for_status(client, run_id, "completed")

            assert data["status"] == "completed"
            assert "completed_steps" in data
            assert "traces" in data
            assert "artifacts" in data
            assert isinstance(data["traces"], list)
            assert isinstance(data["artifacts"], list)
            # echo_tool 应有 step_finished trace
            assert len(data["traces"]) >= 1
    finally:
        await rm.shutdown()


async def test_get_run_detail_not_found(tmp_path):
    """不存在的 run_id → 404。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/runs/nonexistent_run")
            assert resp.status_code == 404
    finally:
        await rm.shutdown()


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/cancel
# ---------------------------------------------------------------------------
async def test_cancel_immediate(tmp_path):
    """?mode=immediate → 200 + cancelled。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/workflows/cancel_wf/runs")
            run_id = resp.json()["run_id"]
            resp = await client.post(
                f"/api/runs/{run_id}/cancel?mode=immediate"
            )
            assert resp.status_code == 200
            assert resp.json()["cancelled"] is True
            assert resp.json()["mode"] == "immediate"
    finally:
        await rm.shutdown()


async def test_cancel_graceful(tmp_path):
    """?mode=graceful → 200 + cancelled。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/workflows/cancel_wf/runs")
            run_id = resp.json()["run_id"]
            resp = await client.post(
                f"/api/runs/{run_id}/cancel?mode=graceful"
            )
            assert resp.status_code == 200
            assert resp.json()["cancelled"] is True
    finally:
        await rm.shutdown()


async def test_cancel_unknown(tmp_path):
    """不存在的 run_id → 404。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/runs/nonexistent/cancel")
            assert resp.status_code == 404
    finally:
        await rm.shutdown()


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/resume
# ---------------------------------------------------------------------------
async def test_resume_wrong_status(tmp_path):
    """completed → resume → 400。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/workflows/test_wf/runs")
            run_id = resp.json()["run_id"]
            await _wait_for_status(client, run_id, "completed")

            resp = await client.post(f"/api/runs/{run_id}/resume")
            assert resp.status_code == 400
    finally:
        await rm.shutdown()


async def test_resume(tmp_path):
    """interrupted → resume → 200。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/workflows/test_wf/runs")
            run_id = resp.json()["run_id"]
            await _wait_for_status(client, run_id, "completed")

            # 手动改为 interrupted
            cp = await rm._checkpoint_store.load(run_id)
            cp.status = "interrupted"
            await rm._checkpoint_store.save(cp)

            resp = await client.post(f"/api/runs/{run_id}/resume")
            assert resp.status_code == 200
            assert "run_id" in resp.json()
    finally:
        await rm.shutdown()


async def test_resume_unknown(tmp_path):
    """不存在的 run_id → 404。"""
    app, rm = _setup(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/runs/nonexistent/resume")
            assert resp.status_code == 404
    finally:
        await rm.shutdown()
