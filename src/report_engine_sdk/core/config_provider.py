"""Configuration providers for the pack-based report engine SDK.

This module belongs to the protocol-agnostic core layer. It introduces the
**report pack** abstraction: a self-contained directory that groups every
report, template, shared variable, and shared rule belonging to one *purpose*
(e.g. learning reports, ops reports). Each pack is independently owned and
versioned, which avoids the multi-editor conflicts, ambiguous ownership, and
lifecycle coupling that a single monolithic ``manifest.json`` produces.

The module defines three things:

* :class:`PackConfig` -- an immutable, fully-parsed pack declaration (raw
  report dicts plus already-loaded shared variables/rules and a pack directory
  used to resolve relative template paths).
* :class:`ConfigProvider` -- a protocol that *discovers* packs and returns
  :class:`PackConfig` objects. The engine consumes any provider, so
  configuration can be supplied from the filesystem, from memory (tests /
  dynamic injection), or from any future source (DB, object storage) without
  the core layer changing.
* :class:`FileSystemPackProvider` and :class:`InMemoryPackProvider` -- two
  concrete providers. The filesystem provider scans ``<config_path>/packs/*``
  (or reads an explicit ``<config_path>/root.json`` enable-list); the in-memory
  provider hands back caller-supplied :class:`PackConfig` objects.

Shared variables / rules may be declared in a pack either as a file path
(string, resolved relative to the pack directory) or inline (dict), so an
in-memory provider can be fully self-contained without touching disk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Union


class PackError(Exception):
    """Base exception for pack loading and self-check failures."""


class PackNotFoundError(PackError):
    """Raised when a pack directory or its ``pack.json`` does not exist."""


class PackFormatError(PackError):
    """Raised when a pack declaration is invalid JSON or is missing required
    sections, references an unknown shared rule, or carries a malformed shared
    variable/rule definition."""


@dataclass(frozen=True)
class PackConfig:
    """Immutable representation of one fully-parsed report pack.

    A pack groups every report serving a single *purpose*. It carries the raw
    report declarations plus shared variables and shared rules that are merged
    into / referenced by the pack's reports at load time.

    Attributes:
        pack_id: Unique identifier of the pack (also the namespace prefix of
            its reports' global ids, ``"<pack_id>:<report_name>"``).
        purpose: Human-readable description of the pack's use case.
        version: Pack version string (informational).
        owner: Owning team / contact (informational).
        pack_dir: Directory used to resolve relative template paths and
            file-based shared definitions. May be a sentinel (e.g. the current
            directory) for in-memory providers whose templates already use
            absolute paths.
        reports: Mapping of local report name -> raw report dict (each shaped
            as ``{"input_schema": ..., "rules": [...], "templates": {...}}``).
            Shared-variable merging, rule-ref expansion, and template path
            resolution are performed later by :class:`~.pack_loader.PackLoader`.
        shared_variables: JSON-schema fragment (``{"properties": {...},
            "required": [...]}``) merged into every report's ``input_schema``.
            Empty dict when the pack declares none.
        shared_rules: Mapping of rule name -> rule body (``{"type": ...,
            "expression": ...}``, without a ``name`` key). Referenced from a
            report's ``rules`` via ``{"ref": "<name>"}``. Empty dict when the
            pack declares none.
    """

    pack_id: str
    purpose: str
    version: str
    owner: str
    pack_dir: Path
    reports: dict[str, dict[str, Any]]
    shared_variables: dict[str, Any] = field(default_factory=dict)
    shared_rules: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict, pack_dir: Path) -> "PackConfig":
        """Build a :class:`PackConfig` from a pack.json-shaped dict.

        Shared variables / rules may be supplied either as a file path string
        (resolved relative to ``pack_dir``) or as an inline dict. This factory
        is shared by :class:`FileSystemPackProvider` (file-based) and by tests
        / in-memory providers that want the same parsing semantics.

        Args:
            raw: The parsed ``pack.json`` content. Must contain ``pack_id`` and
                ``reports``.
            pack_dir: Directory used to resolve file-based shared definitions
                and (later) relative template paths.

        Raises:
            PackFormatError: If ``raw`` is missing required keys or a shared
                definition file cannot be read/parsed.
        """
        if not isinstance(raw, dict):
            raise PackFormatError("Pack declaration must be a JSON object")
        if "pack_id" not in raw or not isinstance(raw["pack_id"], str):
            raise PackFormatError("Pack is missing required 'pack_id' string")
        if "reports" not in raw or not isinstance(raw["reports"], dict):
            raise PackFormatError(
                f"Pack '{raw['pack_id']}' is missing required 'reports' object"
            )

        shared_variables = _load_shared(
            raw.get("shared_variables"),
            pack_dir,
            raw["pack_id"],
            "shared_variables",
        )
        shared_rules = _load_shared(
            raw.get("shared_rules"),
            pack_dir,
            raw["pack_id"],
            "shared_rules",
        )
        if not isinstance(shared_rules, dict):
            raise PackFormatError(
                f"Pack '{raw['pack_id']}' shared_rules must be an object"
            )

        return cls(
            pack_id=raw["pack_id"],
            purpose=str(raw.get("purpose", "")),
            version=str(raw.get("version", "")),
            owner=str(raw.get("owner", "")),
            pack_dir=Path(pack_dir),
            reports=raw["reports"],
            shared_variables=shared_variables,
            shared_rules=shared_rules,
        )


def _load_shared(
    value: Union[str, dict, None],
    pack_dir: Path,
    pack_id: str,
    kind: str,
) -> dict:
    """Resolve a shared-variables / shared-rules declaration to a dict.

    ``value`` may be a file path string (loaded as JSON relative to
    ``pack_dir``), an inline dict (returned as-is), or ``None`` (empty dict).

    Args:
        value: The raw declaration from the pack.
        pack_dir: Directory to resolve file paths against.
        pack_id: Pack id, used for error messages.
        kind: ``"shared_variables"`` or ``"shared_rules"`` (error messages).

    Returns:
        The resolved dict (possibly empty).

    Raises:
        PackFormatError: If a referenced file cannot be read or is not a JSON
            object.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        path = Path(value)
        if not path.is_absolute():
            path = pack_dir / path
        if not path.exists():
            raise PackFormatError(
                f"Pack '{pack_id}' {kind} file not found: {value}"
            )
        try:
            with path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except json.JSONDecodeError as exc:
            raise PackFormatError(
                f"Pack '{pack_id}' {kind} file has invalid JSON: {exc}"
            ) from exc
        if not isinstance(loaded, dict):
            raise PackFormatError(
                f"Pack '{pack_id}' {kind} file must contain a JSON object"
            )
        return loaded
    raise PackFormatError(
        f"Pack '{pack_id}' {kind} must be a file path string or an object"
    )


class ConfigProvider(Protocol):
    """Protocol for discovering report packs.

    Implementations *discover* packs and return fully-parsed
    :class:`PackConfig` objects (shared variables/rules already loaded). The
    :class:`~.pack_loader.PackLoader` consumes any provider, so configuration
    can be supplied from the filesystem, from memory, or from any future source
    without the core layer changing.
    """

    def list_packs(self) -> list[PackConfig]:
        """Return every discovered pack as a :class:`PackConfig`."""
        ...


class FileSystemPackProvider:
    """Discovers packs from a filesystem configuration directory.

    Discovery rules:

        1. If ``<config_path>/root.json`` exists, it must contain
           ``{"packs": ["<rel_path>", ...]}``; each entry is resolved against
           ``config_path`` and must point at a directory containing
           ``pack.json``. This lets operators enable/disable packs without
           deleting their directories.
        2. Otherwise every ``<config_path>/packs/*/pack.json`` is loaded.

    Each ``pack.json`` is parsed via :meth:`PackConfig.from_raw`, which resolves
    file-based shared variables / rules relative to the pack directory.
    """

    def __init__(self, config_path: str) -> None:
        """Initialize the provider.

        Args:
            config_path: Directory containing either a ``root.json`` enable-list
                or a ``packs/`` subdirectory of pack folders.
        """
        self._config_path = Path(config_path)

    def list_packs(self) -> list[PackConfig]:
        """Discover and return every enabled pack.

        Raises:
            PackNotFoundError: If ``config_path`` does not exist, or a pack
                referenced by ``root.json`` has no ``pack.json``.
            PackFormatError: If a ``pack.json`` is malformed.
        """
        if not self._config_path.exists():
            raise PackNotFoundError(str(self._config_path))

        pack_dirs = self._discover_pack_dirs()
        packs: list[PackConfig] = []
        for pack_dir in pack_dirs:
            pack_json = pack_dir / "pack.json"
            if not pack_json.exists():
                raise PackNotFoundError(
                    f"pack.json not found in pack dir: {pack_dir}"
                )
            try:
                with pack_json.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except json.JSONDecodeError as exc:
                raise PackFormatError(
                    f"Invalid JSON in {pack_json}: {exc}"
                ) from exc
            packs.append(PackConfig.from_raw(raw, pack_dir))
        return packs

    def _discover_pack_dirs(self) -> list[Path]:
        """Return the sorted list of pack directories to load."""
        root_json = self._config_path / "root.json"
        if root_json.exists():
            try:
                with root_json.open("r", encoding="utf-8") as fh:
                    root = json.load(fh)
            except json.JSONDecodeError as exc:
                raise PackFormatError(
                    f"Invalid JSON in {root_json}: {exc}"
                ) from exc
            if not isinstance(root, dict) or "packs" not in root:
                raise PackFormatError(
                    f"{root_json} must contain a 'packs' array"
                )
            pack_list = root["packs"]
            if not isinstance(pack_list, list):
                raise PackFormatError(
                    f"{root_json} 'packs' must be an array of paths"
                )
            return [self._config_path / entry for entry in pack_list]

        packs_root = self._config_path / "packs"
        if not packs_root.exists():
            return []
        return sorted(
            child
            for child in packs_root.iterdir()
            if child.is_dir() and (child / "pack.json").exists()
        )


class InMemoryPackProvider:
    """Provider that returns caller-supplied :class:`PackConfig` objects.

    Useful for tests, dynamic injection, and multi-tenant scenarios where packs
    are assembled in code (e.g. from a database) rather than read from disk.
    Callers typically build configs via :meth:`PackConfig.from_raw` (sharing
    the same parsing semantics as the filesystem provider) or construct
    :class:`PackConfig` directly with inline shared definitions.
    """

    def __init__(self, packs: list[PackConfig]) -> None:
        """Initialize the provider with a list of packs.

        Args:
            packs: The pack configs to expose. Order is preserved.
        """
        self._packs = list(packs)

    def list_packs(self) -> list[PackConfig]:
        """Return the supplied pack configs (a copy of the list)."""
        return list(self._packs)


__all__ = [
    "PackConfig",
    "ConfigProvider",
    "FileSystemPackProvider",
    "InMemoryPackProvider",
    "PackError",
    "PackNotFoundError",
    "PackFormatError",
]
