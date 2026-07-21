"""mcp.manager —— MCP 连接管理 + 自动发现注册。

本模块是 AgentKit 框架与外部 MCP Server 交互的统一入口。``MCPManager`` 在启动时
连接所有配置启用的 MCP Server,对其执行 ``list_tools`` / ``list_resources`` /
``list_prompts`` 自动发现,并把发现的 Tool / Resource 包装为框架统一的 ``Tool``
接口注册到 ``ToolRegistry``,使 MCP 能力自动成为框架的「一等公民」工具,可被
Workflow / Step / Function Call 调度直接使用。

典型生命周期::

    manager = MCPManager(configs=[...], tool_registry=registry)
    await manager.connect_all()      # 连接 + 发现 + 注册
    tool = manager.get_tool("fs.read_file")
    ...
    await manager.close_all()        # 关闭所有连接

设计原则:
    - 高度模块化:仅依赖 ``agentkit.mcp.client`` + ``agentkit.mcp.wrapper`` +
      ``agentkit.tools.base`` + 标准库,不依赖 core / steps / llm 等其他子模块,
      避免循环依赖。
    - 容错:单个 server 连接/发现失败不中断整体流程,失败信息记入
      ``MCPDiscoveryResult.error``,该 server 结果 ``connected=False``,继续处理下一个。
    - 可拓展:Prompt 发现预留扩展点(当前仅记录名,后续可映射为 Agent prompt 模板)。
    - 生命周期清晰:``connect_all`` → 使用 → ``close_all``。
    - 类型注解完整,中文 docstring 与注释。

公开 API:
    - MCPServerConfig:    单个 MCP Server 配置(从 ``mcp_servers`` 列表项解析)
    - MCPDiscoveryResult: 单个 server 的发现结果(供 health_check 报告与调试)
    - MCPManager:         MCP 连接管理 + 自动发现注册主类
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentkit.mcp.client import (
    MCPClient,
    MCPError,
    MCPPromptInfo,
    MCPResourceInfo,
    MCPToolInfo,
)
from agentkit.mcp.wrapper import MCPResourceWrapper, MCPToolWrapper
from agentkit.tools.base import (
    Tool,
    ToolRegistry,
    get_tool as _global_get_tool,
    list_tools as _global_list_tools,
    register as register_tool,
)

if TYPE_CHECKING:
    # 仅用于类型注解,运行时不导入 core.agent,避免 mcp -> core 的循环依赖。
    from agentkit.core.agent import AgentConfig

__all__ = [
    "MCPServerConfig",
    "MCPDiscoveryResult",
    "MCPManager",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCPServerConfig —— 单个 MCP Server 配置
# ---------------------------------------------------------------------------
@dataclass
class MCPServerConfig:
    """单个 MCP Server 配置(从 ``mcp_servers`` 列表项解析)。

    Attributes:
        name:            server 标识名(用作注册工具的命名空间前缀)。
        transport:       传输方式,``"stdio"`` 或 ``"sse"``,默认 ``"stdio"``。
        command:         stdio 模式下要启动的可执行文件(如 ``"npx"``)。
        args:            stdio 模式下传给可执行文件的参数列表。
        url:             sse 模式下 MCP server 的 HTTP URL。
        enabled:         是否启用(仅启用的 server 会被连接)。
        request_timeout: 单次请求超时秒数。
    """

    name: str
    transport: str = "stdio"  # stdio | sse
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    enabled: bool = True
    request_timeout: float = 30.0

    @classmethod
    def from_dict(cls, d: dict) -> MCPServerConfig:
        """从配置 dict 解析 ``MCPServerConfig``。

        对应 YAML 中 ``mcp_servers`` 列表的单个项,缺省字段按类默认值处理。

        Args:
            d: 配置 dict,可含字段:
                ``name`` / ``transport`` / ``command`` / ``args`` / ``url`` /
                ``enabled`` / ``request_timeout``。

        Returns:
            MCPServerConfig: 解析后的配置实例。
        """
        # args 缺省为空列表;若提供则拷贝一份,避免外部修改影响内部状态
        raw_args = d.get("args")
        args: list[str] = list(raw_args) if raw_args else []
        # request_timeout 容错:可能从 YAML 读到 int,统一转 float
        try:
            request_timeout = float(d.get("request_timeout", 30.0))
        except (TypeError, ValueError):
            request_timeout = 30.0
        return cls(
            name=d.get("name", ""),
            transport=d.get("transport", "stdio"),
            command=d.get("command"),
            args=args,
            url=d.get("url"),
            enabled=bool(d.get("enabled", True)),
            request_timeout=request_timeout,
        )


# ---------------------------------------------------------------------------
# MCPDiscoveryResult —— 单个 server 的发现结果
# ---------------------------------------------------------------------------
@dataclass
class MCPDiscoveryResult:
    """单个 server 的发现结果(供 health_check 报告与调试)。

    Attributes:
        name:      server 名。
        connected: 是否已建立可用连接。
        error:     失败原因(连接/发现失败时填写,成功为 ``None``)。
        tools:     发现的工具名列表(已注册的框架名,形如 ``{server}.{tool}``)。
        resources: 发现的资源名列表(框架名,形如 ``{server}.resource.{name}``)。
        prompts:   发现的 prompt 名列表(原始 prompt 名,暂未映射为 Tool)。
    """

    name: str
    connected: bool = False
    error: str | None = None
    tools: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# MCPManager —— MCP 连接管理 + 自动发现注册
# ---------------------------------------------------------------------------
class MCPManager:
    """MCP 连接管理 + 自动发现注册。

    启动时连接所有配置启用的 MCP Server,自动发现 Tool / Resource / Prompt,
    把 Tool / Resource 包装为框架 ``Tool`` 注册到 ``ToolRegistry``。Workflow
    持有本实例以管理其生命周期。

    Args:
        configs:       server 配置列表。``connect_all`` 前也可通过 ``add_server``
                       追加。
        tool_registry: 注册 Tool 的目标注册表。``None`` 时使用全局 ``ToolRegistry``
                       (即 ``agentkit.tools.base.register`` / ``get_tool``)。
        auto_register: 发现的 Tool 是否自动注册到 registry。``False`` 时仅发现
                       不注册(结果仍记录工具名,便于上层自行处理)。
    """

    def __init__(
        self,
        configs: list[MCPServerConfig] | None = None,
        *,
        tool_registry: ToolRegistry | None = None,
        auto_register: bool = True,
    ) -> None:
        # 拷贝一份,避免外部 list 修改影响内部状态
        self._configs: list[MCPServerConfig] = list(configs) if configs else []
        # None 表示使用全局 ToolRegistry(register / get_tool / list_tools)
        self._tool_registry: ToolRegistry | None = tool_registry
        self._auto_register: bool = auto_register
        # 已连接的 client:server_name -> MCPClient
        self._clients: dict[str, MCPClient] = {}

    # ------------------------------------------------------------------
    # 配置管理
    # ------------------------------------------------------------------
    def add_server(self, config: MCPServerConfig) -> None:
        """添加一个 server 配置(应在 ``connect_all`` 前调用)。

        Args:
            config: 要添加的 server 配置。
        """
        self._configs.append(config)

    @property
    def available_mcp_names(self) -> list[str]:
        """所有已配置且启用的 server 名列表。

        供 Skill loader 校验 ``requires.mcp`` 用:Skill 声明依赖的 MCP server
        必须在此列表中,否则视为依赖未满足。
        """
        return [c.name for c in self._configs if c.enabled]

    def get_client(self, name: str) -> MCPClient | None:
        """按 server 名取已连接的 client。

        Args:
            name: server 名。

        Returns:
            MCPClient | None: 已连接的客户端;未连接或不存在返回 ``None``。
        """
        return self._clients.get(name)

    # ------------------------------------------------------------------
    # 连接与发现
    # ------------------------------------------------------------------
    async def connect_all(self) -> list[MCPDiscoveryResult]:
        """连接所有启用的 server,自动发现 Tool/Resource/Prompt 并注册。

        单个 server 失败(连接失败或发现失败)不中断整体:捕获异常,记入该 server
        结果的 ``error`` 字段并置 ``connected=False``,继续处理下一个 server。
        连接成功的 client 存入 ``self._clients``。

        Returns:
            list[MCPDiscoveryResult]: 每个被尝试连接的 server 的发现结果
            (按配置顺序;未启用的 server 不包含在内)。
        """
        results: list[MCPDiscoveryResult] = []
        for config in self._configs:
            # 仅处理启用的 server
            if not config.enabled:
                continue
            result = MCPDiscoveryResult(name=config.name)
            try:
                # 构造客户端:stdio 需 command,sse 需 url(由 MCPClient 校验)
                client = MCPClient(
                    config.name,
                    config.transport,
                    command=config.command,
                    args=config.args if config.args else None,
                    url=config.url,
                    enabled=config.enabled,
                    request_timeout=config.request_timeout,
                )
                await client.connect()
                # 连接成功后存入 _clients,供后续 get_client / health_check 使用
                self._clients[config.name] = client
                # 发现并把结果交由 discover 填充(connected / tools / resources / prompts)
                result = await self.discover(client, config.name)
            except Exception as exc:
                # 连接失败或 discover 抛出的未预期异常:记录错误,connected=False
                # discover 内部已捕获各阶段异常,这里主要兜底连接失败
                logger.warning(
                    "MCP server[%s] 连接/发现失败: %r", config.name, exc
                )
                result = MCPDiscoveryResult(
                    name=config.name, connected=False, error=f"{exc!r}"
                )
            results.append(result)
        return results

    async def discover(
        self, client: MCPClient, server_name: str
    ) -> MCPDiscoveryResult:
        """对已连接的 client 执行发现并注册。

        流程:
            1. ``list_tools`` → 每个 ``MCPToolInfo`` 包装为 ``MCPToolWrapper``
               (name=``{server}.{tool}``,role=``action``),若 ``auto_register``
               则注册到 registry,名称记入 ``result.tools``。
            2. ``list_resources`` → ``MCPResourceWrapper``
               (name=``{server}.resource.{name}``,role=``source``),同样注册,
               名称记入 ``result.resources``。失败不致命,留空。
            3. ``list_prompts`` → 暂不映射为 Tool(spec 说映射为 Agent 模板,但
               Agent 模板机制未实现),仅记录 prompt 名到 ``result.prompts``。
               失败不致命,留空。

        tools 发现失败会记入 ``result.error``(但 ``connected`` 仍反映连接状态);
        resources / prompts 失败仅 debug 记录,不影响整体。

        Args:
            client:      已连接的 MCP 客户端。
            server_name: 所属 server 名(用作工具命名空间前缀)。

        Returns:
            MCPDiscoveryResult: 发现结果。
        """
        # connected 反映连接状态(discover 调用方应已连接)
        result = MCPDiscoveryResult(
            name=server_name, connected=client.is_connected
        )

        # 1) Tools 发现 —— 失败记 error
        try:
            tools = await client.list_tools()
            for info in tools:
                wrapper = MCPToolWrapper(client, server_name, info)
                if self._auto_register:
                    self._register_tool(wrapper)
                result.tools.append(wrapper.name)
        except (MCPError, Exception) as exc:
            # tools 发现失败视为该 server 部分不可用,记 error(但不改变 connected)
            logger.warning(
                "MCP server[%s] tools 发现失败: %r", server_name, exc
            )
            result.error = f"tools 发现失败: {exc!r}"

        # 2) Resources 发现 —— 失败不致命
        try:
            resources = await client.list_resources()
            for info in resources:
                wrapper = MCPResourceWrapper(client, server_name, info)
                if self._auto_register:
                    self._register_tool(wrapper)
                result.resources.append(wrapper.name)
        except (MCPError, Exception) as exc:
            # 资源发现失败不致命,留空;仅 debug 记录
            logger.debug(
                "MCP server[%s] resources 发现失败(已忽略): %r",
                server_name,
                exc,
            )

        # 3) Prompts 发现 —— 失败不致命
        # 当前仅记录 prompt 名,后续可扩展为 Agent prompt 模板映射:
        #   for info in prompts: build AgentPromptTemplate(server_name, info) ...
        try:
            prompts = await client.list_prompts()
            for info in prompts:
                result.prompts.append(info.name)
        except (MCPError, Exception) as exc:
            logger.debug(
                "MCP server[%s] prompts 发现失败(已忽略): %r",
                server_name,
                exc,
            )

        return result

    # ------------------------------------------------------------------
    # 注册表便捷访问
    # ------------------------------------------------------------------
    def _register_tool(self, tool: Tool) -> None:
        """注册 tool 到目标 registry(全局或自定义)。

        若工具名已存在则跳过(避免重连/重复发现时 ``ValueError``),仅 debug 记录。

        Args:
            tool: 待注册的 Tool 实例。
        """
        if self._tool_registry is not None:
            if self._tool_registry.has(tool.name):
                logger.debug("工具 %s 已在 registry 中,跳过注册", tool.name)
                return
            self._tool_registry.register(tool)
        else:
            # 全局 registry:用 list_tools 检查重名
            if tool.name in _global_list_tools():
                logger.debug("工具 %s 已在全局 registry 中,跳过注册", tool.name)
                return
            register_tool(tool)

    def get_tool(self, name: str) -> Tool | None:
        """从 registry 取已注册的 MCP Tool(便捷)。

        Args:
            name: 工具注册名。

        Returns:
            Tool | None: 工具实例;未注册返回 ``None``。
        """
        if self._tool_registry is not None:
            if self._tool_registry.has(name):
                return self._tool_registry.get(name)
            return None
        # 全局 registry:get_tool 不存在时抛 KeyError
        try:
            return _global_get_tool(name)
        except KeyError:
            return None

    def list_mcp_tools(self) -> list[str]:
        """列出所有 MCP 注册的 Tool 名(以 server 名为前缀的)。

        扫描目标 registry,筛选出以已配置 server 名为前缀(``{server}.``)且
        非资源(不含 ``.resource.`` 段,资源单独由 ``MCPResourceWrapper`` 注册)
        的工具名。这样既覆盖 action 工具,又排除资源工具。

        Returns:
            list[str]: MCP 注册的工具名列表。
        """
        if self._tool_registry is not None:
            all_names = self._tool_registry.list()
        else:
            all_names = _global_list_tools()
        # server 前缀元组(带点,避免 'fs' 误匹配 'filesystem' 等)
        server_prefixes = tuple(
            f"{c.name}." for c in self._configs if c.enabled
        )
        mcp_tools: list[str] = []
        for n in all_names:
            if not n.startswith(server_prefixes):
                continue
            # 排除资源工具({server}.resource.{name})
            if ".resource." in n:
                continue
            mcp_tools.append(n)
        return mcp_tools

    def inject_mcp_tools(self, agent: "AgentConfig") -> "AgentConfig":
        """把 agent.mcp 声明依赖的 server 对应工具名注入 agent.tools。

        连接完成后调用:遍历 ``agent.mcp`` 列出的 server,从 registry 筛选
        ``{server}.`` 前缀的工具名(排除资源),与 ``agent.tools`` 并集保序后
        经 ``dataclasses.replace`` 返回新实例(不可变理念)。

        补齐「注册表有、schema 无」缺口:MCP 工具注册到 registry 后,经此方法
        注入到 agent.tools,LLMStep._build_tools_schema 才会把它们带给模型。
        ``agent.mcp`` 为空或无匹配工具时返回原 agent(零开销)。

        Args:
            agent: 待注入的 AgentConfig(读取 ``mcp`` / ``tools`` 属性)。

        Returns:
            AgentConfig: tools 已合并 MCP 工具名的新实例;无变化时返回原实例。
        """
        mcp_servers = list(getattr(agent, "mcp", []) or [])
        if not mcp_servers:
            return agent
        # 取所有已发现的 MCP 工具名,再按 agent.mcp 列出的 server 过滤
        all_mcp_tools = self.list_mcp_tools()
        wanted_prefixes = tuple(f"{s}." for s in mcp_servers)
        injected = [n for n in all_mcp_tools if n.startswith(wanted_prefixes)]
        if not injected:
            return agent
        # 与 agent.tools 并集保序(agent 自带优先)
        existing = list(getattr(agent, "tools", []) or [])
        seen: set[str] = set(existing)
        new_tools = list(existing)
        for name in injected:
            if name not in seen:
                new_tools.append(name)
                seen.add(name)
        return dataclasses.replace(agent, tools=new_tools)

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    async def health_check(self) -> list[MCPDiscoveryResult]:
        """检测所有启用 server 的连通性(不重新发现)。

        对每个已建立连接的 client 调用 ``client.health_check()``(不抛异常,
        返回 bool);未建立连接的 server 标记 ``connected=False`` 并填写 error。
        结果中的 tools / resources / prompts 留空(仅反映连通性)。

        供 CLI ``agentkit mcp health-check`` 使用。

        Returns:
            list[MCPDiscoveryResult]: 每个启用 server 的连通性结果。
        """
        results: list[MCPDiscoveryResult] = []
        for config in self._configs:
            if not config.enabled:
                continue
            result = MCPDiscoveryResult(name=config.name)
            client = self._clients.get(config.name)
            if client is None:
                result.connected = False
                result.error = "未建立连接"
            else:
                try:
                    ok = await client.health_check()
                    result.connected = ok
                    if not ok:
                        result.error = "health_check 失败(连接不可用)"
                except Exception as exc:
                    # health_check 设计上不抛,但兜底捕获以防万一
                    result.connected = False
                    result.error = f"health_check 异常: {exc!r}"
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    async def close_all(self) -> None:
        """关闭所有连接。

        遍历 ``self._clients`` 调用 ``client.close()``,单个关闭异常不中断整体
        (仅 warning 记录),最后清空 ``_clients``。
        """
        for name, client in self._clients.items():
            try:
                await client.close()
            except Exception as exc:
                logger.warning(
                    "MCP server[%s] 关闭异常(已忽略): %r", name, exc
                )
        self._clients.clear()
