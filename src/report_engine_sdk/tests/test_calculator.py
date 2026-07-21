"""Unit tests for :mod:`report_engine_sdk.core.calculator`."""

import pytest

from report_engine_sdk.core.calculator import (
    CalculationError,
    CalculationResult,
    FormulaError,
    RuleCalculator,
)
from report_engine_sdk.core.plugin_registry import PluginBase, PluginRegistry


class StubPlugin(PluginBase):
    """Configurable plugin fixture returning a fixed payload dict."""

    def __init__(self, payload: dict) -> None:
        """Store the payload to return from :meth:`run`."""
        self.payload = payload

    def run(self, context: dict) -> dict:
        """Return a shallow copy of the configured payload."""
        return dict(self.payload)


def _make_calculator() -> tuple[RuleCalculator, PluginRegistry]:
    """Return a fresh calculator and its underlying registry."""
    registry = PluginRegistry()
    return RuleCalculator(registry), registry


def test_formula_rule() -> None:
    calc, _ = _make_calculator()
    rules = [
        {
            "name": "total",
            "type": "formula",
            "expression": "base_score * 0.8 + bonus",
        }
    ]
    data = {"base_score": 100, "bonus": 10}
    result = calc.calculate(rules, data)
    assert result.success is True
    assert result.errors is None
    assert result.data is not None
    assert result.data["total"] == pytest.approx(90.0)
    # input fields preserved in the merged output
    assert result.data["base_score"] == 100
    assert result.data["bonus"] == 10


def test_plugin_rule() -> None:
    calc, registry = _make_calculator()
    registry.register("fetch_history", StubPlugin({"history": "some history"}))
    rules = [
        {"name": "history", "type": "plugin", "plugin": "fetch_history"}
    ]
    result = calc.calculate(rules, {})
    assert result.success is True
    assert result.errors is None
    assert result.data is not None
    assert result.data["history"] == "some history"


def test_unregistered_plugin() -> None:
    calc, _ = _make_calculator()
    rules = [{"name": "x", "type": "plugin", "plugin": "missing"}]
    result = calc.calculate(rules, {})
    assert result.success is False
    assert result.data is None
    assert result.errors is not None
    assert result.errors["missing_plugins"] == ["missing"]


def test_empty_rules_passthrough() -> None:
    calc, _ = _make_calculator()
    data = {"a": 1}
    result = calc.calculate([], data)
    assert result.success is True
    assert result.errors is None
    assert result.data == {"a": 1}
    # original input dict must NOT be mutated
    assert data == {"a": 1}


def test_dangerous_expression_blocked() -> None:
    calc, _ = _make_calculator()
    rules = [
        {
            "name": "x",
            "type": "formula",
            "expression": "__import__('os').system('echo hacked')",
        }
    ]
    result = calc.calculate(rules, {})
    assert result.success is False
    assert result.data is None
    assert result.errors is not None
    assert "formula_errors" in result.errors
    formula_errors = result.errors["formula_errors"]
    assert len(formula_errors) == 1
    assert formula_errors[0]["rule"] == "x"
    assert formula_errors[0]["expression"] == rules[0]["expression"]
    # simpleeval must have refused the expression (no command should run)
    assert "error" in formula_errors[0]
    assert isinstance(formula_errors[0]["error"], str)
    assert len(formula_errors[0]["error"]) > 0


def test_input_not_mutated() -> None:
    calc, _ = _make_calculator()
    rules = [{"name": "total", "type": "formula", "expression": "a + b"}]
    data = {"a": 1, "b": 2}
    result = calc.calculate(rules, data)
    assert result.success is True
    assert result.data is not None
    assert result.data["total"] == 3
    # original input dict must remain unchanged
    assert data == {"a": 1, "b": 2}
    assert "total" not in data


def test_formula_with_undefined_name() -> None:
    calc, _ = _make_calculator()
    rules = [
        {"name": "x", "type": "formula", "expression": "undefined_var + 1"}
    ]
    result = calc.calculate(rules, {})
    assert result.success is False
    assert result.data is None
    assert result.errors is not None
    assert "formula_errors" in result.errors
    formula_errors = result.errors["formula_errors"]
    assert len(formula_errors) == 1
    assert formula_errors[0]["rule"] == "x"


def test_unknown_rule_type_collected() -> None:
    calc, _ = _make_calculator()
    rules = [{"name": "x", "type": "weird"}]
    result = calc.calculate(rules, {"a": 1})
    assert result.success is False
    assert result.errors is not None
    assert "formula_errors" in result.errors
    assert result.errors["formula_errors"][0]["rule"] == "x"


def test_calculation_result_is_frozen() -> None:
    result = CalculationResult(success=True, data={"a": 1}, errors=None)
    with pytest.raises(Exception):
        result.success = False  # type: ignore[misc]


def test_formula_error_attributes() -> None:
    err = FormulaError(rule_name="total", expression="a + b")
    assert isinstance(err, CalculationError)
    assert err.rule_name == "total"
    assert err.expression == "a + b"
    assert "total" in str(err)
    assert "a + b" in str(err)


def test_plugin_exception_collected() -> None:
    class BoomPlugin(PluginBase):
        def run(self, context: dict) -> dict:
            raise RuntimeError("boom")

    calc, registry = _make_calculator()
    registry.register("boom", BoomPlugin())
    rules = [{"name": "x", "type": "plugin", "plugin": "boom"}]
    result = calc.calculate(rules, {})
    assert result.success is False
    assert result.errors is not None
    assert "plugin_errors" in result.errors
    plugin_errors = result.errors["plugin_errors"]
    assert len(plugin_errors) == 1
    assert plugin_errors[0]["rule"] == "x"
    assert plugin_errors[0]["plugin"] == "boom"
    assert "boom" in plugin_errors[0]["error"]


def test_multiple_errors_collected_together() -> None:
    calc, _ = _make_calculator()
    rules = [
        {"name": "x", "type": "plugin", "plugin": "missing"},
        {"name": "y", "type": "formula", "expression": "undefined_var + 1"},
        {"name": "z", "type": "weird"},
    ]
    result = calc.calculate(rules, {})
    assert result.success is False
    assert result.errors is not None
    assert result.errors["missing_plugins"] == ["missing"]
    assert len(result.errors["formula_errors"]) == 2
    # empty keys must NOT be present
    assert "plugin_errors" not in result.errors
