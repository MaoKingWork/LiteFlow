"""Multi-view template renderer for the report engine SDK.

This module belongs to the protocol-agnostic core layer and depends only on
the Python standard library and ``jinja2``. :class:`TemplateRenderer` reads a
Jinja2 template corresponding to the requested ``view``, renders it against
the supplied data, persists the rendered Markdown through a
:class:`~report_engine_sdk.storage.base.StorageBackend`, and returns an
immutable :class:`RenderResult` describing the outcome.

Template paths are resolved to absolute form by the
:class:`~report_engine_sdk.core.pack_loader.PackLoader` at load time, so the
renderer simply opens the path it is handed -- it carries no configuration
directory and stays independent of loader internals. This also makes the
renderer trivially testable in isolation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from jinja2 import Environment, StrictUndefined
from jinja2.exceptions import TemplateError, UndefinedError

from ..storage.base import StorageBackend


@dataclass(frozen=True)
class RenderResult:
    """Immutable result of a render operation.

    Attributes:
        success: ``True`` if rendering and persistence succeeded;
            ``False`` otherwise.
        file_uri: Storage URI of the persisted artifact on success;
            ``None`` on failure.
        preview: Truncated Markdown preview of the rendered content on
            success; ``None`` on failure.
        errors: ``None`` on success. On failure either
            ``{"message": <str>}`` for generic failures (missing template
            file, read error, Jinja2 error) or
            ``{"view": <unknown view>, "available": [...], "message": <str>}``
            for an unknown view.
    """

    success: bool
    file_uri: Optional[str] = None
    preview: Optional[str] = None
    errors: Optional[dict] = None


class TemplateRenderer:
    """Renders Jinja2 templates by view and persists the output.

    The renderer is stateless across :meth:`render` calls: it holds only the
    storage backend and a reusable Jinja2 environment, neither of which carry
    per-call mutable state. Concurrent invocations are therefore safe.

    Each ``templates[view]["path"]`` is expected to be an absolute path to an
    existing file (resolved by the loader); the renderer performs no path
    resolution itself.
    """

    #: Maximum number of characters retained in :attr:`RenderResult.preview`.
    _PREVIEW_LIMIT = 2000

    #: Suffix appended to a preview when the rendered output is truncated.
    _TRUNCATION_MARKER = "\n\n... (truncated)"

    def __init__(self, storage: StorageBackend) -> None:
        """Initialize the renderer.

        Args:
            storage: Storage backend used to persist rendered Markdown.
        """
        self._storage = storage
        self._env = Environment(
            autoescape=False,
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )

    def render(
        self,
        templates: dict,
        view: str,
        data: dict,
        report_id: str = "",
    ) -> RenderResult:
        """Render the template bound to ``view`` and persist the output.

        Args:
            templates: The ``templates`` mapping from
                :class:`~report_engine_sdk.core.pack_loader.ReportConfig`
                (view name -> ``{"path": <abs path>, ...}``).
            view: Name of the view to render (e.g. ``"manager"``,
                ``"default"``).
            data: Render context; variables consumed by the template.
            report_id: Identifier of the report (e.g.
                ``"teacher_eval:evaluation"``), used to build the stored
                filename. May be empty.

        Returns:
            A :class:`RenderResult` describing the outcome. Failures are
            reported via the ``errors`` dict rather than raised exceptions.
        """
        if view not in templates:
            return RenderResult(
                success=False,
                file_uri=None,
                preview=None,
                errors={
                    "view": view,
                    "available": sorted(templates.keys()),
                    "message": f"Unknown view '{view}'",
                },
            )

        template_meta = templates[view]
        path_str = template_meta["path"]
        path = Path(path_str)
        if not path.exists():
            return RenderResult(
                success=False,
                file_uri=None,
                preview=None,
                errors={"message": f"Template file not found: {path_str}"},
            )

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return RenderResult(
                success=False,
                file_uri=None,
                preview=None,
                errors={"message": str(exc)},
            )

        try:
            template = self._env.from_string(content)
            rendered = template.render(**data)
        except (UndefinedError, TemplateError) as exc:
            return RenderResult(
                success=False,
                file_uri=None,
                preview=None,
                errors={"message": str(exc)},
            )

        filename = self._build_filename(report_id, view)
        file_uri = self._storage.save(filename, rendered)
        preview = self._build_preview(rendered)
        return RenderResult(
            success=True,
            file_uri=file_uri,
            preview=preview,
            errors=None,
        )

    def _build_filename(self, report_id: str, view: str) -> str:
        """Build a unique, filesystem-safe Markdown filename.

        The ``report_id`` (which may contain a ``":"`` pack separator, e.g.
        ``"teacher_eval:evaluation"``) is sanitized by replacing path-unsafe
        characters with ``_`` so the filename is portable across operating
        systems.

        Args:
            report_id: Identifier of the report (may be empty).
            view: Name of the rendered view.

        Returns:
            A filename of the form ``"<sanitized_report_id>_<view>_<8-hex>.md"``
            or ``"<view>_<8-hex>.md"`` when ``report_id`` is empty.
        """
        suffix = uuid.uuid4().hex[:8]
        if report_id:
            safe_id = report_id.replace(":", "_").replace("/", "_")
            return f"{safe_id}_{view}_{suffix}.md"
        return f"{view}_{suffix}.md"

    def _build_preview(self, rendered: str) -> str:
        """Truncate rendered content to a preview-friendly length.

        Args:
            rendered: The full rendered Markdown string.

        Returns:
            The rendered content truncated to :attr:`_PREVIEW_LIMIT`
            characters, with :attr:`_TRUNCATION_MARKER` appended when
            truncation occurs.
        """
        if len(rendered) <= self._PREVIEW_LIMIT:
            return rendered
        return rendered[: self._PREVIEW_LIMIT] + self._TRUNCATION_MARKER
