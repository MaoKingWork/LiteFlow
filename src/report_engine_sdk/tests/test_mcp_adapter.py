"""Tests for ``report_engine_sdk.adapters.mcp_server``.

The core logic (``_generate_report_impl``) is framework-free and is exercised
directly here. Only ``test_create_mcp_server_returns_server`` requires
``fastmcp`` to be installed; it skips gracefully otherwise. The remaining
tests (``_generate_report_impl`` cases and the missing-fastmcp guard) run
without ``fastmcp`` installed, so the adapter's lazy-import contract is
verified on every environment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from report_engine_sdk.adapters.mcp_server import (
    _generate_report_impl,
    create_mcp_server,
)
from report_engine_sdk.core.engine import ReportEngine
from report_engine_sdk.storage.memory import MemoryStorage


def _setup_engine(tmp_path: Path) -> ReportEngine:
    """Build a :class:`ReportEngine` rooted at ``tmp_path``.

    Creates a ``packs/demo_pack/pack.json`` with a single pass-through report
    ``demo`` (``rules=[]``, ``input_schema`` requiring ``name: string``) and a
    matching template under ``packs/demo_pack/templates/``. The report's
    global id is ``demo_pack:demo``.
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


def test_mcp_tool_success(tmp_path: Path) -> None:
    """A valid facts dict yields a file_uri and a preview containing the name."""
    engine = _setup_engine(tmp_path)

    result = _generate_report_impl(
        engine, "demo_pack:demo", {"name": "Alice"}, "default"
    )

    assert "error" not in result
    assert "file_uri" in result
    assert "preview" in result
    assert isinstance(result["file_uri"], str)
    assert len(result["file_uri"]) > 0
    assert "Alice" in result["preview"]
    assert result["preview"].startswith("# Hi ")


def test_mcp_tool_validation_failure(tmp_path: Path) -> None:
    """Missing required fields surface as an ``error`` dict with missing_fields."""
    engine = _setup_engine(tmp_path)

    result = _generate_report_impl(
        engine, "demo_pack:demo", {"wrong_field": "x"}, "default"
    )

    assert "file_uri" not in result
    assert "error" in result
    errors = result["error"]
    assert isinstance(errors, dict)
    assert "missing_fields" in errors
    assert "name" in errors["missing_fields"]


def test_mcp_tool_unknown_report(tmp_path: Path) -> None:
    """An unknown report_id returns ``{"error": {"message": ...}}`` (no raise)."""
    engine = _setup_engine(tmp_path)

    result = _generate_report_impl(
        engine, "nonexistent", {}, "default"
    )

    assert "file_uri" not in result
    assert "error" in result
    error = result["error"]
    assert isinstance(error, dict)
    assert "message" in error
    assert "nonexistent" in error["message"]


def test_create_mcp_server_returns_server(tmp_path: Path) -> None:
    """``create_mcp_server`` returns a server with a ``run`` method (fastmcp required)."""
    pytest.importorskip("fastmcp")
    engine = _setup_engine(tmp_path)

    mcp = create_mcp_server(engine)

    assert mcp is not None
    assert callable(getattr(mcp, "run", None))


def test_create_mcp_server_missing_fastmcp(tmp_path: Path) -> None:
    """When ``fastmcp`` cannot be imported, ``create_mcp_server`` raises ImportError.

    The raised error must carry the install hint. This test runs without
    ``fastmcp`` installed (it forces the import to fail via ``sys.modules``
    manipulation), guarding the adapter's lazy-import contract on every
    environment.
    """
    engine = _setup_engine(tmp_path)

    # Setting sys.modules["fastmcp"] = None makes ``from fastmcp import ...``
    # raise ImportError ("import of fastmcp halted; None in sys.modules").
    with mock.patch.dict(sys.modules, {"fastmcp": None}):
        with pytest.raises(ImportError) as exc_info:
            create_mcp_server(engine)

    message = str(exc_info.value)
    assert "fastmcp" in message
    assert "report-engine-sdk[mcp]" in message
