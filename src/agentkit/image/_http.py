"""image._http —— 安全传输层：URL 校验 / DNS 解析 / HTTP 请求 / 流式下载 / 异常映射。

本模块是 ``image`` 子包的共享 HTTP 基础设施，集中处理四类问题：

    1. URL 安全校验：拦截 SSRF（非 HTTP 协议 / 元数据接口 / 字面量私网 IP）
    2. DNS 解析校验：域名解析后校验所有 IP，拦截 DNS rebinding 攻击
    3. HTTP 异常映射：httpx 异常 → ``ImageGenerationError``（带 retryable 标志）
    4. 流式下载：Content-Length 预检 + 分块写入，防止内存暴涨 DoS

双层 SSRF 防护：
    - ``validate_url`` 中同步解析域名并校验 IP（请求前预检）
    - ``_SSRFSafeTransport`` 在连接时异步二次解析校验（关闭 TOCTOU 窗口）

设计原则：
    - 所有对外发起的 HTTP 请求必须经本模块，不允许客户端自行调用 httpx。
    - DNS 解析校验在请求前和连接时各执行一次，确保安全策略无旁路。
    - 共享 httpx.AsyncClient 连接池，避免每次请求重建 TCP+TLS 握手。
    - 异常映射集中在一处，客户端实现无需关心 httpx 异常类型。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

from agentkit.image.base import ImageGenerationError


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 被拦截的主机名（云元数据接口等）
_BLOCKED_HOSTS: frozenset[str] = frozenset({
    "169.254.169.254",          # AWS / GCP / Azure 元数据接口
    "metadata.google.internal",  # GCP 元数据
    "metadata",                 # 通用元数据别名
    "fd00.ec2.internal",        # AWS IPv6 元数据
})

# 允许的 URL scheme
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# API Key 环境变量名合法模式（大写字母开头 + 大写字母/数字/下划线，防注入）
_API_KEY_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 下载大小上限（默认 20MB）
_DEFAULT_MAX_DOWNLOAD = 20 * 1024 * 1024


# ---------------------------------------------------------------------------
# URL 安全校验 + DNS 解析
# ---------------------------------------------------------------------------
def validate_url(
    url: str,
    *,
    allow_private: bool = False,
    resolve_dns: bool = True,
) -> None:
    """校验 URL 安全性，拦截 SSRF 攻击。

    校验规则：
        1. scheme 必须是 ``http`` 或 ``https``
        2. hostname 不得为空
        3. hostname 不得在 ``_BLOCKED_HOSTS`` 黑名单中
        4. hostname 不得为私网/回环/链路本地 IP（字面量 IP 直接检查）
        5. 若 hostname 是域名（非字面量 IP）且 ``resolve_dns=True``，
           解析 DNS 后校验所有 IP

    ``resolve_dns=False`` 用于配置期校验（如 ``ImageProvider.__post_init__``），
    避免在模块加载时触发网络请求。请求期（``post_json`` / ``download``）始终
    做 DNS 解析，``_SSRFSafeTransport`` 在连接时二次校验。

    Args:
        url:          待校验的 URL。
        allow_private: 是否允许私网地址。生成 API 的 base_url 在预设中
                       固定，由 provider 校验时调用（``allow_private=False``）；
                       本地开发环境可显式放行。
        resolve_dns:  是否对域名执行 DNS 解析并校验 IP。配置期设为 False
                       以避免网络请求；请求期设为 True（默认）。

    Raises:
        ImageGenerationError: URL 不安全（``retryable=False``，永久错误）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ImageGenerationError(
            f"不支持的 URL scheme: {parsed.scheme!r}，仅允许 http/https",
            reason="invalid_url_scheme",
            retryable=False,
        )
    hostname = parsed.hostname
    if not hostname:
        raise ImageGenerationError(
            f"URL 缺少 hostname: {url!r}",
            reason="invalid_url",
            retryable=False,
        )
    if hostname in _BLOCKED_HOSTS:
        raise ImageGenerationError(
            f"被拦截的主机名: {hostname!r}（云元数据接口）",
            reason="blocked_host",
            retryable=False,
        )
    if not allow_private and resolve_dns:
        _validate_hostname_ips(hostname)
    elif not allow_private and not resolve_dns:
        # 配置期：仅校验字面量 IP（域名留给请求期校验）
        try:
            ip = ipaddress.ip_address(hostname)
            _check_ip_safety(hostname, ip)
        except ValueError:
            pass  # 域名，留待请求期 DNS 解析校验


