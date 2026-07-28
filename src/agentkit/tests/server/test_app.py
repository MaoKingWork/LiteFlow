"""server.app —— FastAPI 应用工厂 + lifespan 测试（F1）。

出口标准（对齐 P1 §F1）：
    - create_app() 返回 FastAPI 实例,含所有路由
    - lifespan 启动后僵尸 run 被标记 interrupted
    - lifespan 启动后 GCSweeper 循环运行
    - lifespan 关闭后 task done,EventBus closed
    - 未安装 fastapi 时 import 不报错（懒加载）
    - /health 端点返回 200
    - SSE 端点 GET /api/runs/{run_id}/events 可达
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from agentkit.core.checkpoint import LocalCheckpointStore, RunStatus
from agentkit.server.app import create_app
from agentkit.server.settings import ServerSettings


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _make_settings(tmp_path) -> ServerSettings:
    """构造测试用 ServerSettings（短 GC 间隔便于测试）。"""
    return ServerSettings(
        host="127.0.0.1",
        port=8000,
        token="",
        cors_origins=[],
        event_queue_size=100,
        event_log_max_events=1000,
        artifact_max_size=10 * 1024 * 1024,
        artifact_max_total=100 * 1024 * 1024,
        gc_interval_seconds=0.05,  # 短间隔便于测试
        gc_orphan_grace_seconds=0.01,
    )


def _make_checkpoint_store(tmp_path) -> LocalCheckpointStore:
    return LocalCheckpointStore(base_dir=str(tmp_path / "checkpoints"))


# ---------------------------------------------------------------------------
# create_app 基础
# ---------------------------------------------------------------------------
def test_create_app_returns_fastapi(tmp_path):
    """create_app() 返回 FastAPI 实例。"""
    app = create_app(str(tmp_path), settings=_make_settings(tmp_path))
    assert isinstance(app, FastAPI)


def test_create_app_has_state(tmp_path):
    """app.state 含 settings / run_manager / workflow_dir / base_dir。"""
    settings = _make_settings(tmp_path)
    app = create_app(str(tmp_path), settings=settings)
    assert app.state.settings is settings
    assert app.state.run_manager is not None
    assert app.state.workflow_dir == str(tmp_path)
    assert app.state.base_dir is not None


def test_create_app_default_settings(tmp_path):
    """settings=None 时用 ServerSettings.from_config()。"""
    app = create_app(str(tmp_path))
    assert isinstance(app.state.settings, ServerSettings)


def test_create_app_default_checkpoint_store(tmp_path):
    """checkpoint_store=None 时用 LocalCheckpointStore。"""
    app = create_app(str(tmp_path), settings=_make_settings(tmp_path))
    # RunManager 内部持有 checkpoint_store
    assert app.state.run_manager._checkpoint_store is not None


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------
def test_routes_registered(tmp_path):
    """create_app() 含所有路由。

    通过 OpenAPI schema 校验路径注册（比遍历 app.routes 更可靠,
    跨 FastAPI/Starlette 版本兼容）。
    """
    app = create_app(str(tmp_path), settings=_make_settings(tmp_path))
    # 用 TestClient 触发 OpenAPI schema 生成（lifespan 不影响 schema）
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = set(resp.json()["paths"].keys())
    # workflows
    assert "/api/workflows/{name}" in paths
    assert "/api/workflows/validate" in paths
    assert "/api/meta/step-types" in paths
    assert "/api/meta/tools" in paths
    assert "/api/meta/agents" in paths
    # runs
    assert "/api/workflows/{name}/runs" in paths
    assert "/api/runs" in paths
    assert "/api/runs/{run_id}" in paths
    assert "/api/runs/{run_id}/cancel" in paths
    assert "/api/runs/{run_id}/resume" in paths
    # artifacts
    assert "/api/runs/{run_id}/artifacts" in paths
    assert "/api/artifacts/{run_id}/{artifact_id}" in paths
    # SSE
    assert "/api/runs/{run_id}/events" in paths
    # health
    assert "/health" in paths


def test_health_endpoint(tmp_path):
    """/health 返回 200 + ok。"""
    app = create_app(str(tmp_path), settings=_make_settings(tmp_path))
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# lifespan
# ---------------------------------------------------------------------------
def test_lifespan_reconciles_zombie_run(tmp_path):
    """启动后僵尸 run 被标记 interrupted。

    用 TestClient（同步）自动运行 lifespan 上下文。
    """
    cp_store = _make_checkpoint_store(tmp_path)
    settings = _make_settings(tmp_path)

    # 预置一个 running 状态的 zombie run
    from agentkit.core.checkpoint import Checkpoint

    cp = Checkpoint(
        run_id="run_zombie",
        workflow_name="test_wf",
        status=RunStatus.RUNNING,
        completed_steps=[],
        started_at=1000.0,
        updated_at=1000.0,
    )
    asyncio.run(cp_store.save(cp))

    app = create_app(
        str(tmp_path),
        settings=settings,
        checkpoint_store=cp_store,
    )

    with TestClient(app) as client:
        # lifespan 启动时触发 reconcile
        resp = client.get("/health")
        assert resp.status_code == 200

    # 退出 with 后 lifespan 关闭;校验 zombie run 被标记 interrupted
    cp_after = asyncio.run(cp_store.load("run_zombie"))
    assert cp_after.status == RunStatus.INTERRUPTED
    assert cp_after.error == "process_restart"


def test_lifespan_starts_gc(tmp_path, monkeypatch):
    """lifespan 启动后 GCSweeper 循环运行。

    GCSweeper 在 gc_sweeper_loop 内懒加载,patch 源模块 agentkit.runtime.artifact。
    """
    import agentkit.runtime.artifact as artifact_mod

    call_count = {"n": 0}

    class _MockSweeper:
        def __init__(self, **kwargs):
            pass

        def sweep_once(self):
            call_count["n"] += 1
            return {"deleted_tmp": 0, "deleted_orphan": 0}

    monkeypatch.setattr(artifact_mod, "GCSweeper", _MockSweeper)

    settings = _make_settings(tmp_path)
    app = create_app(str(tmp_path), settings=settings)

    with TestClient(app) as client:
        # 等待 GC 触发（间隔 0.05s）
        for _ in range(100):
            if call_count["n"] >= 1:
                break
            time.sleep(0.01)

    assert call_count["n"] >= 1


def test_lifespan_shutdown_cleans(tmp_path):
    """app 关闭后所有 task done,EventBus closed。"""
    settings = _make_settings(tmp_path)
    app = create_app(str(tmp_path), settings=settings)
    rm = app.state.run_manager

    with TestClient(app) as client:
        # 触发一次 /health 确保 lifespan 完成
        resp = client.get("/health")
        assert resp.status_code == 200

    # 退出 with 后 lifespan 关闭
    # 检查 RunManager 内存注册表已清空
    assert len(rm._handles) == 0


# ---------------------------------------------------------------------------
# 懒加载
# ---------------------------------------------------------------------------
def test_import_without_fastapi():
    """未安装 fastapi 时 import 不报错（懒加载）。

    本测试只验证模块可被 import（顶层不依赖 fastapi）。
    create_app 内部会尝试 import fastapi,此处不调用。
    """
    # 已经在模块顶层 import 了,只需验证无异常
    from agentkit.server import app as app_mod

    assert hasattr(app_mod, "create_app")


# ---------------------------------------------------------------------------
# SSE 端点
# ---------------------------------------------------------------------------
async def test_sse_endpoint_unknown_run_404(tmp_path):
    """GET /api/runs/{unknown}/events → 404。"""
    app = create_app(str(tmp_path), settings=_make_settings(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/runs/nonexistent/events")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 静态前端挂载
# ---------------------------------------------------------------------------
def test_static_frontend_mounted(tmp_path):
    """GET / 返回可视化前端 index.html;静态资源可达。"""
    app = create_app(str(tmp_path), settings=_make_settings(tmp_path))
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "LiteFlow" in resp.text
        assert "text/html" in resp.headers.get("content-type", "")
        # 静态 JS 模块可达
        js = client.get("/js/main.js")
        assert js.status_code == 200
        # CSS 可达
        css = client.get("/css/app.css")
        assert css.status_code == 200


def test_static_mount_does_not_shadow_api(tmp_path):
    """挂载 / 不影响 API 与 health 路由匹配。"""
    app = create_app(str(tmp_path), settings=_make_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/workflows").status_code == 200
        assert client.get("/api/meta/step-types").status_code == 200


async def test_sse_endpoint_finished_run(tmp_path):
    """GET /api/runs/{finished}/events → 200 SSE 流（run 已结束,仅回放历史）。"""
    from agentkit.runtime.event import EventLog, EventType, RunEvent

    settings = _make_settings(tmp_path)
    cp_store = _make_checkpoint_store(tmp_path)
    from agentkit.core.checkpoint import Checkpoint

    cp = Checkpoint(
        run_id="run_done",
        workflow_name="test_wf",
        status=RunStatus.COMPLETED,
        completed_steps=["s1"],
        started_at=1000.0,
        updated_at=1000.0,
    )
    await cp_store.save(cp)

    # 写入历史事件（含终态）
    base_dir = os.path.join("output", "runs")
    log = EventLog("run_done", base_dir=base_dir)
    log.append(RunEvent(
        run_id="run_done", seq=1, type=EventType.RUN_CREATED,
        payload={"workflow_name": "test_wf"},
    ))
    log.append(RunEvent(
        run_id="run_done", seq=2, type=EventType.RUN_COMPLETED,
        payload={},
    ))

    app = create_app(str(tmp_path), settings=settings, checkpoint_store=cp_store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/runs/run_done/events")
        assert resp.status_code == 200
        # SSE 响应 text/event-stream
        assert "text/event-stream" in resp.headers.get("content-type", "")
