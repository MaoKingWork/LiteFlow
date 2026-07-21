"""Tests for ``report_engine_sdk.core.engine.ReportEngine``.

Each test builds a minimal pack (``demo_pack`` with reports ``demo`` and
``with_plugin``) plus templates inside a pytest ``tmp_path`` fixture directory
and drives the engine through the two-step evaluate -> render workflow and the
single-step direct-render workflow. The ``with_plugin`` report exercises the
plugin registration path end-to-end. A final test exercises the programmatic
:meth:`ReportEngine.from_config_provider` entry point with an
:class:`InMemoryPackProvider`.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from report_engine_sdk.core.config_provider import InMemoryPackProvider, PackConfig
from report_engine_sdk.core.engine import EvaluateResult, ReportEngine, RenderResult
from report_engine_sdk.core.pack_loader import PackError
from report_engine_sdk.core.plugin_registry import PluginBase
from report_engine_sdk.storage.memory import MemoryStorage


class StubPlugin(PluginBase):
    """Configurable plugin fixture returning a fixed payload dict."""

    def __init__(self, payload: dict) -> None:
        """Store the payload to return from :meth:`run`."""
        self.payload = payload

    def run(self, context: dict) -> dict:
        """Return a shallow copy of the configured payload."""
        return dict(self.payload)


def _setup_engine(tmp_path: Path) -> ReportEngine:
    """Build a :class:`ReportEngine` rooted at ``tmp_path``.

    Creates ``packs/demo_pack/pack.json`` with two reports (``demo`` and
    ``with_plugin``) and the supporting templates under
    ``packs/demo_pack/templates/``.
    """
    pack_dir = tmp_path / "packs" / "demo_pack"
    pack_dir.mkdir(parents=True)
    templates_dir = pack_dir / "templates"
    templates_dir.mkdir()

    pack = {
        "pack_id": "demo_pack",
        "purpose": "demo",
        "version": "1.0.0",
        "owner": "tests",
        "reports": {
            "demo": {
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "base_score": {"type": "number"},
                        "bonus": {"type": "number"},
                    },
                    "required": ["base_score"],
                },
                "rules": [
                    {
                        "name": "total",
                        "type": "formula",
                        "expression": "base_score * 0.8 + bonus",
                    }
                ],
                "templates": {
                    "default": {"path": "templates/demo.md"},
                    "manager": {"path": "templates/manager.md"},
                },
            },
            "with_plugin": {
                "input_schema": {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"],
                },
                "rules": [
                    {"name": "history", "type": "plugin", "plugin": "fetch_db_history"}
                ],
                "templates": {"default": {"path": "templates/plugin_report.md"}},
            },
        },
    }
    (pack_dir / "pack.json").write_text(
        json.dumps(pack, ensure_ascii=False), encoding="utf-8"
    )

    (templates_dir / "demo.md").write_text(
        "# Demo\nTotal: {{ total }}\nBase: {{ base_score }}", encoding="utf-8"
    )
    (templates_dir / "manager.md").write_text(
        "# Manager View\nScore: {{ total }}", encoding="utf-8"
    )
    (templates_dir / "plugin_report.md").write_text(
        "# Plugin Report\nHistory: {{ history }}\nUser: {{ user_id }}",
        encoding="utf-8",
    )

    return ReportEngine(str(tmp_path), MemoryStorage())


def test_evaluate_success(tmp_path: Path) -> None:
    """A valid facts dict produces computed data with rule outputs merged."""
    engine = _setup_engine(tmp_path)

    result = engine.evaluate("demo_pack:demo", {"base_score": 100, "bonus": 10})

    assert isinstance(result, EvaluateResult)
    assert result.success is True
    assert result.errors is None
    assert result.data is not None
    assert result.data["total"] == pytest.approx(90.0)
    assert result.data["base_score"] == 100
    assert result.data["bonus"] == 10


def test_evaluate_validation_failure(tmp_path: Path) -> None:
    """Missing required fields yield a structured validation failure."""
    engine = _setup_engine(tmp_path)

    result = engine.evaluate("demo_pack:demo", {"bonus": 10})

    assert result.success is False
    assert result.data is None
    assert result.errors is not None
    assert "missing_fields" in result.errors
    assert "base_score" in result.errors["missing_fields"]


def test_render_after_evaluate(tmp_path: Path) -> None:
    """Two-step workflow: evaluate then render the default view."""
    engine = _setup_engine(tmp_path)

    eval_result = engine.evaluate("demo_pack:demo", {"base_score": 100, "bonus": 10})
    assert eval_result.success is True
    assert eval_result.data is not None

    render_result = engine.render(
        "demo_pack:demo", eval_result.data, view="default"
    )

    assert isinstance(render_result, RenderResult)
    assert render_result.success is True
    assert render_result.errors is None
    assert render_result.file_uri is not None
    assert render_result.preview is not None
    assert "Total: 90" in render_result.preview


def test_render_direct(tmp_path: Path) -> None:
    """Single-step direct render (agent scenario) skips evaluation."""
    engine = _setup_engine(tmp_path)

    render_result = engine.render(
        "demo_pack:demo", {"total": 50, "base_score": 60}, view="default"
    )

    assert render_result.success is True
    assert render_result.preview is not None
    assert "Total: 50" in render_result.preview


def test_render_unknown_view(tmp_path: Path) -> None:
    """An unknown view yields a structured failure describing the view."""
    engine = _setup_engine(tmp_path)

    render_result = engine.render(
        "demo_pack:demo", {"total": 50, "base_score": 60}, view="nonexistent"
    )

    assert render_result.success is False
    assert render_result.file_uri is None
    assert render_result.preview is None
    assert render_result.errors is not None
    assert render_result.errors["view"] == "nonexistent"
    assert "nonexistent" in render_result.errors["message"]


def test_register_plugin(tmp_path: Path) -> None:
    """A registered plugin is invoked end-to-end by a plugin rule."""
    engine = _setup_engine(tmp_path)
    engine.register_plugin("fetch_db_history", StubPlugin({"history": "some history"}))

    result = engine.evaluate("demo_pack:with_plugin", {"user_id": "u-123"})

    assert result.success is True
    assert result.data is not None
    assert result.data["history"] == "some history"
    assert result.data["user_id"] == "u-123"

    render_result = engine.render(
        "demo_pack:with_plugin", result.data, view="default"
    )
    assert render_result.success is True
    assert render_result.preview is not None
    assert "some history" in render_result.preview
    assert "u-123" in render_result.preview


def test_concurrent_safety(tmp_path: Path) -> None:
    """Concurrent evaluate calls do not cross-contaminate results."""
    engine = _setup_engine(tmp_path)
    num_threads = 10

    def run(i: int) -> float:
        result = engine.evaluate(
            "demo_pack:demo", {"base_score": i, "bonus": 0}
        )
        assert result.success is True
        assert result.data is not None
        return result.data["total"]

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {executor.submit(run, i): i for i in range(num_threads)}
        for future, i in futures.items():
            total = future.result()
            assert total == pytest.approx(i * 0.8)


def test_unknown_report_id_raises(tmp_path: Path) -> None:
    """An unknown report_id raises PackError (programming error)."""
    engine = _setup_engine(tmp_path)

    with pytest.raises(PackError):
        engine.evaluate("demo_pack:nonexistent", {})


def test_list_reports(tmp_path: Path) -> None:
    """list_reports returns the sorted pack-aware report identifiers."""
    engine = _setup_engine(tmp_path)

    assert engine.list_reports() == ["demo_pack:demo", "demo_pack:with_plugin"]


def test_evaluate_result_is_frozen(tmp_path: Path) -> None:
    """EvaluateResult is immutable."""
    result = EvaluateResult(success=True, data={"a": 1}, errors=None)
    with pytest.raises(Exception):
        result.success = False  # type: ignore[misc]


def test_from_config_provider(tmp_path: Path) -> None:
    """ReportEngine.from_config_provider accepts an InMemoryPackProvider."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "demo.md").write_text("# Hi {{ name }}", encoding="utf-8")

    pack = PackConfig(
        pack_id="mem_pack",
        purpose="",
        version="",
        owner="",
        pack_dir=tmp_path,
        reports={
            "demo": {
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                "rules": [],
                "templates": {"default": {"path": "templates/demo.md"}},
            }
        },
    )
    engine = ReportEngine.from_config_provider(
        InMemoryPackProvider([pack]), MemoryStorage()
    )

    assert engine.list_reports() == ["mem_pack:demo"]

    render_result = engine.render(
        "mem_pack:demo", {"name": "Alice"}, view="default"
    )
    assert render_result.success is True
    assert "Alice" in render_result.preview
