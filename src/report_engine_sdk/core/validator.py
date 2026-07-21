"""Schema validator for the report engine SDK.

This module provides a protocol-agnostic JSON Schema-based validator that
surfaces structured information about missing required fields, type
mismatches, and other validation failures, rather than opaque error strings.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Optional

from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError


@dataclass(frozen=True)
class ValidationResult:
    """Immutable result of schema validation.

    Attributes:
        success: ``True`` if facts conform to schema; ``False`` otherwise.
        missing_fields: Required field names absent from facts.
            Empty list when none are missing.
        invalid_types: Type-mismatch descriptors of shape
            ``{"field": <name>, "expected": <type>, "actual": <type>}``.
            Empty list when none are mismatched.
        errors: Combined convenience dict on failure (subset of
            ``{"missing_fields": [...], "invalid_types": [...], "other": [...]}``);
            ``None`` on success.
    """

    success: bool
    missing_fields: list[str] = field(default_factory=list)
    invalid_types: list[dict] = field(default_factory=list)
    errors: Optional[dict] = None


def _format_field_path(path: Iterable) -> str:
    """Join path segments into dotted notation.

    Args:
        path: Iterable of path segments (e.g. a ``deque`` from
            ``ValidationError.absolute_path``).

    Returns:
        Dotted path string (e.g. ``"user.address.city"``); empty string
        when the path is empty.
    """
    return ".".join(str(segment) for segment in path)


class SchemaValidator:
    """Stateless JSON Schema (Draft 7) validator.

    Wraps ``jsonschema.Draft7Validator`` to surface structured information
    about missing required fields and type mismatches, instead of opaque
    error strings. Holds no state across calls and is safe to invoke
    concurrently from multiple threads without external synchronization.
    """

    def __init__(self) -> None:
        """Initialize the validator. No state is held."""
        pass

    def validate(self, schema: dict, facts: dict) -> ValidationResult:
        """Validate ``facts`` against a JSON Schema.

        Args:
            schema: JSON Schema dict (Draft 7).
            facts: Instance dict to validate.

        Returns:
            A ``ValidationResult`` describing missing fields, type
            mismatches, and any other errors encountered.
        """
        missing_fields: list[str] = []
        invalid_types: list[dict] = []
        other_errors: list[str] = []

        draft_validator = Draft7Validator(schema)
        for error in draft_validator.iter_errors(facts):
            if error.validator == "required":
                missing_fields.extend(self._extract_missing_fields(error))
            elif error.validator == "type":
                invalid_types.append(self._build_type_error(error))
            else:
                other_errors.append(error.message)

        has_failures = bool(missing_fields or invalid_types or other_errors)
        if not has_failures:
            return ValidationResult(success=True)

        errors: dict = {}
        if missing_fields:
            errors["missing_fields"] = list(missing_fields)
        if invalid_types:
            errors["invalid_types"] = list(invalid_types)
        if other_errors:
            errors["other"] = list(other_errors)

        return ValidationResult(
            success=False,
            missing_fields=missing_fields,
            invalid_types=invalid_types,
            errors=errors,
        )

    @staticmethod
    def _extract_missing_fields(error: ValidationError) -> list[str]:
        """Extract missing required field names from a ``required`` error.

        Args:
            error: A ``ValidationError`` with ``validator == "required"``.

        Returns:
            List of required field names that are absent from
            ``error.instance``.
        """
        required = error.validator_value or []
        instance = error.instance if isinstance(error.instance, dict) else {}
        return [name for name in required if name not in instance]

    @staticmethod
    def _build_type_error(error: ValidationError) -> dict:
        """Build a structured descriptor for a ``type`` error.

        Args:
            error: A ``ValidationError`` with ``validator == "type"``.

        Returns:
            Dict with ``field`` (dotted path), ``expected`` (schema-declared
            type), and ``actual`` (Python type name of the offending value).
        """
        field_path = _format_field_path(error.absolute_path) or "<root>"
        expected = error.validator_value
        actual = type(error.instance).__name__
        return {
            "field": field_path,
            "expected": expected,
            "actual": actual,
        }
