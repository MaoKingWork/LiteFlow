"""Hybrid rule calculator for the report engine SDK.

This module evaluates a manifest's ``rules`` array, mixing declarative
formula rules (evaluated in a restricted ``simpleeval`` sandbox) with
imperative plugin rules (resolved through the :class:`PluginRegistry`).
Rule failures are collected as structured errors rather than raised, so a
single ``calculate()`` call surfaces every problem in one pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import simpleeval

from .plugin_registry import PluginBase, PluginRegistry


class CalculationError(Exception):
    """Base exception for calculator failures.

    Subclasses surface specific failure modes; callers may catch
    :class:`CalculationError` to handle any calculator-level error
    uniformly.
    """


class FormulaError(CalculationError):
    """Raised when a formula rule cannot be evaluated.

    Note that :class:`RuleCalculator` collects formula failures into
    :attr:`CalculationResult.errors` rather than raising. This exception
    is provided as part of the public API so callers (e.g. adapters) can
    opt into fail-fast behavior by raising it themselves when inspecting
    a failed ``CalculationResult``.

    Attributes:
        rule_name: The ``name`` of the offending rule.
        expression: The expression text that failed to evaluate.
    """

    def __init__(
        self,
        rule_name: str,
        expression: str,
        message: str = "",
    ) -> None:
        """Initialize the formula error.

        Args:
            rule_name: The ``name`` of the offending rule.
            expression: The expression text that failed to evaluate.
            message: Optional human-readable detail. Defaults to a
                generated message naming the rule and expression.
        """
        super().__init__(
            message
            or f"Formula rule '{rule_name}' failed for expression: {expression!r}"
        )
        self.rule_name = rule_name
        self.expression = expression


@dataclass(frozen=True)
class CalculationResult:
    """Immutable result of a rule calculation pass.

    Attributes:
        success: ``True`` if every rule evaluated without errors.
        data: The computed data (input + rule outputs merged) on success;
            ``None`` on failure.
        errors: Structured error dict on failure, shaped as a subset of
            ``{"missing_plugins": [...], "formula_errors": [...],
            "plugin_errors": [...]}`` (only non-empty keys included);
            ``None`` on success.
    """

    success: bool
    data: Optional[dict] = None
    errors: Optional[dict] = None


class RuleCalculator:
    """Stateless evaluator for mixed formula/plugin rules.

    The calculator iterates a manifest's ``rules`` list in order, writing
    each rule's output into a working copy of the input data. Formula
    rules are evaluated through ``simpleeval`` with no exposed functions
    (so dangerous calls such as ``__import__`` or ``open`` cannot run);
    plugin rules delegate to instances registered in a
    :class:`PluginRegistry`.

    Individual rule failures are collected into a structured ``errors``
    dict rather than raised; only truly unexpected internal errors
    propagate to the caller. The calculator holds no per-call state and
    is safe to invoke concurrently from multiple threads.
    """

    def __init__(self, plugin_registry: PluginRegistry) -> None:
        """Initialize the calculator with a plugin registry reference.

        Args:
            plugin_registry: The registry used to resolve ``type=plugin``
                rules. The reference is stored (not copied); plugins
                registered after construction remain visible to
                subsequent ``calculate()`` calls.
        """
        self._registry = plugin_registry

    def calculate(self, rules: list[dict], data: dict) -> CalculationResult:
        """Evaluate ``rules`` against ``data`` and return the merged result.

        Args:
            rules: Ordered list of rule dicts. Each rule has a ``type`` of
                ``"formula"`` (with ``name`` + ``expression``) or
                ``"plugin"`` (with ``name`` + ``plugin``).
            data: The input data context. A shallow copy is made; the
                caller's dict is never mutated.

        Returns:
            A :class:`CalculationResult`. On success, ``data`` holds the
            input plus every rule output. On failure, ``data`` is
            ``None`` and ``errors`` collects every problem encountered.
        """
        current_data: dict = dict(data)

        missing_plugins: list[str] = []
        formula_errors: list[dict] = []
        plugin_errors: list[dict] = []

        for rule in rules:
            rule_type = rule.get("type")
            rule_name = rule.get("name", "<unknown>")

            if rule_type == "formula":
                self._apply_formula(
                    rule_name=rule_name,
                    expression=rule.get("expression", ""),
                    current_data=current_data,
                    formula_errors=formula_errors,
                )
            elif rule_type == "plugin":
                self._apply_plugin(
                    rule_name=rule_name,
                    plugin_name=rule.get("plugin", ""),
                    current_data=current_data,
                    missing_plugins=missing_plugins,
                    plugin_errors=plugin_errors,
                )
            else:
                formula_errors.append(
                    {
                        "rule": rule_name,
                        "error": f"Unknown rule type: {rule_type}",
                    }
                )

        errors: Optional[dict] = None
        if missing_plugins or formula_errors or plugin_errors:
            errors = {}
            if missing_plugins:
                errors["missing_plugins"] = missing_plugins
            if formula_errors:
                errors["formula_errors"] = formula_errors
            if plugin_errors:
                errors["plugin_errors"] = plugin_errors
            return CalculationResult(success=False, data=None, errors=errors)

        return CalculationResult(success=True, data=current_data, errors=None)

    @staticmethod
    def _apply_formula(
        rule_name: str,
        expression: str,
        current_data: dict,
        formula_errors: list[dict],
    ) -> None:
        """Evaluate a single formula rule and merge its result.

        The expression is evaluated with ``simpleeval.simple_eval`` using
        ``current_data`` as the ``names`` dict and no exposed functions,
        so dangerous builtins such as ``__import__`` and ``open`` are
        unreachable. Failures are appended to ``formula_errors`` and
        swallowed so the surrounding loop can continue with the next
        rule.
        """
        try:
            result = simpleeval.simple_eval(expression, names=current_data)
        except simpleeval.InvalidExpression as exc:
            formula_errors.append(
                {
                    "rule": rule_name,
                    "expression": expression,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        except Exception as exc:  # noqa: BLE001 - collect, do not raise
            formula_errors.append(
                {
                    "rule": rule_name,
                    "expression": expression,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return
        current_data[rule_name] = result

    def _apply_plugin(
        self,
        rule_name: str,
        plugin_name: str,
        current_data: dict,
        missing_plugins: list[str],
        plugin_errors: list[dict],
    ) -> None:
        """Resolve and invoke a single plugin rule, merging its output.

        If the plugin is not registered, its name is appended to
        ``missing_plugins`` and the rule is skipped. Any exception raised
        by the plugin is captured into ``plugin_errors`` so the
        surrounding loop can continue with the next rule.
        """
        if not self._registry.has(plugin_name):
            missing_plugins.append(plugin_name)
            return

        plugin: PluginBase = self._registry.get(plugin_name)
        try:
            result = plugin.run(context=current_data)
        except Exception as exc:  # noqa: BLE001 - collect, do not raise
            plugin_errors.append(
                {
                    "rule": rule_name,
                    "plugin": plugin_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return

        if isinstance(result, dict):
            current_data.update(result)
            if rule_name not in result:
                current_data[rule_name] = result
        else:
            current_data[rule_name] = result