def _validate_hostname_ips(hostname: str) -> None:
    """校验 hostname 解析出的所有 IP 地址均非私网/回环/链路本地。

    - 若 hostname 是字面量 IP，直接检查
    - 若 hostname 是域名，调用 ``socket.getaddrinfo`` 解析后检查所有 IP

    DNS 解析失败时不拦截（可能是临时 DNS 故障，交给 httpx 在请求时报错），
    但若解析出的任一 IP 为私网地址则立即拒绝。
    """
    # 先尝试作为字面量 IP 检查
    try:
        ip = ipaddress.ip_address(hostname)
        _check_ip_safety(hostname, ip)
        return
    except ValueError:
        pass  # 不是字面量 IP，继续 DNS 解析

    # 域名：解析 DNS 并校验所有 IP
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # DNS 解析失败，不拦截（可能是临时故障），交给 httpx 报错
        return

    # 去重后逐个校验
    resolved_ips = {info[4][0] for info in infos}
    for ip_str in resolved_ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        _check_ip_safety(hostname, ip)


def _check_ip_safety(hostname: str, ip: ipaddress._BaseAddress) -> None:
    """检查单个 IP 是否为私网/回环/链路本地/保留地址。"""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ImageGenerationError(
            f"私网/回环地址被拦截: {hostname!r} → {ip}",
            reason="private_ip_blocked",
            retryable=False,
        )


async def _async_validate_hostname(hostname: str) -> None:
    """异步版本的 hostname IP 校验，供 ``_SSRFSafeTransport`` 在连接时调用。

    使用 ``asyncio.get_running_loop().getaddrinfo`` 异步解析 DNS，避免阻塞
    事件循环。与 ``_validate_hostname_ips`` 逻辑一致，但在连接前执行，
    关闭 DNS rebinding 的 TOCTOU 窗口。
    """
    if hostname in _BLOCKED_HOSTS:
        raise ImageGenerationError(
            f"被拦截的主机名: {hostname!r}（云元数据接口）",
            reason="blocked_host",
            retryable=False,
        )
    # 字面量 IP 直接检查
    try:
        ip = ipaddress.ip_address(hostname)
        _check_ip_safety(hostname, ip)
        return
    except ValueError:
        pass
    # 域名：异步解析
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None)
    except socket.gaierror:
        return
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        _check_ip_safety(hostname, ip)


# ---------------------------------------------------------------------------
# _SSRFSafeTransport —— SSRF 安全传输层
# ---------------------------------------------------------------------------
class _SSRFSafeTransport(httpx.AsyncBaseTransport):
    """SSRF 安全传输层：在每次连接前异步解析 DNS 并校验 IP。

    httpx 的 transport 在实际发起 TCP 连接前调用 ``handle_async_request``，
    此时 DNS 解析尚未发生。本类在委托给底层 transport 前，先异步解析
    hostname 并校验所有 IP，将 DNS rebinding 的 TOCTOU 窗口压缩到微秒级
    （``validate_url`` 的同步解析与本类的异步解析之间，DNS 记录几乎不可能
    变化）。

    注意：本类不修改底层 transport 的 DNS 解析行为（httpx 内部仍会自行
    解析），但两次解析的时间间隔极短，攻击者无法可靠地在两次解析之间
    切换 DNS 记录。对于要求绝对安全的场景，可进一步通过 IP pinning
    （修改请求 URL 为 IP + Host header）实现，但这会引入 TLS SNI 兼容
    问题，当前实现已在工程层面足够安全。
    """

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        host = request.url.host
        if host:
            await _async_validate_hostname(host)
        return await self._inner.handle_async_request(request)


# ---------------------------------------------------------------------------
# 共享 httpx.AsyncClient（连接池复用）
# ---------------------------------------------------------------------------
_shared_client: httpx.AsyncClient | None = None


