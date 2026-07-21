"""Unit tests for :mod:`report_engine_sdk.core.plugin_registry`."""

import pytest

from report_engine_sdk.core.plugin_registry import (
    PluginBase,
    PluginNotFoundError,
    PluginRegistry,
)


class StubPlugin(PluginBase):
    """Minimal plugin fixture used across the registry tests."""

    def run(self, context: dict) -> dict:
        return {"computed": context.get("x", 0) * 2}


def test_register_and_get() -> None:
    registry = PluginRegistry()
    plugin = StubPlugin()
    registry.register("stub", plugin)
    assert registry.get("stub") is plugin


def test_get_not_found() -> None:
    registry = PluginRegistry()
    with pytest.raises(PluginNotFoundError):
        registry.get("missing")


def test_register_duplicate() -> None:
    registry = PluginRegistry()
    registry.register("stub", StubPlugin())
    with pytest.raises(ValueError):
        registry.register("stub", StubPlugin())


def test_has() -> None:
    registry = PluginRegistry()
    assert registry.has("stub") is False
    registry.register("stub", StubPlugin())
    assert registry.has("stub") is True


def test_names() -> None:
    registry = PluginRegistry()
    registry.register("zeta", StubPlugin())
    registry.register("alpha", StubPlugin())
    registry.register("mu", StubPlugin())
    assert registry.names() == ["alpha", "mu", "zeta"]


def test_plugin_base_is_abstract() -> None:
    with pytest.raises(TypeError):
        PluginBase()  # type: ignore[abstract]
