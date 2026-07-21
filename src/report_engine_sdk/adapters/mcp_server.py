"""MCP Server adapter — exposes ReportEngine as an MCP Tool via fastmcp.

This module is optional; install the MCP extra with::

    pip install report-engine-sdk[mcp]

The core logic (``_generate_report_impl``) is independent of ``fastmcp`` and can
be unit-tested without the optional dependency installed. ``fastmcp`` is only
imported inside :func:`create_mcp_server`, so importing this module (or the
:mod:`report_engine_sdk.adapters` package) never triggers a framework import.
"""

from __future__ import annotations

from typing import Any

from report_engine_sdk.core.engine import ReportEngine
from report_engine_sdk.core.pack_loader import PackError


def _generate_report_impl(
    engine: ReportEngine,
    report_id: str,
    facts: dict,
    role: str = "default",
) -> dict:
    """Run the evaluate -> render pipeline and return an MCP-friendly dict.

    This is the framework-free core of the ``generate_report`` MCP tool. It is
    exposed at module level so it can be unit-tested without ``fastmcp``
    installed; :func:`create_mcp_server` registers a thin wrapper that
    delegates here.

    Error contract (never raises for ordinary flow errors):

        * Unknown ``report_id`` (``PackError``) -> ``{"error": {"message": ...}}``.
        * Validation/calculation failure -> ``{"error": <errors dict>}`` (the
          ``EvaluateResult.errors`` / ``RenderResult.errors`` payload).
        * Success -> ``{"file_uri": <str>, "preview": <str>}``.

    Args:
        engine: A configured :class:`ReportEngine` instance.
        report_id: Global report identifier ``"<pack_id>:<report_name>"``.
        facts: Input data conforming to the report's ``input_schema``.
        role: View name to render (e.g. ``"manager"``, ``"teacher"``,
            ``"default"``).

    Returns:
        A result dict shaped as described above.
    """
    try:
        eval_res = engine.evaluate(report_id, facts)
    except PackError as exc:
        return {"error": {"message": str(exc)}}

    if not eval_res.success:
        return {"error": eval_res.errors}

    try:
        render_res = engine.render(report_id, eval_res.data, view=role)
    except PackError as exc:
        return {"error": {"message": str(exc)}}

    if not render_res.success:
        return {"error": render_res.errors}

    return {"file_uri": render_res.file_uri, "preview": render_res.preview}


def create_mcp_server(
    engine: ReportEngine, name: str = "ReportGenerator"
) -> Any:
    """Create a FastMCP server exposing the report engine as an MCP tool.

    ``fastmcp`` is imported lazily inside this factory so that importing
    :mod:`report_engine_sdk.adapters.mcp_server` (or the parent
    :mod:`report_engine_sdk.adapters` package) does not require the optional
    dependency to be installed.

    Args:
        engine: A configured :class:`ReportEngine` instance.
        name: MCP server name.

    Returns:
        A ``FastMCP`` server instance with the ``generate_report`` tool
        registered.

    Raises:
        ImportError: If ``fastmcp`` is not installed. Install it with
            ``pip install report-engine-sdk[mcp]``.
    """
    try:
        from fastmcp import FastMCP
    except ImportError as exc:
        raise ImportError(
            "fastmcp is required for the MCP adapter. "
            "Install it with: pip install report-engine-sdk[mcp]"
        ) from exc

    mcp = FastMCP(name)

    @mcp.tool()
    def generate_report(
        report_id: str, facts: dict, role: str = "default"
    ) -> dict:
        """Generate a Markdown report for the given report_id.

        Args:
            report_id: Global report identifier ``"<pack_id>:<report_name>"``.
            facts: Input data conforming to the report's input_schema.
            role: View name (e.g. 'manager', 'teacher', 'default').

        Returns:
            On success: ``{"file_uri": <str>, "preview": <str>}``.
            On validation/calculation failure: ``{"error": <errors dict>}``.
            On unknown report_id: ``{"error": {"message": <str>}}``.
        """
        return _generate_report_impl(engine, report_id, facts, role)

    return mcp


def run_mcp_server(engine: ReportEngine, name: str = "ReportGenerator") -> None:
    """Create and run the MCP server (blocking). Convenience entry point.

    Args:
        engine: A configured :class:`ReportEngine` instance.
        name: MCP server name.

    Raises:
        ImportError: If ``fastmcp`` is not installed.
    """
    mcp = create_mcp_server(engine, name)
    mcp.run()


__all__ = ["create_mcp_server", "run_mcp_server", "_generate_report_impl"]
