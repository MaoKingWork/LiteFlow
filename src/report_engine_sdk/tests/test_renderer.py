"""Tests for ``report_engine_sdk.core.renderer.TemplateRenderer``.

Each test builds template files inside a pytest ``tmp_path`` fixture directory
and renders them through a :class:`TemplateRenderer` backed by
:class:`~report_engine_sdk.storage.memory.MemoryStorage`, whose ``read()``
helper is used to verify the persisted content. Template paths are supplied as
absolute paths (the loader resolves them to absolute form in production).
"""

from __future__ import annotations

from pathlib import Path

from report_engine_sdk.core.renderer import RenderResult, TemplateRenderer
from report_engine_sdk.storage.memory import MemoryStorage


def _write_template(tmp_path: Path, name: str, content: str) -> Path:
    """Write ``content`` to ``tmp_path/templates/<name>`` and return its path."""
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir(exist_ok=True)
    path = templates_dir / name
    path.write_text(content, encoding="utf-8")
    return path


def _make_renderer() -> TemplateRenderer:
    """Build a :class:`TemplateRenderer` backed by :class:`MemoryStorage`."""
    return TemplateRenderer(MemoryStorage())


def test_render_multi_view(tmp_path: Path) -> None:
    """Two views render through their respective templates."""
    manager_path = _write_template(
        tmp_path, "manager.md", "# Manager\nScore: {{ total }}"
    )
    teacher_path = _write_template(
        tmp_path, "teacher.md", "# Teacher\nBase: {{ base_score }}"
    )
    templates = {
        "manager": {"path": str(manager_path)},
        "teacher": {"path": str(teacher_path)},
    }
    renderer = _make_renderer()

    result = renderer.render(
        templates=templates,
        view="manager",
        data={"total": 90, "base_score": 100},
        report_id="teacher_eval:performance",
    )

    assert result.success is True
    assert isinstance(result, RenderResult)
    assert result.file_uri is not None
    assert result.file_uri.startswith("memory://")
    assert result.errors is None
    assert result.preview is not None
    assert "Score: 90" in result.preview

    storage = renderer._storage  # noqa: SLF001 - test-only access
    stored = storage.read(result.file_uri)
    assert stored == "# Manager\nScore: 90"


def test_render_default_view(tmp_path: Path) -> None:
    """A single ``default`` view renders via its template."""
    health_path = _write_template(
        tmp_path, "health.md", "# Health\nStatus: {{ status }}"
    )
    templates = {"default": {"path": str(health_path)}}
    renderer = _make_renderer()

    result = renderer.render(
        templates=templates,
        view="default",
        data={"status": "ok"},
    )

    assert result.success is True
    assert result.errors is None
    assert result.preview is not None
    assert "Status: ok" in result.preview


def test_unknown_view(tmp_path: Path) -> None:
    """An unknown view yields a structured failure listing available views."""
    health_path = _write_template(tmp_path, "health.md", "# Health\n{{ status }}")
    templates = {"default": {"path": str(health_path)}}
    renderer = _make_renderer()

    result = renderer.render(
        templates=templates,
        view="manager",
        data={"status": "ok"},
    )

    assert result.success is False
    assert result.file_uri is None
    assert result.preview is None
    assert result.errors is not None
    assert result.errors["view"] == "manager"
    assert result.errors["available"] == ["default"]
    assert "manager" in result.errors["message"]


def test_preview_truncation(tmp_path: Path) -> None:
    """Rendered output longer than the preview limit is truncated."""
    long_path = _write_template(
        tmp_path,
        "long.md",
        "{% for i in range(300) %}line {{ i }}\n{% endfor %}",
    )
    templates = {"default": {"path": str(long_path)}}
    renderer = _make_renderer()

    result = renderer.render(
        templates=templates,
        view="default",
        data={},
    )

    assert result.success is True
    assert result.preview is not None
    assert "(truncated)" in result.preview
    assert len(result.preview) <= 2000 + len("\n\n... (truncated)")


def test_template_file_not_found(tmp_path: Path) -> None:
    """A missing template file yields a failure mentioning the missing path."""
    templates = {"v": {"path": str(tmp_path / "missing.md")}}
    renderer = _make_renderer()

    result = renderer.render(
        templates=templates,
        view="v",
        data={},
    )

    assert result.success is False
    assert result.file_uri is None
    assert result.preview is None
    assert result.errors is not None
    assert "not found" in result.errors["message"]
    assert "missing.md" in result.errors["message"]


def test_undefined_variable_strict(tmp_path: Path) -> None:
    """A template referencing an undefined variable fails (StrictUndefined)."""
    strict_path = _write_template(tmp_path, "strict.md", "{{ undefined_var }}")
    templates = {"default": {"path": str(strict_path)}}
    renderer = _make_renderer()

    result = renderer.render(
        templates=templates,
        view="default",
        data={},
    )

    assert result.success is False
    assert result.file_uri is None
    assert result.preview is None
    assert result.errors is not None
    assert "undefined_var" in result.errors["message"]


def test_filename_sanitizes_pack_separator(tmp_path: Path) -> None:
    """The pack ``":"`` separator is sanitized in the built filename.

    ``_build_filename`` is tested directly because :class:`MemoryStorage` keys
    content by UUID and never exposes the filename; the sanitization only
    matters for filesystem-backed storage (e.g. :class:`LocalStorage`).
    """
    renderer = _make_renderer()

    filename = renderer._build_filename(  # noqa: SLF001 - test-only access
        "teacher_eval:performance", "default"
    )

    assert filename.endswith(".md")
    # No colon (illegal on Windows) and no path separator leaks through.
    assert ":" not in filename
    assert "/" not in filename
    assert "\\" not in filename
    assert filename.startswith("teacher_eval_performance_default_")
