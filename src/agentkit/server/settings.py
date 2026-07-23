"""server.settings —— Server 配置快照。

启动时从 :mod:`agentkit.config` 读取所有 ``server_*`` 配置，封装为不可变
dataclass，供 FastAPI 依赖注入与中间件共享。一次读取、进程内共享，避免
请求路径上反复 ``get_default``。

设计原则：
    - 用 ``dataclass`` 而非 pydantic ``BaseSettings``，避免引入额外依赖
      与 pydantic 局部类解析陷阱（见 ``adapters/api_router.py`` 注释）
    - ``token`` 优先读 config，其次读环境变量 ``AGENTKIT_SERVER_TOKEN``
    - 模块顶层仅依赖 :mod:`agentkit.config` 与标准库；不 import fastapi

注意：本模块 **不用** ``from __future__ import annotations``
（Pydantic + FastAPI 局部类解析陷阱）。

公开 API：
    - ServerSettings: Server 配置快照
    - from_config:    便利构造函数（等价 ``ServerSettings.from_config()``）
"""

import os
from dataclasses import dataclass, field
from typing import List

from agentkit.config import get_default

__all__ = ["ServerSettings"]


def _read_env_token() -> str:
    """从环境变量 ``AGENTKIT_SERVER_TOKEN`` 读取 token。

    与 config ``server_token`` 的优先级：config 非空时优先；为空时回落 env。
    这样 CLI ``--token`` 覆盖（写入 config）优先级最高，env 作为兜底默认。
    """
    return os.environ.get("AGENTKIT_SERVER_TOKEN", "")


@dataclass(frozen=True)
class ServerSettings:
    """Server 配置快照（启动时从 config 读取一次，进程内共享）。

    使用 ``frozen=True`` 使实例不可变，便于在多处共享而不被误改。

    Attributes:
        host:                    绑定地址（默认 127.0.0.1）。
        port:                    绑定端口（默认 8000）。
        token:                   鉴权 bearer token；空字符串表示仅允许本地访问。
        cors_origins:            CORS 允许的 origin 列表；空列表表示关闭 CORS。
        event_queue_size:        EventBus per-subscriber 队列容量。
        event_log_max_events:    单 run 事件日志最大事件数。
        artifact_max_size:       单 artifact 最大字节。
        artifact_max_total:      单 run 产物总量最大字节。
        gc_interval_seconds:     GCSweeper 定时扫描间隔（秒）。
        gc_orphan_grace_seconds: GCSweeper 孤儿文件宽限期（秒）。
    """

    host: str
    port: int
    token: str
    cors_origins: List[str] = field(default_factory=list)
    event_queue_size: int = 1000
    event_log_max_events: int = 100000
    artifact_max_size: int = 100 * 1024 * 1024
    artifact_max_total: int = 1024 * 1024 * 1024
    gc_interval_seconds: float = 6 * 3600.0
    gc_orphan_grace_seconds: float = 24 * 3600.0

    @classmethod
    def from_config(cls) -> "ServerSettings":
        """从 ``config.get_default`` 读取所有 ``server_*`` 配置。

        ``token`` 优先读 config 的 ``server_token``；为空时回落到环境变量
        ``AGENTKIT_SERVER_TOKEN``（便于容器化部署通过 env 注入敏感凭据，
        避免写入代码或 config）。

        Returns:
            ServerSettings: 基于当前生效配置构造的快照。
        """
        token = get_default("server_token") or _read_env_token()
        return cls(
            host=get_default("server_host"),
            port=int(get_default("server_port")),
            token=token,
            cors_origins=list(get_default("server_cors_origins")),
            event_queue_size=int(get_default("server_event_queue_size")),
            event_log_max_events=int(get_default("server_event_log_max_events")),
            artifact_max_size=int(get_default("server_artifact_max_size")),
            artifact_max_total=int(get_default("server_artifact_max_total")),
            gc_interval_seconds=float(get_default("server_gc_interval_seconds")),
            gc_orphan_grace_seconds=float(
                get_default("server_gc_orphan_grace_seconds")
            ),
        )


def from_config() -> ServerSettings:
    """便利函数：返回 ``ServerSettings.from_config()``。

    等价于类方法调用，提供更简洁的导入入口：``from agentkit.server.settings
    import from_config``。
    """
    return ServerSettings.from_config()
