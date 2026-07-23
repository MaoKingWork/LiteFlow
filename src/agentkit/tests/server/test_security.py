"""server.security —— 鉴权 + CORS + 绑定校验测试（C3）。

出口标准（对齐 P1 §C3）：
    - is_local_request: 127.0.0.1 / ::1 / localhost → True,其他 → False
    - extract_bearer_token: 正确提取 Bearer token
    - verify_token(token 空 + 本地) → 放行
    - verify_token(token 空 + 远程) → 401
    - verify_token(token 匹配) → 放行
    - verify_token(token 不匹配) → 401
    - verify_token(token 缺失) → 401
    - CORS 启用 / 关闭

设计：
    - 纯 Python 辅助函数(is_local_request / extract_bearer_token)无需 fastapi
    - verify_token / create_security_middleware 测试用 pytest.importorskip("fastapi")
      守卫,未安装 fastapi 时自动跳过
"""
from __future__ import annotations

import pytest

from agentkit.server.security import (
    extract_bearer_token,
    is_local_request,
)
from agentkit.server.settings import ServerSettings


# ---------------------------------------------------------------------------
# 纯 Python 辅助函数（无需 fastapi）
# ---------------------------------------------------------------------------
def test_is_local_request_localhost():
    """127.0.0.1 / ::1 / localhost → True。"""
    assert is_local_request("127.0.0.1") is True
    assert is_local_request("::1") is True
    assert is_local_request("localhost") is True


def test_is_local_request_remote():
    """远程地址 → False。"""
    assert is_local_request("10.0.0.1") is False
    assert is_local_request("192.168.1.1") is False
    assert is_local_request("8.8.8.8") is False
    assert is_local_request("") is False


def test_extract_bearer_token_valid():
    """标准 Bearer token 格式正确提取。"""
    assert extract_bearer_token("Bearer abc123") == "abc123"
    assert extract_bearer_token("bearer xyz") == "xyz"  # 大小写不敏感
    assert extract_bearer_token("BEARER TOKEN") == "TOKEN"


def test_extract_bearer_token_with_spaces():
    """token 含空格时保留(只在前后 strip)。"""
    # split(" ", 1) 后取第二部分,strip 前后空白
    assert extract_bearer_token("Bearer  abc with space  ") == "abc with space"


def test_extract_bearer_token_missing_prefix():
    """无 Bearer 前缀 → 空字符串。"""
    assert extract_bearer_token("abc123") == ""
    assert extract_bearer_token("Basic abc123") == ""


def test_extract_bearer_token_empty():
    """空或 None → 空字符串。"""
    assert extract_bearer_token("") == ""
    assert extract_bearer_token(None) == ""  # type: ignore[arg-type]


def test_extract_bearer_token_no_token_part():
    """只有 'Bearer' 无 token → 空字符串。"""
    assert extract_bearer_token("Bearer") == ""
    assert extract_bearer_token("Bearer ") == ""


# ---------------------------------------------------------------------------
# verify_token / create_security_middleware（需 fastapi）
# ---------------------------------------------------------------------------
@pytest.fixture
def fastapi_dep():
    """懒加载 fastapi,未安装时跳过整个测试。"""
    pytest.importorskip("fastapi")
    pytest.importorskip("starlette")


@pytest.fixture
def local_settings():
    """无 token,仅本地访问的 settings。"""
    return ServerSettings(
        host="127.0.0.1", port=8000, token="", cors_origins=[]
    )


@pytest.fixture
def token_settings():
    """配置了 token 的 settings。"""
    return ServerSettings(
        host="127.0.0.1", port=8000, token="secret_token", cors_origins=[]
    )


class _MockRequest:
    """模拟 starlette.Request,只暴露 verify_token 需要的属性。"""

    def __init__(self, headers: dict, client_host: str = "127.0.0.1"):
        self._headers = headers
        self.client = type("Client", (), {"host": client_host})() if client_host else None

    @property
    def headers(self):
        return _MockHeaders(self._headers)


class _MockHeaders:
    """模拟 starlette.Headers,支持 get(key, default)。"""

    def __init__(self, headers: dict):
        # starlette.Headers 内部小写化,这里模拟
        self._headers = {k.lower(): v for k, v in headers.items()}

    def get(self, key: str, default: str = "") -> str:
        return self._headers.get(key.lower(), default)


# ---------------------------------------------------------------------------
# verify_token: 无 token 模式
# ---------------------------------------------------------------------------
async def test_no_token_localhost_ok(fastapi_dep, local_settings):
    """token 空 + 请求来自 127.0.0.1 → 放行(不抛异常)。"""
    from agentkit.server.security import verify_token

    dep = verify_token(local_settings)
    request = _MockRequest({}, client_host="127.0.0.1")
    # 不抛 HTTPException 即视为通过
    await dep(request)


