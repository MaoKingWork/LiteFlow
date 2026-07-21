"""Tests for ``report_engine_sdk.adapters.langchain_tool``.

The core logic (:func:`generate_report_impl`) is exercised without langchain
installed — only ``pydantic`` (a transitive langchain dependency) is required
for the input schema, and even that is not touched by the impl tests. The
``create_langchain_tool`` tests use ``pytest.importorskip`` so they are
gracefully skipped when langchain is not installed, except the missing-langchain
test which manipulates ``sys.modules`` to simulate the absence of langchain.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from report_engine_sdk.adapters.langchain_tool import (
    create_langchain_tool,
    generate_report_impl,
)
from report_engine_sdk.core.engine import ReportEngine
from report_engine_sdk.storage.memory import MemoryStorage


def _setup_engine(tmp_path: Path) -> ReportEngine:
    """Build a :class:`ReportEngine` rooted at ``tmp_path``.

    Creates ``packs/demo_pack/pack.json`` with a single ``demo`` report
    (``name: string`` required, no rules) and a ``default`` template rendering
    ``# Hi {{ name }}``. The report's global id is ``demo_pack:demo``.
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
    (templates_dir / "demo.md").write_text("# Hi {{ name }}", encoding="utf-8")

    return ReportEngine(str(tmp_path), MemoryStorage())


def test_generate_report_impl_success(tmp_path: Path) -> None:
    """A valid facts dict yields a ``memory://`` file_uri string."""
    engine = _setup_engine(tmp_path)

    result = generate_report_impl(
        engine, "demo_pack:demo", {"name": "Alice"}, "default"
    )

    assert isinstance(result, str)
    assert result.startswith("memory://")
    assert not result.startswith("ERROR:")


def test_generate_report_impl_validation_failure(tmp_path: Path) -> None:
    """Missing required fields yield an ``ERROR:`` string mentioning validation."""
    engine = _setup_engine(tmp_path)

    result = generate_report_impl(
        engine, "demo_pack:demo", {}, "default"
    )

    assert isinstance(result, str)
    assert result.startswith("ERROR:")
    assert "validation" in result


def test_generate_report_impl_unknown_report(tmp_path: Path) -> None:
    """An unknown report_id yields an ``ERROR:`` string (no exception raised)."""
    engine = _setup_engine(tmp_path)

    result = generate_report_impl(engine, "nonexistent", {}, "default")

    assert isinstance(result, str)
    assert result.startswith("ERROR:")


def test_create_langchain_tool_returns_tool(tmp_path: Path) -> None:
    """create_langchain_tool returns a StructuredTool that invokes the engine."""
    pytest.importorskip("langchain")
    engine = _setup_engine(tmp_path)

    tool = create_langchain_tool(engine)

    assert tool.name == "markdown_report_generator"
    result = tool.invoke(
        {
            "report_id": "demo_pack:demo",
            "data": {"name": "Bob"},
            "view": "default",
        }
    )
    assert isinstance(result, str)
    assert result.startswith("memory://")


def test_create_langchain_tool_missing_langchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create_langchain_tool raises ImportError with an install hint when
    langchain is unavailable.

    The adapter falls back from ``langchain.tools`` to ``langchain_core.tools``
    (langchain 1.x moved ``StructuredTool`` there), so both module trees must
    be blocked to simulate a fully missing langchain toolchain. Setting a
    ``sys.modules`` entry to ``None`` causes ``import <name>`` to raise
    :class:`ImportError` regardless of whether the package is installed.
    """
    engine = _setup_engine(tmp_path)

    for mod in (
        "langchain",
        "langchain.tools",
        "langchain_core",
        "langchain_core.tools",
    ):
        monkeypatch.setitem(sys.modules, mod, None)

    with pytest.raises(ImportError, match="langchain is required"):
        create_langchain_tool(engine)
