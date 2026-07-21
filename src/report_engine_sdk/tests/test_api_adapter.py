"""Tests for ``report_engine_sdk.adapters.api_router``.

The ``generate_report_impl`` tests exercise the pure-Python core logic without
``fastapi`` installed. The ``create_api_router`` / ``create_app`` tests use
``pytest.importorskip`` to skip when the optional ``api`` extra (fastapi) or
``httpx`` (required by ``fastapi.testclient.TestClient``) is missing.

A minimal pack ``demo_pack`` with one report ``demo`` (``name: string``
required, ``rules=[]``, default template ``# Hi {{ name }}``) is built inside
a pytest ``tmp_path`` fixture directory. The report's global id is
``demo_pack:demo``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from report_engine_sdk.adapters.api_router import (
    create_api_router,
    create_app,
    generate_report_impl,
)
from report_engine_sdk.core.engine import ReportEngine
from report_engine_sdk.storage.memory import MemoryStorage


def _setup_engine(tmp_path: Path) -> ReportEngine:
    """Build a :class:`ReportEngine` rooted at ``tmp_path``.

    Creates ``packs/demo_pack/pack.json`` with a single report ``demo``
    (``name`` required, empty ``rules``, default template rendering
    ``# Hi {{ name }}``) plus the supporting template under
    ``packs/demo_pack/templates/``.
    """
    pack_dir = tmp_path / "packs" / "demo_pack"
    pack_dir.mkdir(parents=True, exist_ok=True)

    pack_json = {
        "pack_id": "demo_pack",
        "purpose": "demo",
        "version": "0.1.0",
        "owner": "tests",
        "reports": {
            "demo": {
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                "rules": [],
                "templates": {"default": {"path": "demo.md"}},
            }
        },
    }
    (pack_dir / "pack.json").write_text(
        json.dumps(pack_json), encoding="utf-8"
    )

    templates_dir = pack_dir / "templates"
    templates_dir.mkdir(exist_ok=True)
    (templates_dir / "demo.md").write_text(
        "# Hi {{ name }}", encoding="utf-8"
    )

    return ReportEngine(str(tmp_path), MemoryStorage())


# ---------------------------------------------------------------------------
# generate_report_impl — pure Python, no fastapi required.
# ---------------------------------------------------------------------------


def test_generate_report_impl_success(tmp_path: Path) -> None:
    """A valid facts dict produces a success result with file_uri and preview."""
    engine = _setup_engine(tmp_path)

    result = generate_report_impl(
        engine, "demo_pack:demo", {"name": "Alice"}, "default"
    )

    assert result["success"] is True
    assert isinstance(result["file_uri"], str)
    assert result["file_uri"].startswith("memory://")
    assert result["preview"] == "# Hi Alice"


def test_generate_report_impl_validation_failure(tmp_path: Path) -> None:
    """Missing required fields yield a structured validation failure."""
    engine = _setup_engine(tmp_path)

    result = generate_report_impl(
        engine, "demo_pack:demo", {"wrong": "x"}, "default"
    )

    assert result["success"] is False
    assert "file_uri" not in result
    assert "preview" not in result
    assert isinstance(result["error"], dict)
    assert "missing_fields" in result["error"]
    assert "name" in result["error"]["missing_fields"]


def test_generate_report_impl_unknown_report(tmp_path: Path) -> None:
    """An unknown report_id produces a structured PackError-shaped failure."""
    engine = _setup_engine(tmp_path)

    result = generate_report_impl(
        engine, "nonexistent", {}, "default"
    )

    assert result["success"] is False
    assert "file_uri" not in result
    assert "preview" not in result
    assert isinstance(result["error"], dict)
    assert "message" in result["error"]
    assert "nonexistent" in result["error"]["message"]


# ---------------------------------------------------------------------------
# create_api_router / create_app — fastapi required.
# ---------------------------------------------------------------------------


def test_create_api_router_missing_fastapi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_api_router`` raises ImportError with install hint when fastapi is absent.

    Simulates a missing fastapi by setting ``sys.modules["fastapi"]`` to
    ``None``, which causes ``from fastapi import ...`` to raise ImportError.
    This test runs regardless of whether fastapi is actually installed.
    """
    # Force the lazy import inside create_api_router to fail. Setting the
    # module entry to None tells Python's import machinery the module is
    # explicitly absent.
    monkeypatch.setitem(sys.modules, "fastapi", None)

    # A throwaway engine is not even constructed because the import fails
    # before any engine use. We pass a sentinel to make the intent clear.
    sentinel: Any = None

    with pytest.raises(ImportError) as exc_info:
        create_api_router(sentinel)  # type: ignore[arg-type]

    assert "report-engine-sdk[api]" in str(exc_info.value)


def test_create_api_router(tmp_path: Path) -> None:
    """The router exposes ``/generate`` and ``/reports`` and behaves end-to-end."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")  # required by fastapi.testclient.TestClient
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    engine = _setup_engine(tmp_path)
    router = create_api_router(engine)

    # Verify the router has routes (paths include the prefix since the router
    # was constructed with prefix="/api/reports").
    route_paths = {route.path for route in router.routes}
    assert "/api/reports/generate" in route_paths
    assert "/api/reports/reports" in route_paths

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    # Happy path: success=True, preview contains the rendered name.
    response = client.post(
        "/api/reports/generate",
        json={
            "report_id": "demo_pack:demo",
            "facts": {"name": "Alice"},
            "view": "default",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["file_uri"].startswith("memory://")
    assert "Alice" in body["preview"]

    # Validation failure: missing required field ``name``.
    response = client.post(
        "/api/reports/generate",
        json={
            "report_id": "demo_pack:demo",
            "facts": {"wrong": "x"},
            "view": "default",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["error"] is not None
    assert "missing_fields" in body["error"]
    assert "name" in body["error"]["missing_fields"]

    # Unknown report_id: 404.
    response = client.post(
        "/api/reports/generate",
        json={
            "report_id": "nonexistent",
            "facts": {},
            "view": "default",
        },
    )
    assert response.status_code == 404

    # List reports.
    response = client.get("/api/reports/reports")
    assert response.status_code == 200
    assert response.json() == {"reports": ["demo_pack:demo"]}


def test_create_app(tmp_path: Path) -> None:
    """``create_app`` returns a FastAPI instance with the report router mounted."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")  # required by fastapi.testclient.TestClient
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    engine = _setup_engine(tmp_path)
    app = create_app(engine)

    assert isinstance(app, FastAPI)

    # Verify the router is mounted by issuing real requests against the
    # endpoints (newer FastAPI versions wrap included routers in
    # ``_IncludedRouter`` objects without a ``.path`` attribute, so route
    # introspection is not portable across versions; HTTP probing is).
    client = TestClient(app)

    response = client.get("/api/reports/reports")
    assert response.status_code == 200
    assert response.json() == {"reports": ["demo_pack:demo"]}

    response = client.post(
        "/api/reports/generate",
        json={
            "report_id": "demo_pack:demo",
            "facts": {"name": "Alice"},
            "view": "default",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "Alice" in body["preview"]
