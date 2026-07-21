"""mcp.client —— MCP(Model Context Protocol)客户端实现。

本模块实现 AgentKit 作为 MCP Client 与单个外部 MCP Server 通信的能力。
MCP 协议基于 JSON-RPC 2.0,客户端可发现并调用服务端暴露的 Tool / Resource /
Prompt。本模块只依赖标准库(asyncio / json)+ httpx,不依赖其他 agentkit 子模块,
保证可独立 import 与复用。

支持两种传输方式:
    - ``stdio``:启动子进程,通过其 stdin/stdout 以换行分隔的 JSON-RPC 通信
    - ``sse``:基于 HTTP,POST JSON-RPC 请求并解析 JSON 响应(简化实现)

设计原则:
    - 高度模块化:仅依赖标准库 + httpx
    - 可拓展:传输层可在子类或新增 transport 分支扩展(如 websocket)
    - 优雅:``health_check`` 不抛异常,连接前/关闭后方法调用给出明确错误
    - 类型注解完整,中文 docstring 与注释

公开 API:
    - MCPError:                MCP 调用异常(包装 JSON-RPC error)
    - MCPToolInfo:             工具元信息
    - MCPResourceInfo:         资源元信息
    - MCPPromptInfo:           Prompt 模板元信息
    - MCPCallResult:           工具调用结果(含便捷 ``text`` 属性)
    - MCPClient:               MCP 客户端主类
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = [
    "MCPError",
    "MCPToolInfo",
    "MCPResourceInfo",
    "MCPPromptInfo",
    "MCPCallResult",
    "MCPClient",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class MCPError(Exception):
    """MCP 调用异常。

    当 JSON-RPC 响应中包含 ``error`` 对象(含 code/message/data)时抛出,
    消息格式为 ``[<code>] <message>``。
    """


# ---------------------------------------------------------------------------
# 数据类:服务端能力元信息
# ---------------------------------------------------------------------------
@dataclass
class MCPToolInfo:
    """MCP 工具元信息。

    Attributes:
        name:         工具名(调用时使用)
        description:  工具描述
        input_schema: 工具入参 JSON Schema(对应 MCP 协议的 inputSchema)
    """

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)


@dataclass
class MCPResourceInfo:
    """MCP 资源元信息。

    Attributes:
        uri:        资源唯一标识(读取时使用)
        name:       资源名
        description:资源描述
        mime_type:  资源 MIME 类型
    """

    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""


@dataclass
class MCPPromptInfo:
    """MCP Prompt 模板元信息。

    Attributes:
        name:        Prompt 名(获取时使用)
        description: Prompt 描述
        arguments:   Prompt 参数声明(list[dict])
    """

    name: str
    description: str = ""
    arguments: list[dict] = field(default_factory=list)


@dataclass
class MCPCallResult:
    """MCP 工具调用结果。

    Attributes:
        content:  内容块列表,形如 ``[{"type":"text","text":"..."}]``
        is_error: 服务端是否将本次调用标记为错误
    """

    content: list[dict] = field(default_factory=list)
    is_error: bool = False

    @property
    def text(self) -> str:
        """便捷:拼接所有 ``type=="text"`` 的 content 文本。"""
        return "".join(
            c.get("text", "") for c in self.content if c.get("type") == "text"
        )


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------
class MCPClient:
    """MCP 客户端:连接单个 MCP Server,封装 JSON-RPC 调用。

    支持两种传输(transport):
        - ``stdio``:通过 ``command`` + ``args`` 启动子进程,经 stdin/stdout 通信
        - ``sse``:经 ``url`` 以 HTTP POST 发送 JSON-RPC(简化实现)

    Args:
        name:            客户端标识名(便于日志/管理器区分多个连接)
        transport:       传输方式,``"stdio"`` 或 ``"sse"``
        command:         stdio 模式下要启动的可执行文件(如 ``"npx"``)
        args:            stdio 模式下传给可执行文件的参数列表
        url:             sse 模式下 MCP server 的 HTTP URL
        enabled:         是否启用(供上层管理器决定是否连接)
        request_timeout: 单次请求超时秒数
    """

    # MCP 协议版本( initialize 握手时声明)
    _PROTOCOL_VERSION = "2024-11-05"
    # 客户端标识
    _CLIENT_NAME = "agentkit"
    _CLIENT_VERSION = "0.1.0"

    def __init__(
        self,
        name: str,
        transport: str,
        *,
        command: str | None = None,
        args: list[str] | None = None,
        url: str | None = None,
        enabled: bool = True,
        request_timeout: float = 30.0,
    ) -> None:
        if transport not in ("stdio", "sse"):
            raise ValueError(f"不支持的 transport: {transport!r},仅支持 'stdio' / 'sse'")
        # stdio 模式必须提供 command;sse 模式必须提供 url
        if transport == "stdio" and not command:
            raise ValueError("stdio 传输需要提供 command 参数")
        if transport == "sse" and not url:
            raise ValueError("sse 传输需要提供 url 参数")

        self.name: str = name
        self.transport: str = transport
        self.command: str | None = command
        self.args: list[str] = list(args) if args else []
        self.url: str | None = url
        self.enabled: bool = enabled
        self.request_timeout: float = request_timeout

        # 连接状态与底层传输句柄
        self._connected: bool = False
        self._process: asyncio.subprocess.Process | None = None  # stdio
        self._http_client: httpx.AsyncClient | None = None  # sse
        # stderr 读取任务(stdio 模式下持续消费子进程 stderr,避免管道阻塞)
        self._stderr_task: asyncio.Task[None] | None = None

        # JSON-RPC 请求 id 自增计数器
        self._next_id: int = 0

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        """是否已建立连接(且尚未 close)。"""
        return self._connected

    def _ensure_connected(self) -> None:
        """断言已连接,否则抛 RuntimeError。供各 RPC 方法调用前检查。"""
        if not self._connected:
            raise RuntimeError("MCP client 未连接")

    # ------------------------------------------------------------------
    # 连接与握手
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """建立连接并执行 initialize 握手。

        - stdio:启动子进程,然后发送 initialize 请求 + initialized 通知
        - sse:  创建 httpx.AsyncClient,发送 initialize 请求 + initialized 通知

        若已连接则直接返回(no-op)。
        """
        if self._connected:
            return

        if self.transport == "stdio":
            await self._connect_stdio()
        else:  # sse
            await self._connect_sse()

        # 执行 initialize 握手
        try:
            await self._request(
                "initialize",
                {
                    "protocolVersion": self._PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": self._CLIENT_NAME,
                        "version": self._CLIENT_VERSION,
                    },
                },
            )
            # 握手成功后,发送 notifications/initialized 通知(无 id,无响应)
            await self._notify("notifications/initialized", {})
        except Exception:
            # 握手失败需清理已建立的底层连接,避免句柄泄漏
            await self._cleanup_transport()
            raise

        self._connected = True

    async def _connect_stdio(self) -> None:
        """stdio 传输:启动子进程。"""
        assert self.command is not None  # 已在 __init__ 校验,这里仅为类型收窄
        self._process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # 持续读取并丢弃 stderr,避免子进程 stderr 缓冲区写满导致阻塞
        if self._process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _connect_sse(self) -> None:
        """sse 传输:创建 httpx 异步客户端。"""
        self._http_client = httpx.AsyncClient(timeout=self.request_timeout)

    async def _drain_stderr(self) -> None:
        """持续读取子进程 stderr 并以 debug 级别记录,避免管道阻塞。

        出错或流结束时安静退出。
        """
        if self._process is None or self._process.stderr is None:
            return
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    # EOF:子进程已关闭 stderr
                    break
                try:
                    text = line.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    text = repr(line)
                if text:
                    logger.debug("MCP server[%s] stderr: %s", self.name, text)
        except Exception as exc:
            # 读取异常不应影响主流程,仅记录
            logger.debug("MCP server[%s] stderr 读取结束: %r", self.name, exc)

    # ------------------------------------------------------------------
    # JSON-RPC 底层收发
    # ------------------------------------------------------------------
    async def _request(self, method: str, params: Any = None) -> Any:
        """发送 JSON-RPC 请求并等待对应 id 的响应。

        构造 ``{"jsonrpc":"2.0","id":<id>,"method":<method>,"params":<params>}``,
        根据 transport 走 stdio 或 sse 发送,然后等待并解析响应:
            - 含 ``result``:返回 result
            - 含 ``error``:抛 ``MCPError(f"[{code}] {message}")``

        Args:
            method: JSON-RPC 方法名(如 ``"tools/list"``)
            params: 方法参数(dict 或 None)

        Returns:
            响应中的 ``result`` 字段值。

        Raises:
            MCPError: 服务端返回 JSON-RPC error,或响应缺失 result/error。
        """
        self._next_id += 1
        msg_id = self._next_id
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        if self.transport == "stdio":
            response = await self._send_stdio(message)
        else:  # sse
            response = await self._send_sse(message)

        # 响应校验与解析
        if not isinstance(response, dict):
            raise MCPError(f"非法的 JSON-RPC 响应(非对象): {response!r}")
        if "error" in response and response["error"] is not None:
            err = response["error"] or {}
            code = err.get("code", -1)
            message_txt = err.get("message", "")
            raise MCPError(f"[{code}] {message_txt}")
        if "result" not in response:
            # 既无 result 也无 error,视为协议错误
            raise MCPError(f"JSON-RPC 响应缺少 result/error: {response!r}")
        return response["result"]

    async def _notify(self, method: str, params: Any = None) -> None:
        """发送 JSON-RPC 通知(无 id,无响应)。

        用于 ``notifications/initialized`` 等通知类消息。
        """
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            message["params"] = params

        if self.transport == "stdio":
            # 通知无响应,写入后即返回
            await self._write_stdio(message)
        else:  # sse:通知同样以 POST 发送,但不解析响应
            assert self._http_client is not None
            assert self.url is not None
            try:
                await self._http_client.post(
                    self.url, json=message, timeout=self.request_timeout
                )
            except Exception as exc:
                # 通知失败仅记录,不影响主流程(通知本来就是 best-effort)
                logger.debug("MCP 通知 %s 发送失败: %r", method, exc)

    # ------------------------------------------------------------------
    # stdio 传输底层
    # ------------------------------------------------------------------
    # 说明:MCP stdio 协议实际使用换行分隔的 JSON-RPC(Newline-Delimited JSON-RPC)。
    # 这里采用每条消息 ``json.dumps(msg) + "\n"`` 写入 stdin,从 stdout 按行读取响应,
    # 简化实现并兼容大多数 stdio MCP server。
    async def _write_stdio(self, message: dict[str, Any]) -> None:
        """向子进程 stdin 写入一条 JSON-RPC 消息(以换行结尾)。"""
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("stdio 子进程未启动,无法写入")
        line = (json.dumps(message) + "\n").encode("utf-8")
        self._process.stdin.write(line)
        await self._process.stdin.drain()

    async def _send_stdio(self, message: dict[str, Any]) -> dict[str, Any]:
        """stdio 发送请求并读取一行响应。"""
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("stdio 子进程未启动,无法读写")
        await self._write_stdio(message)
        # 按行读取,跳过空行与非 JSON-RPC 行(某些 server 会输出日志到 stdout)
        while True:
            line_bytes = await self._process.stdout.readline()
            if not line_bytes:
                # EOF:子进程已关闭 stdout
                raise MCPError("MCP server(stdio)stdout 已关闭,无法读取响应")
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                # 非 JSON 行(可能是 server 日志),debug 记录后跳过
                logger.debug("MCP server[%s] stdout 非 JSON 行: %s", self.name, line)
                continue
            if isinstance(parsed, dict) and ("id" in parsed or "result" in parsed or "error" in parsed):
                return parsed
            # 既非响应也非通知的 JSON,跳过
            logger.debug("MCP server[%s] stdout 非 JSON-RPC 响应: %s", self.name, parsed)

    # ------------------------------------------------------------------
    # sse 传输底层
    # ------------------------------------------------------------------
    # 说明:完整的 MCP over SSE 协议中,客户端 POST 请求到 endpoint,服务端通过 SSE 流
    # 推送响应。此处采用简化实现:POST JSON-RPC 请求,响应体直接返回 JSON-RPC 结果
    # (很多 MCP SSE server 同时支持这种同步式 POST+JSON 响应)。完整 SSE 流式可后续扩展。
    async def _send_sse(self, message: dict[str, Any]) -> dict[str, Any]:
        """sse 发送请求并解析 JSON 响应。"""
        if self._http_client is None:
            raise RuntimeError("httpx 客户端未初始化")
        assert self.url is not None
        resp = await self._http_client.post(
            self.url, json=message, timeout=self.request_timeout
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception as exc:
            raise MCPError(f"MCP server(sse)响应非 JSON: {exc!r}") from exc
        if not isinstance(data, dict):
            raise MCPError(f"MCP server(sse)响应非对象: {data!r}")
        return data

    # ------------------------------------------------------------------
    # 协议方法:tools
    # ------------------------------------------------------------------
    async def list_tools(self) -> list[MCPToolInfo]:
        """``tools/list`` —— 列出服务端暴露的所有工具。"""
        self._ensure_connected()
        result = await self._request("tools/list", {})
        tools = (result or {}).get("tools", []) if isinstance(result, dict) else []
        return [
            MCPToolInfo(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}) or {},
            )
            for t in tools
            if isinstance(t, dict)
        ]

    async def call_tool(self, name: str, arguments: dict) -> MCPCallResult:
        """``tools/call`` —— 调用指定工具。

        Args:
            name:     工具名
            arguments:工具入参

        Returns:
            MCPCallResult:包含 content 与 is_error
        """
        self._ensure_connected()
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            return MCPCallResult()
        return MCPCallResult(
            content=list(result.get("content", []) or []),
            is_error=bool(result.get("isError", False)),
        )

    # ------------------------------------------------------------------
    # 协议方法:resources
    # ------------------------------------------------------------------
    async def list_resources(self) -> list[MCPResourceInfo]:
        """``resources/list`` —— 列出服务端暴露的所有资源。"""
        self._ensure_connected()
        result = await self._request("resources/list", {})
        resources = (result or {}).get("resources", []) if isinstance(result, dict) else []
        return [
            MCPResourceInfo(
                uri=r.get("uri", ""),
                name=r.get("name", ""),
                description=r.get("description", ""),
                mime_type=r.get("mimeType", ""),
            )
            for r in resources
            if isinstance(r, dict)
        ]

    async def read_resource(self, uri: str) -> str:
        """``resources/read`` —— 读取指定资源,返回拼接后的文本内容。

        Args:
            uri: 资源 URI

        Returns:
            str: 资源文本内容(多个 content 块的 text 拼接)
        """
        self._ensure_connected()
        result = await self._request("resources/read", {"uri": uri})
        if not isinstance(result, dict):
            return ""
        contents = result.get("contents", []) or []
        # 拼接所有 text 字段;blob 类型此处忽略(可后续扩展为 bytes 解码)
        return "".join(
            c.get("text", "") for c in contents if isinstance(c, dict) and "text" in c
        )

    # ------------------------------------------------------------------
    # 协议方法:prompts
    # ------------------------------------------------------------------
    async def list_prompts(self) -> list[MCPPromptInfo]:
        """``prompts/list`` —— 列出服务端暴露的所有 Prompt 模板。"""
        self._ensure_connected()
        result = await self._request("prompts/list", {})
        prompts = (result or {}).get("prompts", []) if isinstance(result, dict) else []
        return [
            MCPPromptInfo(
                name=p.get("name", ""),
                description=p.get("description", ""),
                arguments=list(p.get("arguments", []) or []),
            )
            for p in prompts
            if isinstance(p, dict)
        ]

    async def get_prompt(
        self, name: str, arguments: dict | None = None
    ) -> list[dict]:
        """``prompts/get`` —— 获取指定 Prompt 渲染后的消息列表。

        Args:
            name:      Prompt 名
            arguments: Prompt 参数(可空)

        Returns:
            list[dict]: 消息列表,形如 ``[{"role":..., "content":{"type":..., "text":...}}]``
        """
        self._ensure_connected()
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments
        result = await self._request("prompts/get", params)
        if not isinstance(result, dict):
            return []
        return list(result.get("messages", []) or [])

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    async def health_check(self) -> bool:
        """检测连通性,返回 True/False(不抛异常)。

        - 未连接直接返回 False
        - 已连接则尝试 ``list_tools()``,成功返回 True,异常返回 False
        """
        if not self._connected:
            return False
        try:
            await self.list_tools()
            return True
        except Exception as exc:
            logger.debug("MCP client[%s] health_check 失败: %r", self.name, exc)
            return False

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """关闭连接。

        - stdio:终止子进程(``terminate``)并 await 退出
        - sse:  关闭 httpx 客户端

        多次调用安全(no-op)。
        """
        if not self._connected and self._process is None and self._http_client is None:
            return
        await self._cleanup_transport()
        self._connected = False

    async def _cleanup_transport(self) -> None:
        """清理底层传输句柄(stdio 子进程 / sse httpx / stderr 任务)。"""
        # 取消 stderr 读取任务
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
        self._stderr_task = None

        # stdio:终止子进程
        if self._process is not None:
            try:
                if self._process.returncode is None:
                    self._process.terminate()
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        # terminate 未生效,强制 kill
                        self._process.kill()
                        try:
                            await asyncio.wait_for(self._process.wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            pass
            except ProcessLookupError:
                # 进程已退出
                pass
            except Exception as exc:
                logger.debug("MCP client[%s] 关闭子进程异常: %r", self.name, exc)
            self._process = None

        # sse:关闭 httpx 客户端
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception as exc:
                logger.debug("MCP client[%s] 关闭 httpx 异常: %r", self.name, exc)
            self._http_client = None
