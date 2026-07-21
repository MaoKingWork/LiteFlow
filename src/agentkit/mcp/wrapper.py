"""mcp.wrapper —— MCP 能力到 AgentKit Tool 接口的适配器。

本模块把 MCP(Model Context Protocol)服务端暴露的 Tool / Resource 包装为
AgentKit 框架统一的 ``Tool`` 接口,使 MCP 提供的能力自动成为框架的「一等公民」
工具,可直接参与 Function Call 调度与 Step 编排,无需为每个 MCP 工具重复定义
pydantic 参数模型。

包装策略:
    - ``MCPToolWrapper``:  包装 MCP Tool,role=``action``。
        * 命名 ``{server_name}.{tool_name}``,避免多 server 间命名冲突。
        * 复用 MCP 提供的 ``inputSchema`` 作为 Function Call 的参数 schema,
          ``param_model`` 始终返回 ``None``(不引入 pydantic 模型)。
        * 调用时把 ``params`` 直接作为 MCP ``tools/call`` 的 arguments。
        * MCP 返回 ``is_error=True`` 时抛 ``RuntimeError``(交由上层 Step 的
          retry 机制处理);网络/RPC 异常直接向上抛。
    - ``MCPResourceWrapper``: 包装 MCP Resource,role=``source``。
        * 命名 ``{server_name}.resource.{resource_name}``。
        * 通过 ``read_resource`` 读取文本内容;调用方可传 ``{"uri": ...}``
          覆盖默认 URI。
        * schema 暴露一个可选的 ``uri`` 字符串参数。

设计原则:
    - 高度模块化:仅依赖 ``agentkit.tools.base`` 与 ``agentkit.mcp.client``,
      ``Context`` 通过 ``TYPE_CHECKING`` 引用(运行时 duck typing),不依赖
      其他 agentkit 子模块,避免循环依赖。
    - 可拓展:复用 ``Tool`` 接口,新增 MCP 能力类型(如 Prompt)可参照本模式
      新增 Wrapper 子类。
    - 类型注解完整,中文 docstring 与注释。

公开 API:
    - MCPToolWrapper:     MCP Tool -> AgentKit Tool
    - MCPResourceWrapper: MCP Resource -> AgentKit Tool(role=source)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from agentkit.mcp.client import (
    MCPClient,
    MCPResourceInfo,
    MCPToolInfo,
)
from agentkit.tools.base import Tool, _CallableSchemaDict

if TYPE_CHECKING:
    from agentkit.core.context import Context


__all__ = ["MCPToolWrapper", "MCPResourceWrapper"]


# ---------------------------------------------------------------------------
# MCPToolWrapper —— MCP Tool 包装为框架 Tool(role=action)
# ---------------------------------------------------------------------------
class MCPToolWrapper(Tool):
    """把 MCP Tool 包装为框架 ``Tool``。

    复用 MCP 提供的 ``inputSchema`` 作为参数 schema,无需重复定义
    ``param_model``。调用时把 ``params`` 直接透传给 MCP ``tools/call``。

    Attributes:
        _client:      底层 MCP 客户端(已连接)。
        _server_name: 所属 MCP server 名(用于命名空间隔离)。
        _tool_info:   MCP 工具元信息(含 name / description / input_schema)。
        name:         框架工具名,``{server_name}.{tool_name}``。
        description:  工具描述,取自 ``tool_info.description``。
        role:         固定 ``"action"``。
    """

    def __init__(
        self,
        client: MCPClient,
        server_name: str,
        tool_info: MCPToolInfo,
    ) -> None:
        """初始化 MCP Tool 包装器。

        Args:
            client:      已连接的 MCP 客户端实例。
            server_name: 所属 server 名(命名空间前缀)。
            tool_info:   MCP 工具元信息。
        """
        self._client: MCPClient = client
        self._server_name: str = server_name
        self._tool_info: MCPToolInfo = tool_info
        # 实例属性覆盖基类类属性,确保每个 wrapper 有独立命名
        self.name: str = f"{server_name}.{tool_info.name}"
        self.description: str = tool_info.description
        self.role: str = "action"

    async def call(self, params: dict, ctx: "Context") -> dict:
        """调用 MCP tool。

        ``params`` 即 MCP ``tools/call`` 的 arguments,直接透传。MCP 返回
        ``is_error=True`` 时抛 ``RuntimeError``(交由上层 Step 的 retry 机制
        处理);底层网络/RPC 异常直接向上抛(同样交由 retry 机制)。

        Args:
            params: 调用参数,作为 MCP tool 的 arguments。
            ctx:    会话上下文,**只读**(本实现未使用,仅为满足接口契约)。

        Returns:
            dict: 成功时形如
            ``{"text": <拼接文本>, "raw_content": <content 列表>, "is_error": False}``。

        Raises:
            RuntimeError: MCP 服务端将本次调用标记为错误(``is_error=True``)。
            Exception:    底层网络/RPC 异常(MCPError 等),向上抛由 retry 处理。
        """
        # 直接透传 params 作为 arguments;底层异常不捕获,交由上层 retry
        result = await self._client.call_tool(self._tool_info.name, params)
        # MCP 协议层的「业务错误」:服务端返回 is_error=True。此处抛 RuntimeError,
        # 让 LLMStep/ToolStep 的 retry 机制统一处理(与其他 Tool 失败语义一致)。
        if result.is_error:
            raise RuntimeError(
                f"MCP tool {self.name} 返回错误: {result.text}"
            )
        return {
            "text": result.text,
            "raw_content": result.content,
            "is_error": False,
        }

    @property
    def schema(self) -> dict:
        """复用 MCP ``inputSchema`` 生成 Function Call schema。

        若 ``input_schema`` 为空 dict,则降级为空 object 占位 schema,
        保证 schema 始终合法。

        Returns:
            dict: 形如
            ``{"name": ..., "description": ..., "parameters": <jsonschema>}``,
            返回 ``_CallableSchemaDict`` 以兼容 ``.schema`` 与 ``.schema()``
            两种访问形式(与 ``Tool`` 基类行为一致)。
        """
        # 空 dict 视为未提供,降级为合法的空 object schema
        params_schema: dict[str, Any] = self._tool_info.input_schema or {
            "type": "object",
            "properties": {},
        }
        return _CallableSchemaDict(
            name=self.name,
            description=self.description,
            parameters=params_schema,
        )

    @property
    def param_model(self) -> type[BaseModel] | None:
        """始终返回 ``None``。

        MCP 工具使用 ``input_schema``(JSON Schema)描述参数,不使用 pydantic
        model,因此此处返回 ``None`` 以禁用基类的 model_json_schema 路径,
        避免与 ``schema`` 属性的复用逻辑冲突。
        """
        return None


# ---------------------------------------------------------------------------
# MCPResourceWrapper —— MCP Resource 包装为框架 Tool(role=source)
# ---------------------------------------------------------------------------
class MCPResourceWrapper(Tool):
    """把 MCP Resource 包装为框架 ``Tool``(role=``source``)。

    通过 ``read_resource`` 读取资源文本内容。调用方可在 ``params`` 中传入
    ``{"uri": ...}`` 覆盖默认 URI,否则使用注册时的 ``resource_info.uri``。

    Attributes:
        _client:        底层 MCP 客户端(已连接)。
        _server_name:   所属 MCP server 名。
        _resource_info: MCP 资源元信息(含 uri / name / description / mime_type)。
        name:           框架工具名,``{server_name}.resource.{resource_name}``。
        description:    工具描述,取自 ``resource_info.description`` 或回退描述。
        role:           固定 ``"source"``。
    """

    def __init__(
        self,
        client: MCPClient,
        server_name: str,
        resource_info: MCPResourceInfo,
    ) -> None:
        """初始化 MCP Resource 包装器。

        Args:
            client:        已连接的 MCP 客户端实例。
            server_name:   所属 server 名(命名空间前缀)。
            resource_info: MCP 资源元信息。
        """
        self._client: MCPClient = client
        self._server_name: str = server_name
        self._resource_info: MCPResourceInfo = resource_info
        # 实例属性覆盖基类类属性
        self.name: str = f"{server_name}.resource.{resource_info.name}"
        # 描述缺失时回退为包含 URI 的默认描述,便于 LLM 理解用途
        self.description: str = (
            resource_info.description
            or f"MCP resource: {resource_info.uri}"
        )
        self.role: str = "source"

    async def call(self, params: dict, ctx: "Context") -> dict:
        """读取 MCP 资源。

        ``params`` 可含 ``{"uri": ...}`` 覆盖默认 URI,否则使用
        ``resource_info.uri``。底层网络/RPC 异常直接向上抛(交由上层 retry)。

        Args:
            params: 调用参数,可含 ``uri`` 覆盖默认资源 URI。
            ctx:    会话上下文,**只读**(本实现未使用,仅为满足接口契约)。

        Returns:
            dict: 形如
            ``{"text": <资源文本>, "uri": <实际读取的 URI>, "mime_type": <MIME>}``。

        Raises:
            Exception: 底层网络/RPC 异常,向上抛由 retry 机制处理。
        """
        # 允许调用方覆盖 URI;默认使用注册时的 URI
        uri: str = params.get("uri", self._resource_info.uri)
        text: str = await self._client.read_resource(uri)
        return {
            "text": text,
            "uri": uri,
            "mime_type": self._resource_info.mime_type,
        }

    @property
    def schema(self) -> dict:
        """生成 Function Call schema。

        暴露一个可选的 ``uri`` 字符串参数,允许 LLM 在调用时指定要读取的资源 URI;
        不提供时使用注册的默认 URI。

        Returns:
            dict: 形如
            ``{"name": ..., "description": ..., "parameters": <jsonschema>}``,
            返回 ``_CallableSchemaDict`` 以兼容 ``.schema`` 与 ``.schema()``
            两种访问形式。
        """
        params_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "资源 URI,可选,默认使用注册的 URI",
                }
            },
            "required": [],
        }
        return _CallableSchemaDict(
            name=self.name,
            description=self.description,
            parameters=params_schema,
        )

    @property
    def param_model(self) -> type[BaseModel] | None:
        """始终返回 ``None``。

        MCP 资源使用 schema 属性描述参数,不使用 pydantic model。
        """
        return None
