"""tools —— 内置工具子包。

导入本包即自动注册所有内置 Tool 到全局 ``ToolRegistry``:

    - DBQueryTool  (name="db.query",  role="source")
    - HTTPTool     (name="http.request", role="source")
    - WeComSink    (name="sink.wecom",   role="sink")
    - WebhookSink  (name="sink.webhook", role="sink")
"""

# 导入各工具模块,触发 @tool 装饰器注册到全局 ToolRegistry
from agentkit.tools.base import Tool, register, get_tool, list_tools
from agentkit.tools.db import DBQueryTool
from agentkit.tools.http import HTTPTool
from agentkit.tools.sinks import WeComSink, WebhookSink

__all__ = [
    "Tool",
    "register",
    "get_tool",
    "list_tools",
    "DBQueryTool",
    "HTTPTool",
    "WeComSink",
    "WebhookSink",
]
