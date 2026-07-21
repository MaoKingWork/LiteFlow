"""Adapter layer — optional framework integrations.

Each adapter module lazily imports its framework dependency, so importing
this package does not require installing optional extras. Users import the
desired adapter directly, e.g.::

    from report_engine_sdk.adapters.mcp_server import create_mcp_server
"""

__all__: list[str] = []
