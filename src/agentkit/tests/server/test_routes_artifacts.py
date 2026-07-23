"""server.routes.artifacts —— E4 路由层测试。

出口标准（对齐 P1 §E4）:
    - GET /runs/{run_id}/artifacts → 产物清单
    - GET /artifacts/{run_id}/{artifact_id} → 下载（支持 Range）
    - 不存在 → 404
    - gc_sweeper_loop 定时触发
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.runtime.artifact import ArtifactRef
from agentkit.runtime.event import EventLog, EventType, RunEvent
from agentkit.server.routes.artifacts import (
    create_artifact_routes,
    gc_sweeper_loop,
    list_artifacts_from_log,
)


# ---------------------------------------------------------------------------
# 辅助：构造有产物的 EventLog
# ---------------------------------------------------------------------------
def _write_artifact_event(tmp_path, run_id="run_1", artifact_id="art_1",
                          content=b"hello world", step_id="s1"):
    """写一个 ARTIFACT_PRODUCED 事件 + 对应产物文件。"""
    art_path = tmp_path / run_id / "artifacts" / step_id / artifact_id
    art_path.parent.mkdir(parents=True, exist_ok=True)
    art_path.write_bytes(content)

    ref = ArtifactRef(
        id=artifact_id,
        run_id=run_id,
        step_id=step_id,
        uri=str(art_path),
        content_type="text/plain",
        size=len(content),
        md5="d41d8cd98f00b204e9800998ecf8427e",
        summary="test artifact",
        ts=1000000.0,
    )
    log = EventLog(run_id, base_dir=str(tmp_path))
    log.append(RunEvent(
        run_id=run_id,
        seq=1,
        type=EventType.ARTIFACT_PRODUCED,
        step_id=step_id,
        payload=ref.to_dict(),
    ))
    return ref


def _make_app(base_dir: str) -> TestClient:
    app = FastAPI()
    app.include_router(create_artifact_routes(base_dir))
    return TestClient(app)


# ---------------------------------------------------------------------------
# list_artifacts_from_log 辅助函数
# ---------------------------------------------------------------------------
def test_list_artifacts_from_log(tmp_path):
    """从事件日志重建产物引用列表。"""
    _write_artifact_event(tmp_path)
    refs = list_artifacts_from_log("run_1", str(tmp_path))
    assert len(refs) == 1
    assert refs[0]["id"] == "art_1"
    assert refs[0]["step_id"] == "s1"
    assert refs[0]["size"] == len(b"hello world")


def test_list_artifacts_from_log_empty(tmp_path):
    """无产物事件 → 空列表。"""
    refs = list_artifacts_from_log("run_empty", str(tmp_path))
    assert refs == []


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/artifacts
# ---------------------------------------------------------------------------
def test_list_artifacts(tmp_path):
    """run 有产物 → 返回清单,含 size/md5/summary。"""
    _write_artifact_event(tmp_path)
    client = _make_app(str(tmp_path))
    resp = client.get("/api/runs/run_1/artifacts")
    assert resp.status_code == 200
    arts = resp.json()["artifacts"]
    assert len(arts) == 1
    assert arts[0]["id"] == "art_1"
    assert arts[0]["size"] == 11
    assert arts[0]["md5"] != ""
    assert arts[0]["summary"] == "test artifact"


def test_list_artifacts_empty(tmp_path):
    """run 无产物 → 返回空列表。"""
    client = _make_app(str(tmp_path))
    resp = client.get("/api/runs/run_noart/artifacts")
    assert resp.status_code == 200
    assert resp.json()["artifacts"] == []


# ---------------------------------------------------------------------------
# GET /artifacts/{run_id}/{artifact_id}
# ---------------------------------------------------------------------------
def test_download_artifact(tmp_path):
    """GET 下载 → 200 + 文件内容 + 正确 Content-Type。"""
    _write_artifact_event(tmp_path, content=b"hello world")
    client = _make_app(str(tmp_path))
    resp = client.get("/api/artifacts/run_1/art_1")
    assert resp.status_code == 200
    assert resp.content == b"hello world"
    assert "content-disposition" in {k.lower() for k in resp.headers}


def test_download_artifact_range(tmp_path):
    """Range 请求 → 206 Partial Content。"""
    _write_artifact_event(tmp_path, content=b"hello world")
    client = _make_app(str(tmp_path))
    resp = client.get(
        "/api/artifacts/run_1/art_1",
        headers={"range": "bytes=0-4"},
    )
    assert resp.status_code == 206
    assert resp.content == b"hello"
    assert resp.headers["content-range"] == "bytes 0-4/11"
    assert resp.headers["content-length"] == "5"


def test_download_artifact_range_suffix(tmp_path):
    """Range bytes=-5 → 最后 5 字节。"""
    _write_artifact_event(tmp_path, content=b"hello world")
    client = _make_app(str(tmp_path))
    resp = client.get(
        "/api/artifacts/run_1/art_1",
        headers={"range": "bytes=-5"},
    )
    assert resp.status_code == 206
    assert resp.content == b"world"


def test_download_artifact_not_found(tmp_path):
    """不存在 → 404。"""
    _write_artifact_event(tmp_path)
    client = _make_app(str(tmp_path))
    resp = client.get("/api/artifacts/run_1/nonexistent")
    assert resp.status_code == 404


def test_download_artifact_run_not_found(tmp_path):
    """run 不存在 → 404。"""
    client = _make_app(str(tmp_path))
    resp = client.get("/api/artifacts/unknown_run/unknown_art")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# gc_sweeper_loop
# ---------------------------------------------------------------------------
async def test_gc_sweeper_loop(tmp_path, monkeypatch):
    """定时触发后 sweep_once 被调用。"""
    import agentkit.runtime.artifact as art_mod

    call_count = {"n": 0}

    class _MockSweeper:
        def __init__(self, **kwargs):
            pass

        def sweep_once(self):
            call_count["n"] += 1
            return {"deleted_tmp": 0, "deleted_orphan": 0}

    monkeypatch.setattr(art_mod, "GCSweeper", _MockSweeper)

    task = asyncio.create_task(
        gc_sweeper_loop(str(tmp_path), interval=0.01, grace=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count["n"] >= 1
