"""FastAPI adapter — exposes ReportEngine as HTTP endpoints.

This module is optional; install with: pip install report-engine-sdk[api]

The :func:`generate_report_impl` helper is pure Python and can be imported and
unit-tested without ``fastapi`` installed. The :func:`create_api_router` and
:func:`create_app` factory functions lazily import ``fastapi`` and
``pydantic`` so the module itself never fails to import when the optional
``api`` extra is absent.
"""

# Note: this module intentionally does NOT use ``from __future__ import
# annotations``. The Pydantic request/response models are defined *locally*
# inside :func:`create_api_router` (so the module imports without pydantic
# installed). With PEP 563 deferred annotations, FastAPI would receive the
# string ``"GenerateReportRequest"`` and fail to resolve it via
# ``typing.get_type_hints`` (which only consults module globals, not the
# enclosing function scope), resulting in 422 "Field required" errors.
from typing import Any, Optional

from report_engine_sdk.core.engine import ReportEngine
from report_engine_sdk.core.pack_loader import PackError


def generate_report_impl(
    engine: ReportEngine,
    report_id: str,
    facts: dict,
    view: str = "default",
) -> dict:
    """Core logic for the HTTP endpoint — returns a JSON-serializable dict.

    Testable without fastapi installed.

    Args:
        engine: A configured :class:`ReportEngine` instance.
        report_id: Global report identifier ``"<pack_id>:<report_name>"``.
        facts: Input data conforming to the report's ``input_schema``.
        view: Name of the view to render. Defaults to ``"default"``.

    Returns:
        On success: ``{"success": True, "file_uri": <str>, "preview": <str>}``.
        On a validation or render failure:
        ``{"success": False, "error": <dict or str>}``.
        On an unknown ``report_id``:
        ``{"success": False, "error": {"message": <str>}}``.
    """
    try:
        eval_res = engine.evaluate(report_id, facts)
    except PackError as exc:
        return {"success": False, "error": {"message": str(exc)}}

    if not eval_res.success:
        return {"success": False, "error": eval_res.errors}

    try:
        render_res = engine.render(report_id, eval_res.data, view=view)
    except PackError as exc:
        return {"success": False, "error": {"message": str(exc)}}

    if not render_res.success:
        return {"success": False, "error": render_res.errors}

    return {
        "success": True,
        "file_uri": render_res.file_uri,
        "preview": render_res.preview,
    }


def create_api_router(
    engine: ReportEngine, prefix: str = "/api/reports"
) -> Any:
    """Create a FastAPI ``APIRouter`` exposing report generation endpoints.

    Args:
        engine: A configured :class:`ReportEngine` instance.
        prefix: URL prefix for the router. Defaults to ``"/api/reports"``.

    Returns:
        A FastAPI ``APIRouter`` instance with ``/generate`` and ``/reports``
        routes mounted under ``prefix``.

    Raises:
        ImportError: if ``fastapi`` is not installed. Install it with
            ``pip install report-engine-sdk[api]``.
    """
    try:
        from fastapi import APIRouter, HTTPException
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise ImportError(
            "fastapi is required for the API adapter. "
            "Install it with: pip install report-engine-sdk[api]"
        ) from exc

    router = APIRouter(prefix=prefix, tags=["reports"])

    class GenerateReportRequest(BaseModel):
        """Request body for the ``/generate`` endpoint."""

        report_id: str = Field(
            ...,
            description="Global report identifier '<pack_id>:<report_name>'",
        )
        facts: dict = Field(
            ...,
            description="Input data conforming to the report's input_schema",
        )
        view: str = Field("default", description="View name")

    class GenerateReportResponse(BaseModel):
        """Response body for the ``/generate`` endpoint."""

        success: bool
        file_uri: Optional[str] = None
        preview: Optional[str] = None
        error: Optional[Any] = None

    @router.post("/generate", response_model=GenerateReportResponse)
    def generate_report(
        request: GenerateReportRequest,
    ) -> GenerateReportResponse:
        """Generate a Markdown report.

        Returns 200 with ``success=True`` on success, or 200 with
        ``success=False`` on validation/render failure. An unknown
        ``report_id`` returns 404.
        """
        try:
            result = generate_report_impl(
                engine,
                request.report_id,
                request.facts,
                request.view,
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Defensive: generate_report_impl catches PackError and folds
            # engine failures into the result dict; any other unexpected error
            # surfaces as a 500.
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        if (
            not result.get("success")
            and isinstance(result.get("error"), dict)
            and "message" in result["error"]
        ):
            # Unknown report_id (PackError) → 404
            raise HTTPException(
                status_code=404, detail=result["error"]["message"]
            )

        return GenerateReportResponse(
            success=result.get("success", False),
            file_uri=result.get("file_uri"),
            preview=result.get("preview"),
            error=result.get("error"),
        )

    @router.get("/reports")
    def list_reports() -> dict:
        """List available ``report_id`` identifiers."""
        return {"reports": engine.list_reports()}

    return router


def create_app(
    engine: ReportEngine, prefix: str = "/api/reports"
) -> Any:
    """Create a complete FastAPI app with the report router mounted.

    Convenience entry point for standalone deployment.

    Args:
        engine: A configured :class:`ReportEngine` instance.
        prefix: URL prefix for the router. Defaults to ``"/api/reports"``.

    Returns:
        A FastAPI application instance with the report router mounted.

    Raises:
        ImportError: if ``fastapi`` is not installed. Install it with
            ``pip install report-engine-sdk[api]``.
    """
    try:
        from fastapi import FastAPI
    except ImportError as exc:
        raise ImportError(
            "fastapi is required for the API adapter. "
            "Install it with: pip install report-engine-sdk[api]"
        ) from exc

    app = FastAPI(title="Report Engine API", version="0.1.0")
    app.include_router(create_api_router(engine, prefix=prefix))
    return app


__all__ = ["create_api_router", "create_app", "generate_report_impl"]
