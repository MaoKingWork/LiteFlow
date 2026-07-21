"""Pack loader with self-check for the report engine SDK.

This module belongs to the protocol-agnostic core layer and depends only on
the Python standard library and ``jinja2``. It consumes pack declarations from
any :class:`~report_engine_sdk.core.config_provider.ConfigProvider`, builds
immutable :class:`ReportConfig` objects (merging shared variables, expanding
shared-rule references, and resolving template paths to absolute form), and
runs a self-check that verifies every Jinja2 template variable can be
satisfied by either the report's ``input_schema`` properties or a rule output.
Configuration drift is therefore surfaced at engine startup rather than at
render time.

A report's global identifier is ``"<pack_id>:<report_name>"`` -- unique across
all packs -- so each pack can evolve independently while the engine exposes a
single flat namespace to callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment
from jinja2.meta import find_undeclared_variables

from .config_provider import (
    ConfigProvider,
    PackConfig,
    PackError,
    PackFormatError,
    PackNotFoundError,
)


class PackSelfCheckError(PackError):
    """Raised when a pack self-check fails.

    The self-check fails when a template variable cannot be satisfied by the
    report's ``input_schema`` or rule outputs, when a referenced template file
    is missing, or when an unknown report id is requested.
    """


@dataclass(frozen=True)
class ReportConfig:
    """Immutable representation of a single fully-resolved report declaration.

    Attributes:
        report_id: Global identifier ``"<pack_id>:<report_name>"``.
        pack_id: Identifier of the owning pack.
        name: Local report name within its pack.
        input_schema: JSON-schema-like dict describing accepted input fields,
            with the pack's shared variables already merged in.
        rules: Ordered list of rule declarations (formula or plugin) with
            ``{"ref": ...}`` entries expanded to their shared-rule bodies. May
            be empty for a pass-through report.
        templates: Mapping of view name to ``{"path": <abs path>, ...}``. Paths
            are absolute and guaranteed to exist at load time.
    """

    report_id: str
    pack_id: str
    name: str
    input_schema: dict[str, Any]
    rules: list[dict[str, Any]]
    templates: dict[str, dict[str, Any]]


class PackLoader:
    """Loads every pack from a provider and runs self-checks at construction.

    The loader performs eager loading: pulling every :class:`PackConfig` from
    the injected :class:`ConfigProvider`, merging shared variables, expanding
    shared-rule references, resolving template paths to absolute form, and
    verifying that every Jinja2 template variable is satisfiable. Any
    configuration drift is surfaced at engine startup rather than at render
    time.

    Because loading is driven by a provider, the loader is agnostic to whether
    packs come from the filesystem, memory, or any future source.
    """

    def __init__(self, provider: ConfigProvider) -> None:
        """Initialize the loader and eagerly load every pack.

        Args:
            provider: The configuration provider that discovers packs.

        Raises:
            PackError: If any pack is malformed or fails its self-check.
        """
        self._provider = provider
        self._reports: dict[str, ReportConfig] = {}
        self._load()

    def _load(self) -> None:
        """Load, resolve, self-check, and cache every report from every pack.

        Raises:
            PackFormatError: If a report is missing required keys, references
                an unknown shared rule, or produces a duplicate report id.
            PackSelfCheckError: If a self-check fails for any report.
        """
        for pack in self._provider.list_packs():
            for report_name, raw_report in pack.reports.items():
                if not isinstance(raw_report, dict):
                    raise PackFormatError(
                        f"Report '{pack.pack_id}:{report_name}' config must "
                        f"be an object"
                    )
                required_keys = {"input_schema", "rules", "templates"}
                missing = required_keys - set(raw_report.keys())
                if missing:
                    raise PackFormatError(
                        f"Report '{pack.pack_id}:{report_name}' is missing "
                        f"required keys: {sorted(missing)}"
                    )

                input_schema = _merge_input_schema(
                    pack.shared_variables, raw_report["input_schema"]
                )
                rules = _expand_rules(
                    raw_report["rules"],
                    pack.shared_rules,
                    pack.pack_id,
                    report_name,
                )
                templates = _resolve_templates(
                    raw_report["templates"], pack.pack_dir, pack.pack_id
                )

                report_id = f"{pack.pack_id}:{report_name}"
                if report_id in self._reports:
                    raise PackFormatError(
                        f"Duplicate report id '{report_id}' (pack "
                        f"'{pack.pack_id}' collides with an earlier pack)"
                    )

                config = ReportConfig(
                    report_id=report_id,
                    pack_id=pack.pack_id,
                    name=report_name,
                    input_schema=input_schema,
                    rules=rules,
                    templates=templates,
                )
                self._self_check(config)
                self._reports[report_id] = config

    def _self_check(self, config: ReportConfig) -> None:
        """Verify each template's variables are satisfiable.

        For every view, the referenced template file must exist (its absolute
        path was resolved at load time; existence is re-checked here for
        safety) and every Jinja2 undeclared variable must be present in
        ``input_schema.properties`` or produced by a rule (i.e. match a
        ``rule["name"]``).

        Args:
            config: The report configuration to validate.

        Raises:
            PackSelfCheckError: If a template file is missing or a template
                references a variable that is not satisfiable.
        """
        available_vars: set[str] = set()
        properties = config.input_schema.get("properties", {})
        if isinstance(properties, dict):
            available_vars.update(properties.keys())
        for rule in config.rules:
            if isinstance(rule, dict) and rule.get("name"):
                available_vars.add(rule["name"])

        env = Environment()

        for view, view_config in config.templates.items():
            path_str = view_config.get("path", "")
            resolved = Path(path_str)
            if not resolved.is_absolute() or not resolved.exists():
                raise PackSelfCheckError(
                    f"Template file not found for report '{config.report_id}' "
                    f"view '{view}': {path_str}"
                )

            content = resolved.read_text(encoding="utf-8")
            ast = env.parse(content)
            used_vars = find_undeclared_variables(ast)

            for var in used_vars:
                if var not in available_vars:
                    raise PackSelfCheckError(
                        f"Template '{path_str}' for view '{view}' of report "
                        f"'{config.report_id}' uses variable '{var}' which is "
                        f"not defined in input_schema or produced by rules"
                    )

    def get(self, report_id: str) -> ReportConfig:
        """Return the cached :class:`ReportConfig` for ``report_id``.

        Args:
            report_id: Global identifier ``"<pack_id>:<report_name>"``.

        Returns:
            The cached :class:`ReportConfig` for the requested report.

        Raises:
            PackSelfCheckError: If ``report_id`` is not a known report.
        """
        if report_id not in self._reports:
            raise PackSelfCheckError(
                f"Unknown report_id: '{report_id}'. "
                f"Available: {sorted(self._reports.keys())}"
            )
        return self._reports[report_id]

    def list_reports(self) -> list[str]:
        """Return a sorted list of all known global report identifiers."""
        return sorted(self._reports.keys())


def _merge_input_schema(
    shared: dict[str, Any], report_schema: dict[str, Any]
) -> dict[str, Any]:
    """Merge a pack's shared variables into a report's ``input_schema``.

    Shared ``properties`` are merged underneath the report's properties (the
    report wins on key collision), and ``required`` lists are unioned. Other
    schema keys (``type``, ``additionalProperties``, ...) are taken from the
    report schema. When the pack declares no shared variables the report
    schema is returned unchanged.

    Args:
        shared: The pack's shared-variables fragment (may be empty).
        report_schema: The report's own ``input_schema``.

    Returns:
        A new merged schema dict; inputs are not mutated.
    """
    if not shared:
        return dict(report_schema)

    merged = dict(report_schema)
    shared_props = shared.get("properties", {})
    report_props = report_schema.get("properties", {})
    if shared_props or report_props:
        merged_props = dict(shared_props) if isinstance(shared_props, dict) else {}
        if isinstance(report_props, dict):
            merged_props.update(report_props)
        merged["properties"] = merged_props

    shared_required = shared.get("required", [])
    report_required = report_schema.get("required", [])
    if shared_required or report_required:
        merged_required = list(shared_required) + list(report_required)
        # De-duplicate while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for field_name in merged_required:
            if field_name not in seen:
                seen.add(field_name)
                deduped.append(field_name)
        merged["required"] = deduped

    return merged


def _expand_rules(
    rules: list[Any],
    shared_rules: dict[str, dict[str, Any]],
    pack_id: str,
    report_name: str,
) -> list[dict[str, Any]]:
    """Expand ``{"ref": NAME}`` entries against the pack's shared rules.

    Each ``{"ref": NAME}`` is replaced by ``{"name": NAME, **shared_rules[NAME]}``
    in place (preserving pipeline order, so a shared rule can depend on an
    earlier rule's output). Inline rule dicts are copied unchanged.

    Args:
        rules: The report's raw ``rules`` list.
        shared_rules: The pack's shared-rule library (name -> body).
        pack_id: Pack id, for error messages.
        report_name: Report name, for error messages.

    Returns:
        A new list of expanded rule dicts.

    Raises:
        PackFormatError: If a ``ref`` targets a name absent from
            ``shared_rules``, or a rule entry is not an object.
    """
    expanded: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise PackFormatError(
                f"Report '{pack_id}:{report_name}' has a rule that is not an "
                f"object: {rule!r}"
            )
        if "ref" in rule:
            ref_name = rule["ref"]
            if ref_name not in shared_rules:
                raise PackFormatError(
                    f"Report '{pack_id}:{report_name}' references unknown "
                    f"shared rule '{ref_name}'"
                )
            body = shared_rules[ref_name]
            expanded_rule: dict[str, Any] = {"name": ref_name}
            expanded_rule.update(body)
            expanded.append(expanded_rule)
        else:
            expanded.append(dict(rule))
    return expanded


def _resolve_templates(
    templates: dict[str, Any],
    pack_dir: Path,
    pack_id: str,
) -> dict[str, dict[str, Any]]:
    """Resolve every template ``path`` to an absolute, existing filesystem path.

    Resolution order for each path value:
        1. Absolute paths are used as-is (existence required).
        2. ``pack_dir / path``
        3. ``pack_dir / "templates" / path``

    Args:
        templates: The report's raw ``templates`` mapping (view -> meta).
        pack_dir: The pack directory, used for relative resolution.
        pack_id: Pack id, for error messages.

    Returns:
        A new mapping where each view's ``path`` is an absolute path string
        pointing at an existing file; extra meta keys (e.g. ``prompt``) are
        preserved.

    Raises:
        PackFormatError: If the templates mapping is not an object or a view
            meta is malformed.
        PackSelfCheckError: If a template file cannot be resolved.
    """
    if not isinstance(templates, dict):
        raise PackFormatError(
            f"Pack '{pack_id}' has a report whose 'templates' is not an object"
        )

    resolved_map: dict[str, dict[str, Any]] = {}
    for view, view_config in templates.items():
        if not isinstance(view_config, dict) or "path" not in view_config:
            raise PackFormatError(
                f"Pack '{pack_id}' view '{view}' must be an object with a "
                f"'path' field"
            )
        path_str = view_config["path"]
        resolved = _resolve_template_path(path_str, pack_dir)
        if resolved is None:
            raise PackSelfCheckError(
                f"Template file not found for pack '{pack_id}' view '{view}': "
                f"{path_str}"
            )
        new_meta = dict(view_config)
        new_meta["path"] = str(resolved)
        resolved_map[view] = new_meta
    return resolved_map


def _resolve_template_path(
    path_str: str, pack_dir: Path
) -> Optional[Path]:
    """Resolve a template ``path`` value to a concrete filesystem path.

    Resolution order:
        1. Absolute paths are used as-is.
        2. ``pack_dir / path``
        3. ``pack_dir / "templates" / path``

    Returns the first existing path, or ``None`` if no candidate exists.
    """
    candidate = Path(path_str)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    direct = pack_dir / path_str
    if direct.exists():
        return direct
    nested = pack_dir / "templates" / path_str
    if nested.exists():
        return nested
    return None


__all__ = [
    "PackLoader",
    "ReportConfig",
    "PackSelfCheckError",
    "PackError",
    "PackNotFoundError",
    "PackFormatError",
]
