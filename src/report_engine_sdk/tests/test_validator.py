"""Tests for ``report_engine_sdk.core.validator.SchemaValidator``."""
import pytest

from report_engine_sdk.core.validator import SchemaValidator, ValidationResult


@pytest.fixture
def validator() -> SchemaValidator:
    return SchemaValidator()


@pytest.fixture
def person_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
        },
        "required": ["name"],
    }


def test_validate_success(
    validator: SchemaValidator, person_schema: dict
) -> None:
    result = validator.validate(person_schema, {"name": "Alice", "age": 30})

    assert result.success is True
    assert result.missing_fields == []
    assert result.invalid_types == []
    assert result.errors is None


def test_validate_missing_required(
    validator: SchemaValidator, person_schema: dict
) -> None:
    result = validator.validate(person_schema, {"age": 30})

    assert result.success is False
    assert result.missing_fields == ["name"]
    assert isinstance(result.errors, dict)
    assert result.errors.get("missing_fields") == ["name"]


def test_validate_invalid_type(
    validator: SchemaValidator, person_schema: dict
) -> None:
    result = validator.validate(
        person_schema, {"name": "Alice", "age": "thirty"}
    )

    assert result.success is False
    assert len(result.invalid_types) == 1
    type_error = result.invalid_types[0]
    assert type_error["field"] == "age"
    assert type_error["expected"] == "integer"
    assert type_error["actual"] == "str"
    assert isinstance(result.errors, dict)
    assert result.errors.get("invalid_types") == result.invalid_types


def test_validate_multiple_errors(
    validator: SchemaValidator, person_schema: dict
) -> None:
    result = validator.validate(person_schema, {"age": "thirty"})

    assert result.success is False
    assert "name" in result.missing_fields
    assert any(
        err["field"] == "age" and err["expected"] == "integer"
        for err in result.invalid_types
    )
    assert isinstance(result.errors, dict)
    assert "missing_fields" in result.errors
    assert "invalid_types" in result.errors


def test_validate_empty_schema(validator: SchemaValidator) -> None:
    result = validator.validate({}, {"anything": "value"})

    assert result.success is True
    assert result.missing_fields == []
    assert result.invalid_types == []
    assert result.errors is None


def test_validation_result_is_frozen() -> None:
    result = ValidationResult(success=True)
    with pytest.raises(Exception):
        result.success = False  # type: ignore[misc]
