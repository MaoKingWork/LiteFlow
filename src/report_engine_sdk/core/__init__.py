"""Public API for the protocol-agnostic core layer of the report engine SDK."""

from .engine import ReportEngine, EvaluateResult, RenderResult
from .config_provider import (
    PackConfig,
    ConfigProvider,
    FileSystemPackProvider,
    InMemoryPackProvider,
)
from .pack_loader import (
    PackLoader,
    ReportConfig,
    PackError,
    PackNotFoundError,
    PackFormatError,
    PackSelfCheckError,
)
from .validator import SchemaValidator, ValidationResult
from .calculator import RuleCalculator, CalculationResult
from .renderer import TemplateRenderer
from .plugin_registry import PluginRegistry, PluginBase, PluginNotFoundError

__all__ = [
    "ReportEngine", "EvaluateResult", "RenderResult",
    "PackConfig", "ConfigProvider", "FileSystemPackProvider", "InMemoryPackProvider",
    "PackLoader", "ReportConfig",
    "PackError", "PackNotFoundError", "PackFormatError", "PackSelfCheckError",
    "SchemaValidator", "ValidationResult",
    "RuleCalculator", "CalculationResult",
    "TemplateRenderer",
    "PluginRegistry", "PluginBase", "PluginNotFoundError",
]
