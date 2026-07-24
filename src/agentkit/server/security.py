"""server.security —— Bearer token 鉴权 + CORS + 绑定校验。

本模块封装 Server 的安全中间件链，提供：
    - Bearer token 校验（``Authorization: Bearer <token>``）
    - CORS（Cross-Origin Resource Sharing）
    - 绑定校验（``server_host`` 非 127.0.0.1 且无 token 时拒绝远程请求）

设计原则：
    - **懒加载**：``import fastapi`` / ``import starlette`` 在函数内部，
      模块顶层不依赖 fastapi。未安装 ``agentkit[server]`` extra 时，
      ``import agentkit.server.security`` 仍可成功，仅在调用工厂函数时报错。
    - **本地放行**：``settings.token`` 为空时仅允许 ``127.0.0.1`` / ``::1``
      / ``localhost``，其他地址返回 401。
    - **token 优先**：``settings.token`` 非空时，所有请求必须携带匹配的
      Bearer token；本地请求也不例外（避免本地恶意进程绕过）。
    - **CORS 独立**：CORS 头由 ``CORSMiddleware`` 添加，与鉴权无关；
      ``settings.cors_origins`` 为空时关闭 CORS。

注意：本模块 **不用** ``from __future__ import annotations``
（Pydantic + FastAPI 局部类解析陷阱，见 ``adapters/api_router.py`` 注释）。

公开 API：
    - verify_token:              返回 FastAPI 依赖函数，校验 Bearer token
    - create_security_middleware: 创建安全中间件链
    - is_local_request:          判断请求是否来自本地（供中间件复用）
    - extract_bearer_token:      从 Authorization 头提取 token
"""

import hmac
from typing import Any, Callable, List

from agentkit.server.settings import ServerSettings

__all__ = [
    "verify_token",
    "create_security_middleware",
    "is_local_request",
    "extract_bearer_token",
]


# 本地回环地址集合（IPv4 + IPv6 + 主机名）
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_local_request(client_host: str) -> bool:
    """判断请求是否来自本地回环地址。

    Args:
        client_host: 客户端主机地址（来自 ``request.client.host``）。

    Returns:
        bool: ``True`` 表示请求来自 127.0.0.1 / ::1 / localhost。
    """
    return client_host in _LOCAL_HOSTS


def extract_bearer_token(authorization: str) -> str:
    """从 ``Authorization`` 头提取 Bearer token。

    支持格式：``Bearer <token>``（大小写不敏感前缀）。无前缀或格式错误
    时返回空字符串，由调用方决定是否拒绝。

    Args:
        authorization: ``Authorization`` 头的原始值。

    Returns:
        str: 提取出的 token；无或格式错误时为 ``""``。
    """
    if not authorization:
        return ""
    # 大小写不敏感匹配 "Bearer " 前缀
    parts = authorization.split(" ", 1)
    if len(parts) != 2:
        return ""
    if parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def _token_matches(provided: str, expected: str) -> bool:
    """用恒定时间比较避免时序攻击。

    Args:
        provided: 请求提供的 token。
        expected: 配置的预期 token。

    Returns:
        bool: 是否匹配。
    """
    # hmac.compare_digest 对字符串要求类型一致且非 None；
    # 空串与空串比较恒为 True（但调用方已保证 expected 非空）。
    return hmac.compare_digest(provided, expected)


def verify_token(settings: ServerSettings) -> Callable:
    """返回 FastAPI 依赖函数，校验 ``Authorization: Bearer <token>``。

    行为分支：
        - ``settings.token`` 非空：所有请求必须携带匹配的 Bearer token，
          否则 401。本地请求也不例外。
        - ``settings.token`` 为空：
            - 请求来自 127.0.0.1 / ::1 / localhost → 放行
            - 请求来自其他地址 → 401（绑定校验：无 token 时仅允许本地）

    Args:
        settings: Server 配置快照。

    Returns:
        Callable: FastAPI 依赖函数，签名 ``async def dep(request) -> None``。
        校验失败时抛 ``HTTPException(401)``。
    """
    # 懒加载 fastapi
    from fastapi import HTTPException, Request

    expected_token = settings.token

    async def _verify(request: "Request") -> None:
        # 提取客户端地址
        client = request.client
        client_host = client.host if client else ""
        local = is_local_request(client_host)

        if expected_token:
            # token 模式：所有请求必须携带匹配 token
            auth_header = request.headers.get("authorization", "")
            provided = extract_bearer_token(auth_header)
            if not provided or not _token_matches(provided, expected_token):
                raise HTTPException(
                    status_code=401,
                    detail="无效或缺失的 Bearer token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        else:
            # 无 token 模式：仅允许本地请求
            if not local:
                raise HTTPException(
                    status_code=401,
                    detail="Server 未配置 token，仅允许本地访问；"
                    "请配置 server_token 或通过 --token 启动",
                )

    return _verify


def create_security_middleware(settings: ServerSettings) -> List[Any]:
    """创建安全中间件链。

    返回一个 list，元素为可直接传给 ``app.add_middleware()`` 的元组
    ``(MiddlewareClass, kwargs)`` 或已构造的中间件实例。当前实现：

        - CORS（``settings.cors_origins`` 非空时启用）

    Bearer token 鉴权不在此处注册为全局中间件，而是通过
    :func:`verify_token` 作为 FastAPI 依赖注入到需要保护的路由。
    这样可以根据路由粒度灵活控制（如 ``/health`` 不需要鉴权）。

    Args:
        settings: Server 配置快照。

    Returns:
        list: 中间件链。空列表表示无中间件。

    Raises:
        ImportError: 未安装 fastapi / starlette 时。
    """
    middlewares: List[Any] = []

    # CORS 中间件（仅在配置了 origin 时启用）
    if settings.cors_origins:
        try:
            from starlette.middleware.cors import CORSMiddleware
        except ImportError as e:
            raise ImportError(
                "CORS 需要 starlette: pip install agentkit[server]"
            ) from e
        middlewares.append(
            (
                CORSMiddleware,
                {
                    "allow_origins": list(settings.cors_origins),
                    "allow_credentials": True,
                    "allow_methods": ["*"],
                    "allow_headers": ["*"],
                },
            )
        )

    return middlewares