async def test_no_token_remote_401(fastapi_dep, local_settings):
    """token 空 + 请求来自 10.0.0.1 → 401。"""
    from fastapi import HTTPException

    from agentkit.server.security import verify_token

    dep = verify_token(local_settings)
    request = _MockRequest({}, client_host="10.0.0.1")
    with pytest.raises(HTTPException) as exc_info:
        await dep(request)
    assert exc_info.value.status_code == 401


async def test_no_token_localhost_ipv6_ok(fastapi_dep, local_settings):
    """token 空 + ::1 → 放行。"""
    from agentkit.server.security import verify_token

    dep = verify_token(local_settings)
    request = _MockRequest({}, client_host="::1")
    await dep(request)


# ---------------------------------------------------------------------------
# verify_token: token 模式
# ---------------------------------------------------------------------------
async def test_token_match_ok(fastapi_dep, token_settings):
    """token 匹配 → 放行。"""
    from agentkit.server.security import verify_token

    dep = verify_token(token_settings)
    request = _MockRequest({"Authorization": "Bearer secret_token"})
    await dep(request)


async def test_token_mismatch_401(fastapi_dep, token_settings):
    """token 不匹配 → 401。"""
    from fastapi import HTTPException

    from agentkit.server.security import verify_token

    dep = verify_token(token_settings)
    request = _MockRequest({"Authorization": "Bearer wrong_token"})
    with pytest.raises(HTTPException) as exc_info:
        await dep(request)
    assert exc_info.value.status_code == 401


async def test_token_missing_401(fastapi_dep, token_settings):
    """token 配置但请求无 Authorization 头 → 401。"""
    from fastapi import HTTPException

    from agentkit.server.security import verify_token

    dep = verify_token(token_settings)
    request = _MockRequest({})
    with pytest.raises(HTTPException) as exc_info:
        await dep(request)
    assert exc_info.value.status_code == 401


async def test_token_malformed_header_401(fastapi_dep, token_settings):
    """Authorization 头格式错误(无 Bearer 前缀) → 401。"""
    from fastapi import HTTPException

    from agentkit.server.security import verify_token

    dep = verify_token(token_settings)
    request = _MockRequest({"Authorization": "secret_token"})  # 无 Bearer 前缀
    with pytest.raises(HTTPException) as exc_info:
        await dep(request)
    assert exc_info.value.status_code == 401


async def test_token_mode_requires_token_even_for_localhost(fastapi_dep, token_settings):
    """token 模式下,本地请求也必须携带 token。"""
    from fastapi import HTTPException

    from agentkit.server.security import verify_token

    dep = verify_token(token_settings)
    request = _MockRequest({}, client_host="127.0.0.1")
    with pytest.raises(HTTPException) as exc_info:
        await dep(request)
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# create_security_middleware: CORS
# ---------------------------------------------------------------------------
def test_cors_disabled(fastapi_dep, local_settings):
    """cors_origins=[] → 无中间件。"""
    from agentkit.server.security import create_security_middleware

    middlewares = create_security_middleware(local_settings)
    assert middlewares == []


def test_cors_enabled(fastapi_dep):
    """cors_origins 非空 → 返回 CORSMiddleware 配置。"""
    from starlette.middleware.cors import CORSMiddleware

    from agentkit.server.security import create_security_middleware

    settings = ServerSettings(
        host="127.0.0.1",
        port=8000,
        token="",
        cors_origins=["http://localhost:3000", "https://app.example.com"],
    )
    middlewares = create_security_middleware(settings)
    assert len(middlewares) == 1
    mw_cls, mw_kwargs = middlewares[0]
    assert mw_cls is CORSMiddleware
    assert mw_kwargs["allow_origins"] == ["http://localhost:3000", "https://app.example.com"]
    assert mw_kwargs["allow_credentials"] is True
    assert set(mw_kwargs["allow_methods"]) == {"*"}
    assert set(mw_kwargs["allow_headers"]) == {"*"}


def test_cors_enabled_single_origin(fastapi_dep):
    """单个 origin 也能正确启用 CORS。"""
    from starlette.middleware.cors import CORSMiddleware

    from agentkit.server.security import create_security_middleware

    settings = ServerSettings(
        host="127.0.0.1",
        port=8000,
        token="",
        cors_origins=["http://localhost:3000"],
    )
    middlewares = create_security_middleware(settings)
    assert len(middlewares) == 1
    mw_cls, mw_kwargs = middlewares[0]
    assert mw_cls is CORSMiddleware
    assert mw_kwargs["allow_origins"] == ["http://localhost:3000"]


# ---------------------------------------------------------------------------
# token 时序安全（间接验证 _token_matches 用 hmac.compare_digest）
# ---------------------------------------------------------------------------
async def test_token_comparison_case_sensitive(fastapi_dep, token_settings):
    """token 比较大小写敏感。"""
    from fastapi import HTTPException

    from agentkit.server.security import verify_token

    dep = verify_token(token_settings)
    # secret_token vs SECRET_TOKEN → 不匹配
    request = _MockRequest({"Authorization": "Bearer SECRET_TOKEN"})
    with pytest.raises(HTTPException):
        await dep(request)
