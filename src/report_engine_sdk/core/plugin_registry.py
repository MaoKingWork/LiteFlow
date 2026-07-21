"""Plugin registry for the report engine SDK.

This module provides the :class:`PluginBase` abstract class and the
:class:`PluginRegistry` container that allows adapter layers to inject
imperative computation plugins during engine initialization. Plugin rules
declared in a manifest reference plugins by name; the registry resolves those
names to concrete instances at calculation time.
"""

from abc import ABC, abstractmethod


class PluginBase(ABC):
    """Abstract base class for all imperative computation plugins.

    Subclasses must implement :meth:`run`, which receives the current data
    context produced by upstream schema validation and formula rules, and
    returns a dictionary of new fields to merge back into the context.
    """

    @abstractmethod
    def run(self, context: dict) -> dict:
        """Execute the plugin against the current data context.

        :param context: The current data context accumulated by the engine.
        :returns: A dictionary of new fields to merge into the context.
        """
        ...


class PluginNotFoundError(Exception):
    """Raised when a referenced plugin name is not present in the registry.

    :param plugin_name: The unresolved plugin name.
    """

    def __init__(self, plugin_name: str) -> None:
        super().__init__(
            f"Plugin '{plugin_name}' is not registered. "
            f"Register it via engine.register_plugin() before use."
        )
        self.plugin_name = plugin_name


class PluginRegistry:
    """A name-indexed container for :class:`PluginBase` instances.

    The registry is populated during engine initialization (typically by the
    adapter layer) and queried by the rule calculator when a manifest rule of
    ``type=plugin`` is encountered. Duplicate registration is rejected to
    prevent silent overrides of imperative business logic.
    """

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._plugins: dict[str, PluginBase] = {}

    def register(self, name: str, plugin: PluginBase) -> None:
        """Register a plugin instance under ``name``.

        :param name: The unique key under which the plugin is registered.
        :param plugin: The :class:`PluginBase` instance to store.
        :raises ValueError: If ``name`` is already registered.
        """
        if name in self._plugins:
            raise ValueError(f"Plugin '{name}' is already registered")
        self._plugins[name] = plugin

    def get(self, name: str) -> PluginBase:
        """Return the plugin registered under ``name``.

        :param name: The registered plugin name to look up.
        :returns: The stored :class:`PluginBase` instance.
        :raises PluginNotFoundError: If ``name`` is not registered.
        """
        try:
            return self._plugins[name]
        except KeyError:
            raise PluginNotFoundError(name) from None

    def has(self, name: str) -> bool:
        """Return whether a plugin is registered under ``name``."""
        return name in self._plugins

    def names(self) -> list[str]:
        """Return a sorted list of all registered plugin names.

        Sorting makes the output stable, which is convenient for debugging
        and deterministic assertions in tests.
        """
        return sorted(self._plugins)
