"""report_engine_sdk: A protocol-agnostic, config-driven Markdown report generation engine.

The SDK provides a pure-Python core layer (``core/``) that is free of any framework
dependencies, an infrastructure layer (``storage/``) abstracting file persistence, and
an adapter layer (``adapters/``) for integrating with MCP, LangChain, and FastAPI.

Reports are organized into **packs** -- one pack per *purpose* (e.g. learning reports,
ops reports). Each pack is a self-contained directory with its own ``pack.json``,
templates, and optional shared variables / shared rules, so different packs can be
owned and versioned independently. A pack's reports are identified globally as
``"<pack_id>:<report_name>"``. The ``pack.json`` of each report binds together an
``input_schema``, a ``rules`` pipeline (declarative formulas + imperative plugins,
with ``{"ref": ...}`` references to the pack's shared rules), and a multi-view
``templates`` mapping. The ``ReportEngine`` facade orchestrates a stateless
evaluate -> render pipeline, natively supporting multi-role reports, single standard
reports, and agent briefings via a generalized ``view`` parameter.

Configuration is supplied through a :class:`ConfigProvider` (filesystem or in-memory),
so the same engine works with on-disk packs, programmatically-built packs, or any
future source.
"""

from .core import (
    ReportEngine,
    EvaluateResult,
    RenderResult,
    PackConfig,
    ConfigProvider,
    FileSystemPackProvider,
    InMemoryPackProvider,
    PackLoader,
    ReportConfig,
    PackError,
)
from .storage import StorageBackend, LocalStorage, MemoryStorage, S3Storage

__all__ = [
    "ReportEngine", "EvaluateResult", "RenderResult",
    "PackConfig", "ConfigProvider", "FileSystemPackProvider", "InMemoryPackProvider",
    "PackLoader", "ReportConfig", "PackError",
    "StorageBackend", "LocalStorage", "MemoryStorage", "S3Storage",
]

__version__ = "0.2.0"
