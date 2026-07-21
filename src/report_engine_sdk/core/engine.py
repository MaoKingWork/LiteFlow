"""ReportEngine facade and DTOs for the report engine SDK.

This module belongs to the protocol-agnostic core layer. It assembles the
pack loader, schema validator, rule calculator, template renderer, and plugin
registry behind a single :class:`ReportEngine` facade and exposes immutable
DTOs (:class:`EvaluateResult` and :class:`RenderResult`) as the engine's
public contract.

Reports are organized into **packs** (one pack per purpose). A report's global
identifier is ``"<pack_id>:<report_name>"`` -- unique across all packs -- so
each pack can evolve independently while the engine exposes a single flat
namespace to callers. Configuration is supplied through a
:class:`~report_engine_sdk.core.config_provider.ConfigProvider`, so the same
engine works with filesystem packs, in-memory packs, or any future source.

The facade supports two workflows:
    1. **Two-step evaluate -> render**: callers first compute structured data
       via :meth:`ReportEngine.evaluate`, then render it via
       :meth:`ReportEngine.render`. This is the default flow for reports that
       declare calculation rules.
    2. **Single-step direct render**: callers that already hold structured
       data (e.g. an agent briefing) skip evaluation and call
       :meth:`ReportEngine.render` directly.

Both workflows are stateless across calls; the only mutable state held by the
engine is the append-only :class:`PluginRegistry`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config_provider import ConfigProvider, FileSystemPackProvider
from .pack_loader import PackError, PackLoader, ReportConfig
from .validator import SchemaValidator, ValidationResult
from .calculator import CalculationResult, RuleCalculator
from .renderer import RenderResult, TemplateRenderer
from .plugin_registry import PluginBase, PluginRegistry
from ..storage.base import StorageBackend


@dataclass(frozen=True)
class EvaluateResult:
    """Immutable result of an :meth:`ReportEngine.evaluate` call.

    Attributes:
        success: ``True`` if both schema validation and rule calculation
            completed without errors; ``False`` otherwise.
        data: The computed data (input merged with rule outputs) on
            success; ``None`` on failure.
        errors: Structured error dict on failure, shaped as a subset of
            ``{"missing_fields": [...], "invalid_types": [...],
            "missing_plugins": [...], "formula_errors": [...],
            "plugin_errors": [...]}`` (only non-empty keys included).
            On a validation failure only validation keys are present; on a
            calculation failure only calculation keys are present.
            ``None`` on success.
    """

    success: bool
    data: Optional[dict] = None
    errors: Optional[dict] = None


class ReportEngine:
    """Facade orchestrating the pack-driven evaluate -> render pipeline.

    The engine composes a :class:`PackLoader`, :class:`SchemaValidator`,
    :class:`RuleCalculator`, :class:`TemplateRenderer`, and
    :class:`PluginRegistry` behind a small surface area. Construction triggers
    eager pack loading and self-checks; any configuration drift surfaces
    immediately as a :class:`PackError`.

    Two workflows are supported:
        * **Two-step (evaluate then render)** -- :meth:`evaluate` validates
          ``facts`` against the report's ``input_schema`` and runs the
          ``rules`` pipeline, returning an :class:`EvaluateResult`. The
          caller then passes the computed ``data`` to :meth:`render` to
          produce a :class:`RenderResult` for the desired view.
        * **Single-step direct render** -- when the caller already holds
          structured data (e.g. an agent briefing with ``rules=[]``), it
          calls :meth:`render` directly, skipping validation and calculation.

    The engine holds no per-call mutable state; the only mutable state is the
    :class:`PluginRegistry`, which is append-only. Concurrent invocations of
    :meth:`evaluate` and :meth:`render` are therefore safe.
    """

    def __init__(self, config_path: str, storage: StorageBackend) -> None:
        """Initialize the engine from a filesystem config directory.

        Builds a :class:`FileSystemPackProvider` over ``config_path`` and eager
        -loads every pack. For a programmatic configuration source (e.g. an
        in-memory provider for tests or multi-tenant injection), use
        :meth:`from_config_provider` instead.

        Args:
            config_path: Directory containing either a ``root.json`` enable-list
                or a ``packs/`` subdirectory of pack folders.
            storage: Storage backend used to persist rendered reports.

        Raises:
            PackError: If any pack cannot be loaded or fails its self-check.
                This surfaces configuration drift at engine startup rather than
                at render time.
        """
        self._init(FileSystemPackProvider(config_path), storage)

    @classmethod
    def from_config_provider(
        cls, provider: ConfigProvider, storage: StorageBackend
    ) -> "ReportEngine":
        """Initialize the engine from an explicit :class:`ConfigProvider`.

        This is the programmatic configuration entry point: callers can supply
        an :class:`InMemoryPackProvider` (tests, dynamic injection), a custom
        provider backed by a database, or a pre-configured
        :class:`FileSystemPackProvider`.

        Args:
            provider: The configuration provider that discovers packs.
            storage: Storage backend used to persist rendered reports.

        Raises:
            PackError: If any pack cannot be loaded or fails its self-check.
        """
        engine = cls.__new__(cls)
        engine._init(provider, storage)
        return engine

    def _init(self, provider: ConfigProvider, storage: StorageBackend) -> None:
        """Shared initializer used by both constructors."""
        self._loader = PackLoader(provider)
        self._validator = SchemaValidator()
        self._plugin_registry = PluginRegistry()
        self._calculator = RuleCalculator(self._plugin_registry)
        self._renderer = TemplateRenderer(storage)

    def evaluate(self, report_id: str, facts: dict) -> EvaluateResult:
        """Validate ``facts`` and run the report's ``rules`` pipeline.

        Two-step workflow, step one. The method first validates ``facts``
        against the report's ``input_schema``; on failure it returns an
        :class:`EvaluateResult` whose ``errors`` carry validation keys
        (``missing_fields`` / ``invalid_types``). On validation success it
        runs the ``rules`` pipeline; on calculation failure it returns an
        :class:`EvaluateResult` whose ``errors`` carry calculation keys
        (``missing_plugins`` / ``formula_errors`` / ``plugin_errors``). On
        full success it returns the computed ``data`` (input merged with
        rule outputs).

        Args:
            report_id: Global identifier ``"<pack_id>:<report_name>"`` of the
                report whose schema and rules should be applied.
            facts: Input data dict to validate and compute against.

        Returns:
            An :class:`EvaluateResult` describing the outcome. Failures are
            reported via the ``errors`` dict rather than raised exceptions.

        Raises:
            PackError: If ``report_id`` is not a known report. This is a
                programming/configuration error and is allowed to propagate
                rather than being folded into ``errors``.
        """
        config: ReportConfig = self._loader.get(report_id)

        validation_result: ValidationResult = self._validator.validate(
            config.input_schema, facts
        )
        if not validation_result.success:
            return EvaluateResult(
                success=False,
                data=None,
                errors=validation_result.errors,
            )

        calculation_result: CalculationResult = self._calculator.calculate(
            config.rules, facts
        )
        if not calculation_result.success:
            return EvaluateResult(
                success=False,
                data=None,
                errors=calculation_result.errors,
            )

        return EvaluateResult(
            success=True,
            data=calculation_result.data,
            errors=None,
        )

    def render(
        self,
        report_id: str,
        data: dict,
        view: str = "default",
    ) -> RenderResult:
        """Render the report's ``view`` template against ``data``.

        Two-step workflow, step two (or the single step of the direct-render
        workflow). The method looks up the report's ``templates`` mapping,
        selects the entry for ``view``, renders the corresponding Jinja2
        template against ``data``, persists the rendered Markdown through the
        injected :class:`StorageBackend`, and returns a :class:`RenderResult`.

        No validation or calculation is performed; the caller is responsible
        for supplying ``data`` that satisfies the template's variables. This
        is what enables the single-step direct-render workflow for agent
        briefings.

        Args:
            report_id: Global identifier ``"<pack_id>:<report_name>"`` of the
                report whose templates should be used.
            data: Render context; variables consumed by the template.
            view: Name of the view to render. Defaults to ``"default"``.

        Returns:
            A :class:`RenderResult` describing the outcome. Failures (unknown
            view, missing template file, Jinja2 errors) are reported via the
            ``errors`` dict rather than raised exceptions.

        Raises:
            PackError: If ``report_id`` is not a known report. This is a
                programming/configuration error and is allowed to propagate
                rather than being folded into ``errors``.
        """
        config: ReportConfig = self._loader.get(report_id)
        return self._renderer.render(
            templates=config.templates,
            view=view,
            data=data,
            report_id=report_id,
        )

    def register_plugin(self, name: str, plugin: PluginBase) -> None:
        """Register an imperative computation plugin.

        Delegates to the underlying :class:`PluginRegistry`. Plugins must be
        registered before :meth:`evaluate` is called with a ``type=plugin``
        rule that references them. Registration is append-only; duplicate
        names are rejected.

        Args:
            name: Unique key under which the plugin is registered.
            plugin: The :class:`PluginBase` instance to register.

        Raises:
            ValueError: If ``name`` is already registered.
        """
        self._plugin_registry.register(name, plugin)

    def list_reports(self) -> list[str]:
        """Return a sorted list of all known global report identifiers.

        Each identifier is ``"<pack_id>:<report_name>"``. Delegates to the
        underlying :class:`PackLoader`.
        """
        return self._loader.list_reports()
