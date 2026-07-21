"""LangChain Tool adapter — exposes ReportEngine as a StructuredTool.

This module is optional; install with: pip install report-engine-sdk[langchain]

The module is engineered so that the core logic (:func:`generate_report_impl`)
and the input schema (:class:`ReportToolInput`) can be imported without
``langchain`` installed — only ``pydantic`` is required at module import time
(pydantic is a lightweight, ubiquitous dependency and a transitive requirement
of langchain itself). The ``langchain`` import is performed lazily inside
:func:`create_langchain_tool` so that environments without langchain can still
unit-test :func:`generate_report_impl`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from report_engine_sdk.core.engine import ReportEngine
from report_engine_sdk.core.pack_loader import PackError


class ReportToolInput(BaseModel):
    """Input schema for the markdown report generator tool.

    Attributes:
        report_id: Global report identifier ``"<pack_id>:<report_name>"``.
        data: Input data conforming to the report's ``input_schema``
            (already-computed or raw facts).
        view: View name (e.g., ``'manager'``, ``'teacher'``, ``'default'``).
    """

    report_id: str = Field(
        ...,
        description="Global report identifier '<pack_id>:<report_name>'",
    )
    data: dict = Field(
        ...,
        description=(
            "Input data conforming to the report's input_schema "
            "(already-computed or raw facts)"
        ),
    )
    view: str = Field(
        "default",
        description="View name (e.g., 'manager', 'teacher', 'default')",
    )


def generate_report_impl(
    engine: ReportEngine,
    report_id: str,
    data: dict,
    view: str = "default",
) -> str:
    """Core logic for the LangChain tool — returns file_uri or error string.

    This function is testable without langchain installed. It drives the
    two-step evaluate -> render workflow and flattens both structured
    failures and :class:`PackError` exceptions into a single string
    return value, since LangChain tools typically return strings.

    Args:
        engine: A configured :class:`ReportEngine` instance.
        report_id: Global report identifier ``"<pack_id>:<report_name>"``.
        data: Input data (facts). Will be passed through evaluate then
            render.
        view: View name.

    Returns:
        The ``file_uri`` string on success, or an error string starting
        with ``"ERROR:"`` on failure.
    """
    try:
        eval_res = engine.evaluate(report_id, data)
    except PackError as e:
        return f"ERROR: {e}"

    if not eval_res.success:
        return f"ERROR: validation/calculation failed: {eval_res.errors}"

    try:
        render_res = engine.render(report_id, eval_res.data, view=view)
    except PackError as e:
        return f"ERROR: {e}"

    if not render_res.success:
        return f"ERROR: render failed: {render_res.errors}"

    return render_res.file_uri


def create_langchain_tool(
    engine: ReportEngine,
    tool_name: str = "markdown_report_generator",
) -> Any:
    """Create a LangChain ``StructuredTool`` wrapping the report engine.

    Args:
        engine: A configured :class:`ReportEngine` instance.
        tool_name: Name for the ``StructuredTool``.

    Returns:
        A ``langchain.tools.StructuredTool`` instance.

    Raises:
        ImportError: If langchain is not installed. Install it with
            ``pip install report-engine-sdk[langchain]``.
    """
    # ``StructuredTool`` historically lived in ``langchain.tools`` (langchain
    # < 1.0) but was moved to ``langchain_core.tools`` in langchain 1.x (the
    # ``langchain.tools`` namespace no longer re-exports it). Try both
    # locations so the adapter works across langchain versions; only the
    # actual implementation (``langchain-core``) is required at runtime.
    try:
        from langchain.tools import StructuredTool  # langchain < 1.0
    except ImportError:
        try:
            from langchain_core.tools import StructuredTool  # langchain >= 1.0
        except ImportError as e:
            raise ImportError(
                "langchain is required for the LangChain adapter. "
                "Install it with: pip install report-engine-sdk[langchain]"
            ) from e

    def _run(report_id: str, data: dict, view: str = "default") -> str:
        return generate_report_impl(engine, report_id, data, view)

    tool = StructuredTool.from_function(
        func=_run,
        name=tool_name,
        description=(
            "生成格式化的 Markdown 报告，支持多角色视图。调用后返回报告的 "
            "file_uri，失败时返回 ERROR: 前缀的错误信息。"
        ),
        args_schema=ReportToolInput,
    )
    return tool


__all__ = [
    "create_langchain_tool",
    "generate_report_impl",
    "ReportToolInput",
]