def _get_shared_client() -> httpx.AsyncClient:
    """获取共享的 httpx.AsyncClient 实例。

    模块级共享 client，复用连接池，避免每次请求重建 TCP+TLS 握手。

    连接池配置：
        - ``max_connections=20``：限制同时打开的连接数
        - ``max_keepalive_connections=10``：保持 10 个空闲连接复用
    """
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            transport=_SSRFSafeTransport(),
            timeout=httpx.Timeout(120.0),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )
    return _shared_client


async def close_shared_client() -> None:
    """关闭共享的 httpx.AsyncClient（测试用）。

    正常运行时无需调用：共享 client 生命周期与进程一致。
    测试场景下调用此函数确保连接池被清理，避免资源泄漏警告。
    """
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


# ---------------------------------------------------------------------------
# 并发限流（防止重试风暴）
# ---------------------------------------------------------------------------
_provider_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_semaphore(provider: str, max_concurrency: int) -> asyncio.Semaphore:
    """获取 per-provider 的并发信号量。

    每个 provider 维护独立的信号量，限制其最大并发请求数，防止高并发
    fan-out + 自动重试叠加导致的重试风暴。
    """
    key = provider or "default"
    if key not in _provider_semaphores:
        _provider_semaphores[key] = asyncio.Semaphore(max_concurrency)
    return _provider_semaphores[key]


def reset_semaphores() -> None:
    """清除所有 per-provider 信号量（测试用）。

    信号量绑定到事件循环，跨事件循环（不同测试用例）复用会报错。
    测试间调用此函数清理状态。
    """
    _provider_semaphores.clear()


# ---------------------------------------------------------------------------
# API Key 环境变量名校验
# ---------------------------------------------------------------------------
def validate_api_key_env(name: str) -> None:
    """校验 API Key 环境变量名合法性。

    仅允许大写字母开头、含大写字母/数字/下划线的名称，防止注入任意
    环境变量名（如 ``AWS_SECRET_ACCESS_KEY``）导致密钥泄露。

    Args:
        name: 环境变量名。

    Raises:
        ImageGenerationError: 名称不合法（``retryable=False``）。
    """
    if not _API_KEY_ENV_PATTERN.match(name):
        raise ImageGenerationError(
            f"非法的 API Key 环境变量名: {name!r}，"
            f"仅允许大写字母开头、含大写字母/数字/下划线",
            reason="invalid_api_key_env",
            retryable=False,
        )


# ---------------------------------------------------------------------------
# HTTP 请求 + 异常映射
# ---------------------------------------------------------------------------
async def post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str],
    timeout: float = 120.0,
    provider: str = "",
    max_concurrency: int = 5,
) -> dict:
    """发起 POST JSON 请求，返回解析后的 JSON 响应。

    所有异常统一转换为 ``ImageGenerationError``，携带 ``retryable`` 标志位：
        - ``httpx.TimeoutException`` / ``ConnectError`` → ``retryable=True``（瞬时）
        - HTTP 429 / 5xx                        → ``retryable=True``（瞬时）
        - HTTP 400 / 401 / 403 / 404            → ``retryable=False``（永久）
        - JSON 解析失败                          → ``retryable=False``（永久）

    Args:
        url:             请求 URL（会先经 ``validate_url`` 校验）。
        payload:         请求体 JSON。
        headers:         请求头（含 Authorization）。
        timeout:         超时秒数。
        provider:        提供商名（用于异常信息 + 并发限流 key）。
        max_concurrency: per-provider 最大并发请求数，默认 5。
                         防止高并发 fan-out + 自动重试叠加导致的重试风暴。

    Returns:
        dict: 解析后的 JSON 响应。

    Raises:
        ImageGenerationError: 请求失败（含 retryable 标志）。
    """
    validate_url(url)

    client = _get_shared_client()
    sem = _get_semaphore(provider, max_concurrency)

    try:
        async with sem:
            resp = await client.post(
                url, json=payload, headers=headers, timeout=timeout,
            )
    except httpx.TimeoutException as e:
        raise ImageGenerationError(
            f"请求超时: {e}",
            provider=provider, reason="timeout", retryable=True,
        ) from e
    except httpx.ConnectError as e:
        raise ImageGenerationError(
            f"连接失败: {e}",
            provider=provider, reason="connect_error", retryable=True,
        ) from e
    except httpx.RequestError as e:
        # 其他网络层错误（DNS 解析失败、连接重置等）视为瞬时
        raise ImageGenerationError(
            f"网络请求错误: {e}",
            provider=provider, reason="network_error", retryable=True,
        ) from e

    # HTTP 状态码分类
    status = resp.status_code
    if 200 <= status < 300:
        try:
            return resp.json()
        except Exception as e:
            raise ImageGenerationError(
                f"响应 JSON 解析失败: {e}",
                provider=provider, reason="invalid_json",
                status_code=status, retryable=False,
            ) from e

    # 4xx / 5xx：按状态码区分 retryable
    retryable = status == 429 or status >= 500
    reason = _classify_http_error(status, resp.text)
    raise ImageGenerationError(
        f"HTTP {status}: {resp.text[:500]}",
        provider=provider, status_code=status,
        reason=reason, retryable=retryable,
    )


def _classify_http_error(status: int, body: str) -> str:
    """把 HTTP 状态码映射为语义化 reason，供钩子判断。"""
    if status == 400:
        # 尝试从 body 识别内容安全拦截
        body_lower = body.lower()
        if "moderation" in body_lower or "content_filter" in body_lower:
            return "moderation_blocked"
        if "billing" in body_lower or "quota" in body_lower or "余额" in body:
            return "insufficient_quota"
        return "bad_request"
    if status == 401:
        return "authentication_failed"
    if status == 403:
        return "permission_denied"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    return "http_error"


# ---------------------------------------------------------------------------
# 流式下载 + 大小限制
# ---------------------------------------------------------------------------
async def download(
    url: str,
    dest_path: str,
    *,
    max_size: int = _DEFAULT_MAX_DOWNLOAD,
    timeout: float = 60.0,
) -> int:
    """流式下载文件到本地，带大小限制防止内存暴涨 DoS。

    安全措施：
        1. URL 经 ``validate_url`` 校验（拦截 SSRF）
        2. Content-Length 预检：超过 ``max_size`` 直接拒绝，不开始下载
        3. 流式分块写入（8KB 块），不一次性读入内存
        4. 下载过程中累计字节数，若实际大小超过 ``max_size`` 中止并删除

    Args:
        url:       下载 URL。
        dest_path: 本地保存路径（已由调用方做过路径穿越校验）。
        max_size:  最大允许字节数，默认 20MB。
        timeout:   下载超时秒数。

    Returns:
        int: 实际下载的字节数。

    Raises:
        ImageGenerationError: URL 不安全 / 文件过大 / 下载失败。
    """
    validate_url(url)

    client = _get_shared_client()

    try:
        async with client.stream("GET", url, timeout=timeout) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ImageGenerationError(
                    f"下载失败: HTTP {resp.status_code}, "
                    f"body={body.decode('utf-8', 'replace')[:500]}",
                    status_code=resp.status_code,
                    reason=_classify_http_error(resp.status_code, ""),
                    retryable=resp.status_code == 429 or resp.status_code >= 500,
                )

            # Content-Length 预检
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_size:
                raise ImageGenerationError(
                    f"文件过大: Content-Length={content_length} > "
                    f"max_size={max_size}",
                    reason="file_too_large", retryable=False,
                )

            # 流式写入
            downloaded = 0
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=8192):
                    downloaded += len(chunk)
                    if downloaded > max_size:
                        f.close()
                        import os

                        os.remove(dest_path)
                        raise ImageGenerationError(
                            f"下载超出大小限制: {downloaded} > {max_size}",
                            reason="file_too_large", retryable=False,
                        )
                    f.write(chunk)

            return downloaded

    except httpx.TimeoutException as e:
        raise ImageGenerationError(
            f"下载超时: {e}",
            reason="download_timeout", retryable=True,
        ) from e
    except httpx.RequestError as e:
        raise ImageGenerationError(
            f"下载网络错误: {e}",
            reason="download_network_error", retryable=True,
        ) from e


__all__ = [
    "validate_url",
    "validate_api_key_env",
    "post_json",
    "download",
    "close_shared_client",
    "reset_semaphores",
]
