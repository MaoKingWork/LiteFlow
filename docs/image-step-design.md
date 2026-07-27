# ImageStep 设计方案（v3 优化版）

> LiteFlow v0.1.0 · 图片生成节点 · 2026年7月
>
> v2 针对 v1 的三类安全问题（Provider 零校验、路径/下载无防护、错误分类边界不清）做了修订，并补充了图片链式传递设计。v3 修复 v2 审核中发现的四个残留问题：SSRF 域名解析失效、路径穿越前缀匹配旁路、连接池复用矛盾与无限流、工厂硬编码与 provider\_type 未校验。

---

## 1. 设计目标与原则

为 LiteFlow 工作流添加图片生成节点（`type: image`），与现有 LLM 文本生成节点对称，支持多图片生成服务商（MiniMax、AIMIXHUB、StepFun 等），通过统一的抽象层实现"一套接口，多供应商"。

| 原则 | 落地方式 |
|------|----------|
| 轻量 | 仅新增 `image/` 子包 + `image_step.py`，不修改任何现有模块的核心逻辑；依赖仅 httpx（已有） |
| 稳定 | 所有客户端实现统一的 `ImageClient` ABC；不可变 dataclass 传递参数；HTTP 传输层集中处理异常并按 retryable 分类 |
| 安全 | URL 白名单 + 私网拦截 + 环境变量名校验 + 路径穿越防护 + 下载大小上限；API Key 不硬编码 |
| 易于维护 | 每个文件单一职责，中文 docstring 完整；模块间零循环依赖 |
| 易于扩展 | 新增图片服务商只需继承 `ImageClient` + 注册 `ImageProvider`，零改动现有代码 |
| 易于使用 | YAML 声明式配置，3 行即可生成图片；图片链式传递通过 `output_url` 一行实现 |
| 接口清晰 | 与 `llm/` 子包完全对称的 API 设计，学习成本趋近于零 |

---

## 2. 架构总览

新增 `image/` 子包，与现有 `llm/` 子包平行对称。相比 v1，新增 `image/_http.py` 安全传输层，集中处理 URL 校验、HTTP 请求、异常映射与流式下载。

```
llm/                                 image/
├── base.py    (LLMClient ABC)       ├── base.py     (ImageClient ABC + 数据类 + 异常)
├── provider.py (LLMProvider)        ├── provider.py (ImageProvider + 注册表 + 安全校验)
├── openai.py  (OpenAIClient)        ├── _http.py    (安全传输层：URL校验/请求/下载)  ← v2 新增
├── deepseek.py                     ├── openai.py   (OpenAIImageClient)
├── mimo.py                         ├── minimax.py  (MiniMaxImageClient)
└── mock.py    (MockClient)          ├── mock.py     (MockImageClient)
                                     └── __init__.py

steps/
├── llm_step.py  (LLMStep)           ├── image_step.py  (ImageStep)
└── __init__.py                      └── __init__.py     (注册 ImageStep)
```

**调用链**：

1. YAML `type: image` → `StepRegistry` 反序列化为 `ImageStep`
2. `ImageStep.run()` → 解析 prompt 模板 → 解析参考图（可能来自上游 ImageStep 输出）→ 取 `ImageClient` → 调用 `client.generate()`
3. `ImageClient` 由 `create_image_client(provider)` 工厂创建，内部通过 `_http.py` 发起请求
4. 返回 `ImageResponse` → 转换为 `list[ImageRef]` → 可选下载到本地 → 写入 `Context`
5. 下游 Step 通过 `{{step_id.output_url}}` 或 `{{step_id.images[0].url}}` 引用图片

---

## 3. 模块结构

```
src/agentkit/
├── llm/                        # 已有：LLM 客户端子包
├── image/                      # 新增：图片生成客户端子包
│   ├── __init__.py             #   包初始化 + 导出 + 默认客户端机制
│   ├── base.py                 #   ImageClient ABC + 数据类 + ImageGenerationError
│   ├── _http.py                #   安全传输层：URL校验 / HTTP请求 / 流式下载 / 异常映射
│   ├── provider.py             #   ImageProvider + 注册表 + 工厂 + 安全校验
│   ├── openai.py               #   OpenAIImageClient (OpenAI 兼容 API)
│   ├── minimax.py              #   MiniMaxImageClient (MiniMax 原生 API)
│   └── mock.py                 #   MockImageClient (测试用)
├── steps/
│   ├── llm_step.py
│   ├── image_step.py           #   新增：ImageStep
│   └── __init__.py             #   修改：导入 ImageStep 触发注册
└── ...
```

模块间依赖关系：

- `image/base.py` → 仅依赖标准库（abc, dataclasses, typing），零外部依赖
- `image/_http.py` → 依赖 `image/base.py`，httpx，标准库（ipaddress, urllib.parse, re, os）
- `image/provider.py` → 依赖 `image/base.py`（TYPE_CHECKING），`image/_http.py`（校验函数），标准库
- `image/openai.py` → 依赖 `image/base.py`，`image/_http.py`，httpx
- `image/minimax.py` → 依赖 `image/base.py`，`image/_http.py`，httpx
- `image/mock.py` → 依赖 `image/base.py`，标准库
- `steps/image_step.py` → 依赖 `steps/base.py`，`image/base.py`，`image/_http.py`，`core/template.py`

---

## 4. 核心抽象：image/base.py

定义图片生成的统一接口与数据类。相比 v1，`ImageGenerationError` 新增 `retryable` 标志位，`ImageRef` 新增 `to_url()` 方法用于链式传递。

### 4.1 数据类

```python
"""image.base —— 图片生成客户端抽象基类与核心数据类。

设计原则与 llm/base.py 完全对称：
    - 仅依赖 Python 标准库（abc / dataclasses / typing）
    - 无循环依赖，可被任意子模块安全导入
    - 新增图片服务商只需继承 ImageClient 并实现 generate
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ── ImageRequest：图片生成请求（frozen，防止运行期篡改）──
@dataclass(frozen=True)
class ImageRequest:
    """图片生成请求。

    统一描述文生图与图生图的参数，各客户端实现负责映射到具体 API 格式。

    Attributes:
        prompt:            图像描述文本（必填）。
        model:             模型名。为空时由客户端使用其默认模型。
        n:                 生成数量，默认 1。
        size:              图片尺寸，如 "1024x1024"。None 表示由 API 决定。
        aspect_ratio:      宽高比，如 "16:9"。部分 API（MiniMax）使用此字段而非 size。
        seed:              随机种子。None 表示随机。
        response_format:   返回格式："url" | "base64"。默认 "url"。
        quality:           渲染质量："low" | "medium" | "high" | None。
        reference_images:  参考图 URL 列表（图生图）。None 表示纯文生图。
        extra:             提供商特有参数（透传，不解析）。
    """
    prompt: str
    model: str = ""
    n: int = 1
    size: str | None = None
    aspect_ratio: str | None = None
    seed: int | None = None
    response_format: str = "url"
    quality: str | None = None
    reference_images: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ── GeneratedImage：单张生成图片 ──
@dataclass
class GeneratedImage:
    """单张生成图片的结果。

    url 和 b64_json 通常二选一（由 response_format 决定）。

    Attributes:
        url:            图片 URL（可能有过期时间）。
        b64_json:       Base64 编码的图片数据。
        content_type:   MIME 类型，如 "image/png"。
        seed:           生成时使用的种子。
        finish_reason:  结束原因："success" | "content_filtered" 等。
    """
    url: str | None = None
    b64_json: str | None = None
    content_type: str = "image/png"
    seed: int | None = None
    finish_reason: str | None = None


# ── ImageResponse：生成响应 ──
@dataclass
class ImageResponse:
    """图片生成响应。

    Attributes:
        images:  生成的图片列表。
        model:   实际使用的模型名。
        created: 创建时间戳。
        raw:     原始响应（调试用）。
        usage:   部分 API 返回的 token 用量。
    """
    images: list[GeneratedImage] = field(default_factory=list)
    model: str = ""
    created: int | None = None
    raw: Any = None
    usage: dict[str, Any] | None = None


# ── ImageRef：写入 Context 的图片引用（最终输出）──
@dataclass
class ImageRef:
    """图片引用（写入 Context 供下游 Step 使用）。

    设计为可序列化的轻量结构，避免在 Context 中存储大块 base64。
    当 save_local=True 时，local_path 指向本地文件，b64_json 被清除。

    链式传递：下游 Step 通过 reference_image: "{{prev_step.output_url}}"
    即可引用本 Step 生成的图片。to_url() 方法按优先级返回可用的 URL：
    原始 URL > 本地文件 file:// 路径 > data URI（base64 内嵌）。

    Attributes:
        url:           原始 URL（可能过期）。
        b64_json:      Base64 数据（save_local=True 时为 None）。
        local_path:    本地文件路径（save_local=True 时有值）。
        content_type:  MIME 类型。
        size:          文件大小（字节）。
        seed:          生成种子。
        finish_reason: 结束原因。
    """
    url: str | None = None
    b64_json: str | None = None
    local_path: str | None = None
    content_type: str = "image/png"
    size: int = 0
    seed: int | None = None
    finish_reason: str | None = None

    def to_url(self) -> str | None:
        """返回可用于下游消费的图片 URL（链式传递核心方法）。

        优先级：
            1. self.url（API 返回的原始 URL，可直接被图生图 API 消费）
            2. self.local_path 转 file:// URI（本地文件，可被本地服务消费）
            3. self.b64_json 转 data URI（base64 内嵌，通用但体积大）

        Returns:
            str | None: 图片 URL；无任何可用数据时返回 None。
        """
        if self.url:
            return self.url
        if self.local_path:
            import pathlib
            return pathlib.Path(self.local_path).as_uri()
        if self.b64_json:
            return f"data:{self.content_type};base64,{self.b64_json}"
        return None

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（便于 JSON 序列化与 Context 冻结）。"""
        return {
            "url": self.url,
            "b64_json": self.b64_json,
            "local_path": self.local_path,
            "content_type": self.content_type,
            "size": self.size,
            "seed": self.seed,
            "finish_reason": self.finish_reason,
        }
```

### 4.2 ImageClient 抽象基类

```python
class ImageClient(ABC):
    """图片生成客户端抽象基类。

    所有图片生成服务商客户端必须继承本类并实现 generate 方法。
    ImageStep 通过本接口与具体提供商解耦。

    实现方约定：
        - generate 为协程，支持异步并发调用。
        - 返回 ImageResponse，其中 raw 保留原始响应便于调试。
        - HTTP 传输层异常由 _http.py 统一映射为 ImageGenerationError，
          实现方只需调用 _http.post_json() / _http.download()，无需自行
          处理 httpx 异常。
        - 客户端实例可复用（httpx 连接池由 _http.py 管理）。
    """

    @abstractmethod
    async def generate(
        self,
        request: ImageRequest,
    ) -> ImageResponse:
        """调用图片生成 API。

        Args:
            request: 图片生成请求（含 prompt / model / size 等参数）。

        Returns:
            ImageResponse: 生成结果（含图片列表）。

        Raises:
            ImageGenerationError: 生成失败（网络 / 鉴权 / 内容安全等）。
        """
```

### 4.3 ImageGenerationError（v2 核心改进：retryable 标志位）

```python
class ImageGenerationError(Exception):
    """图片生成失败的统一异常。

    v2 核心改进：新增 retryable 标志位，让 BaseStep.execute 的重试逻辑
    能区分"瞬时错误（值得重试）"与"永久错误（重试只会浪费钱和延迟）"。

    分类规则（由 _http.py 的异常映射自动设置）：
        - retryable=True:  网络超时、连接重置、429 限流、5xx 服务端错误。
        - retryable=False: 400 参数错误、401 鉴权失败、403 内容安全拦截、
                           余额不足、模型不存在等。

    图片生成单次调用成本远高于文本 token，对永久错误的重试是实打实的
    金钱与延迟浪费。retryable 标志位让重试策略精准命中瞬时错误。

    Attributes:
        provider:    提供商名。
        status_code: API 返回的 HTTP 状态码（如有）。
        reason:      失败原因简述（如 "moderation_blocked" / "timeout"）。
        retryable:   是否值得重试。True 表示瞬时错误，False 表示永久错误。
    """
    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = None,
        reason: str = "",
        retryable: bool = True,
    ):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.reason = reason
        self.retryable = retryable
```

---

## 5. 安全传输层：image/\_http.py（v2 新增）

这是 v2 设计的核心新增模块。v1 的 `_post()` 和 `_download()` 只在调用处出现，从未定义，把最容易出错的 HTTP 传输层完全留白。本模块集中解决三个问题：

1. **URL 安全校验**：拦截 SSRF（内网地址、云元数据接口、非 HTTP 协议）
2. **HTTP 请求 + 异常映射**：把 httpx 的各种异常统一转换为带 `retryable` 标志的 `ImageGenerationError`
3. **流式下载 + 大小限制**：防止内存暴涨 DoS，支持 Content-Length 预检与流式写入

### 5.1 URL 安全校验 + DNS 解析 + 安全传输

```python
"""image._http —— 安全传输层：URL 校验 / DNS 解析 / HTTP 请求 / 流式下载 / 异常映射。

本模块是 image 子包的共享 HTTP 基础设施，集中处理四类问题：
    1. URL 安全校验：拦截 SSRF（非 HTTP 协议 / 元数据接口 / 字面量私网 IP）
    2. DNS 解析校验：域名解析后校验所有 IP，拦截 DNS rebinding 攻击
    3. HTTP 异常映射：httpx 异常 → ImageGenerationError（带 retryable 标志）
    4. 流式下载：Content-Length 预检 + 分块写入，防止内存暴涨 DoS

v3 核心改进（修复 v2 的 SSRF 域名失效问题）：
    v2 的 _check_private_ip 对域名直接 return，攻击者用一个自己持有的
    域名把 DNS 解析指向 169.254.169.254 即可绕过全部校验。v3 通过两层
    防护关闭此漏洞：
        - validate_url 中同步解析域名并校验 IP（请求前预检）
        - _SSRFSafeTransport 在连接时异步二次解析校验（关闭 TOCTOU 窗口）

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


# ── 被拦截的主机名（云元数据接口等）──
_BLOCKED_HOSTS: frozenset[str] = frozenset({
    "169.254.169.254",   # AWS / GCP / Azure 元数据接口
    "metadata.google.internal",  # GCP 元数据
    "metadata",          # 通用元数据别名
    "fd00.ec2.internal", # AWS IPv6 元数据
})

# ── 允许的 URL scheme ──
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# ── API Key 环境变量名合法模式（大写字母+下划线+数字，防注入）──
_API_KEY_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# ── 下载大小上限（默认 20MB，可由 config 覆盖）──
_DEFAULT_MAX_DOWNLOAD = 20 * 1024 * 1024


def validate_url(url: str, *, allow_private: bool = False) -> None:
    """校验 URL 安全性，拦截 SSRF 攻击。

    v3 改进：对域名执行 DNS 解析并校验所有解析出的 IP，不再跳过域名。

    校验规则：
        1. scheme 必须是 http 或 https
        2. hostname 不得为空
        3. hostname 不得在 _BLOCKED_HOSTS 黑名单中
        4. hostname 不得为私网/回环/链路本地 IP（字面量 IP 直接检查）
        5. 若 hostname 是域名（非字面量 IP），解析 DNS 后校验所有 IP
           - 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
           - 127.0.0.0/8, 169.254.0.0/16, ::1, fc00::/7

    DNS rebinding 防护：
        本函数的 DNS 解析在请求前执行，存在 TOCTOU 窗口（解析后 DNS 记录
        可能变化）。_SSRFSafeTransport 在连接时二次解析校验，将窗口压缩
        到微秒级。两层防护叠加后，DNS rebinding 攻击在实际场景中不可行。

    Args:
        url:          待校验的 URL。
        allow_private: 是否允许私网地址。生成 API 的 base_url 在预设中
                       固定，由 provider 校验时调用（allow_private=False）；
                       本地开发环境可显式放行。

    Raises:
        ImageGenerationError: URL 不安全（retryable=False，永久错误）。
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
    if not allow_private:
        _validate_hostname_ips(hostname)


def _validate_hostname_ips(hostname: str) -> None:
    """校验 hostname 解析出的所有 IP 地址均非私网/回环/链路本地。

    v3 改进（修复 v2 _check_private_ip 对域名跳过的问题）：
        - 若 hostname 是字面量 IP，直接检查
        - 若 hostname 是域名，调用 socket.getaddrinfo 解析后检查所有 IP

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
    """异步版本的 hostname IP 校验，供 _SSRFSafeTransport 在连接时调用。

    使用 asyncio.get_event_loop().getaddrinfo 异步解析 DNS，避免阻塞事件循环。
    与 _validate_hostname_ips 逻辑一致，但在连接前执行，关闭 DNS rebinding
    的 TOCTOU 窗口。
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
    loop = asyncio.get_event_loop()
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


class _SSRFSafeTransport(httpx.AsyncBaseTransport):
    """SSRF 安全传输层：在每次连接前异步解析 DNS 并校验 IP。

    v3 新增。httpx 的 transport 在实际发起 TCP 连接前调用
    handle_async_request，此时 DNS 解析尚未发生。本类在委托给底层
    transport 前，先异步解析 hostname 并校验所有 IP，将 DNS rebinding
    的 TOCTOU 窗口压缩到微秒级（validate_url 的同步解析与本类的异步
    解析之间，DNS 记录几乎不可能变化）。

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


# ── 共享 httpx.AsyncClient（连接池复用，v3 修复 v2 的"每次新建"问题）──
_shared_client: httpx.AsyncClient | None = None


def _get_shared_client() -> httpx.AsyncClient:
    """获取共享的 httpx.AsyncClient 实例。

    v3 修复：v2 的 post_json / download 每次调用都
    `async with httpx.AsyncClient()` 新建连接，导致每次请求都重建
    TCP+TLS 握手。v3 改为模块级共享 client，复用连接池。

    连接池配置：
        - max_connections=20：限制同时打开的连接数
        - max_keepalive_connections=10：保持 10 个空闲连接复用
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


# ── 并发限流（v3 新增：防止重试风暴）──
_provider_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_semaphore(provider: str, max_concurrency: int) -> asyncio.Semaphore:
    """获取 per-provider 的并发信号量。"""
    key = provider or "default"
    if key not in _provider_semaphores:
        _provider_semaphores[key] = asyncio.Semaphore(max_concurrency)
    return _provider_semaphores[key]


def validate_api_key_env(name: str) -> None:
    """校验 API Key 环境变量名合法性。

    仅允许大写字母开头、含大写字母/数字/下划线的名称，防止注入任意
    环境变量名（如 AWS_SECRET_ACCESS_KEY）导致密钥泄露。

    Args:
        name: 环境变量名。

    Raises:
        ImageGenerationError: 名称不合法（retryable=False）。
    """
    if not _API_KEY_ENV_PATTERN.match(name):
        raise ImageGenerationError(
            f"非法的 API Key 环境变量名: {name!r}，"
            f"仅允许大写字母开头、含大写字母/数字/下划线",
            reason="invalid_api_key_env",
            retryable=False,
        )
```

### 5.2 HTTP 请求 + 异常映射

```python
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

    v3 改进：
        - 使用共享 httpx.AsyncClient（连接池复用），不再每次新建
        - 通过 per-provider Semaphore 限制并发，防止重试风暴
        - SSRF 校验由 _SSRFSafeTransport 在连接时二次执行

    所有异常统一转换为 ImageGenerationError，携带 retryable 标志位：
        - httpx.TimeoutException / ConnectError → retryable=True（瞬时）
        - HTTP 429 / 5xx                        → retryable=True（瞬时）
        - HTTP 400 / 401 / 403 / 404            → retryable=False（永久）
        - JSON 解析失败                          → retryable=False（永久）

    Args:
        url:             请求 URL（会先经 validate_url 校验）。
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
    if status >= 200 and status < 300:
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
```

### 5.3 流式下载 + 大小限制

```python
async def download(
    url: str,
    dest_path: str,
    *,
    max_size: int = _DEFAULT_MAX_DOWNLOAD,
    timeout: float = 60.0,
) -> int:
    """流式下载文件到本地，带大小限制防止内存暴涨 DoS。

    安全措施：
        1. URL 经 validate_url 校验（拦截 SSRF）
        2. Content-Length 预检：超过 max_size 直接拒绝，不开始下载
        3. 流式分块写入（8KB 块），不一次性读入内存
        4. 下载过程中累计字节数，若实际大小超过 max_size 中止并删除

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

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 先发 HEAD 或 GET 获取 Content-Length
            async with client.stream("GET", url) as resp:
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
```

---

## 6. Provider 系统：image/provider.py

与 `llm/provider.py` 对称的提供商配置层。v2 核心改进：`resolve_api_key()` 和 `base_url` 新增安全校验。

### 6.1 ImageProvider 配置（v2 新增安全校验）

```python
@dataclass(frozen=True)
class ImageProvider:
    """图片生成提供商配置。

    v2 安全改进：
        - __post_init__ 校验 base_url 合法性（scheme + 非私网）
        - resolve_api_key 校验 api_key_env 变量名合法性
    v3 安全改进：
        - __post_init__ 校验 provider_type 必须为已注册类型，
          防止拼写错误被静默兜底为 OpenAI 处理

    Attributes:
        name:          提供商标识（如 "minimax" / "aihubmix" / "stepfun"）。
        base_url:      API 根地址（经 SSRF 校验）。
        api_key:       API Key；None 时从环境变量读取。
        api_key_env:   API Key 环境变量名（经名称合法性校验）。
        model:         默认模型名。
        provider_type: 提供商类型："openai" | "minimax"（v3 起需为已注册类型）。
    """
    name: str
    base_url: str
    api_key: str | None = None
    api_key_env: str = ""
    model: str = ""
    provider_type: str = "openai"

    def __post_init__(self) -> None:
        """frozen dataclass 的校验入口。

        v3 新增 provider_type 合法性校验：v2 的 else 兜底分支会把
        未知 provider_type（如手滑写成 "openia"）静默当 OpenAI 处理，
        延迟到运行时才以晦涩的 400 错误暴露。v3 在注册时就报错。
        """
        if not self.name:
            raise ValueError("ImageProvider.name 不能为空")
        # v3 新增：校验 provider_type 必须是已注册的类型
        valid_types = _get_registered_provider_types()
        if self.provider_type not in valid_types:
            raise ValueError(
                f"ImageProvider.provider_type={self.provider_type!r} 不合法，"
                f"当前已注册类型: {valid_types}"
            )
        # 校验 base_url：预设的公网 API 不应指向私网
        from agentkit.image._http import validate_url
        validate_url(self.base_url, allow_private=False)
        # 校验 api_key_env 名称合法性（非空时）
        if self.api_key_env:
            from agentkit.image._http import validate_api_key_env
            validate_api_key_env(self.api_key_env)

    def resolve_api_key(self) -> str | None:
        """解析 API Key（v2 新增环境变量名校验）。

        与 LLMProvider.resolve_api_key 逻辑一致，但在读取环境变量前
        校验变量名合法性，防止通过 YAML 注入 AWS_SECRET_ACCESS_KEY
        等其他服务密钥名。

        Returns:
            str | None: API Key，或 None（未配置）。
        """
        if self.api_key is not None:
            return self.api_key
        if self.api_key_env:
            # 校验已 __post_init__ 中完成，此处直接读取
            return os.getenv(self.api_key_env)
        return None
```

### 6.2 内置预设提供商

```python
# 三个内置预设，覆盖两份 API 文档中的主要服务商
PRESET_IMAGE_PROVIDERS: dict[str, ImageProvider] = {
    "minimax": ImageProvider(
        name="minimax",
        base_url="https://api.minimaxi.com",
        api_key_env="MINIMAX_API_KEY",
        model="image-01",
        provider_type="minimax",
    ),
    "aihubmix": ImageProvider(
        name="aihubmix",
        base_url="https://aihubmix.com/v1",
        api_key_env="AIHUBMIX_API_KEY",
        model="gpt-image-1",
        provider_type="openai",
    ),
    "stepfun": ImageProvider(
        name="stepfun",
        base_url="https://api.stepfun.com/v1",
        api_key_env="STEPFUN_API_KEY",
        model="step-1x-medium",
        provider_type="openai",
    ),
}
```

### 6.3 注册表与工厂函数

API 设计与 `llm/provider.py` 完全对称：

| LLM（已有） | Image（新增） | 用途 |
|-------------|---------------|------|
| `ProviderRegistry` | `ImageProviderRegistry` | 提供商注册表 |
| `register_provider()` | `register_image_provider()` | 注册自定义提供商 |
| `get_provider()` | `get_image_provider()` | 按名获取提供商 |
| `list_providers()` | `list_image_providers()` | 列出所有提供商名 |
| `resolve_provider()` | `resolve_image_provider()` | 解析提供商配置 |
| `create_client()` | `create_image_client()` | 工厂创建客户端 |

```python
# ── v3 修复：dict 注册表替代硬编码 if/else ──
# v2 的 create_image_client 用 if/else 硬编码 provider_type 路由，
# else 分支兜底为 OpenAIImageClient，导致未知 provider_type 被静默
# 当 OpenAI 处理。v3 改为 dict 注册表 + 显式注册函数，新增服务商
# 只需 register_image_client("stability", StabilityClient) 一行，
# 无需修改工厂函数本身。

_CLIENT_REGISTRY: dict[str, type[ImageClient]] = {}


def register_image_client(
    provider_type: str,
    client_cls: type[ImageClient],
) -> None:
    """注册 provider_type → ImageClient 子类的映射。

    扩展新服务商时调用此函数即可，无需修改 create_image_client。

    Args:
        provider_type: 提供商类型标识（如 "openai" / "minimax"）。
        client_cls:    ImageClient 子类。构造函数需接受
                       api_key / base_url / model 关键字参数。
    """
    if provider_type in _CLIENT_REGISTRY:
        raise ValueError(
            f"provider_type={provider_type!r} 已注册为 "
            f"{_CLIENT_REGISTRY[provider_type].__name__}"
        )
    _CLIENT_REGISTRY[provider_type] = client_cls


def _get_registered_provider_types() -> set[str]:
    """返回当前已注册的所有 provider_type（供 ImageProvider 校验）。"""
    return set(_CLIENT_REGISTRY.keys())


# ── 内置注册（模块加载时执行）──
register_image_client("openai", OpenAIImageClient)
register_image_client("minimax", MiniMaxImageClient)


def create_image_client(provider: ImageProvider | str | None = None) -> ImageClient:
    """根据提供商配置创建图片生成客户端。

    v3 修复：用 _CLIENT_REGISTRY 字典查找替代 v2 的 if/else 硬编码。
    未知 provider_type 不再静默兜底为 OpenAI，而是显式抛出 ValueError。

    Args:
        provider: ImageProvider 实例 / 提供商名 / None。
                  None 时用全局默认提供商。

    Returns:
        ImageClient: 对应的客户端实例。

    Raises:
        ValueError: provider_type 未注册（v3 新增，v2 会静默兜底）。
    """
    if provider is None:
        provider = resolve_image_provider(None)
    elif isinstance(provider, str):
        provider = resolve_image_provider(provider)

    client_cls = _CLIENT_REGISTRY.get(provider.provider_type)
    if client_cls is None:
        # v3：显式报错，不再静默兜底
        raise ValueError(
            f"未注册的 provider_type={provider.provider_type!r}，"
            f"已注册类型: {list(_CLIENT_REGISTRY.keys())}"
        )
    return client_cls(
        api_key=provider.resolve_api_key(),
        base_url=provider.base_url,
        model=provider.model,
    )
```

---

## 7. 客户端实现

所有客户端通过 `image/_http.py` 发起请求，不再直接调用 httpx，确保安全策略无旁路。

### 7.1 OpenAIImageClient（image/openai.py）

适用于所有兼容 OpenAI Images API（`/v1/images/generations`）的服务商：AIMIXHUB、StepFun 以及 OpenAI 官方。

```python
class OpenAIImageClient(ImageClient):
    """OpenAI 兼容图片生成客户端。

    适用于 AIMIXHUB (gpt-image-1)、StepFun (step-1x-medium) 等
    兼容 OpenAI /v1/images/generations 接口的服务商。

    v2 改进：通过 _http.post_json() 发起请求，异常由传输层统一映射。

    请求映射：
        ImageRequest → OpenAI images.generate payload
        - prompt          → prompt
        - model           → model
        - n               → n
        - size            → size
        - quality         → quality
        - response_format → response_format (url/b64_json)
        - seed            → 透传到 extra_body
        - reference_images → 切换到 /v1/images/edits 端点（图生图）
        - extra           → 合并到请求体
    """

    def __init__(self, api_key: str | None, base_url: str, model: str = ""):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def generate(self, request: ImageRequest) -> ImageResponse:
        # 1. 构建请求 payload
        payload = self._build_payload(request)

        # 2. 选择端点：文生图 /images/generations，图生图 /images/edits
        if request.reference_images:
            url = f"{self._base_url}/images/edits"
            return await self._post_edit(url, payload, request)
        else:
            url = f"{self._base_url}/images/generations"
            return await self._post_generate(url, payload)

    async def _post_generate(self, url: str, payload: dict) -> ImageResponse:
        """文生图：POST JSON → 解析响应。"""
        from agentkit.image._http import post_json

        headers = {"Authorization": f"Bearer {self._api_key}"}
        data = await post_json(url, payload, headers=headers, provider="openai")

        images = []
        for item in data.get("data", []):
            images.append(GeneratedImage(
                url=item.get("url"),
                b64_json=item.get("b64_json"),
                seed=item.get("seed"),
                finish_reason=item.get("finish_reason"),
            ))
        return ImageResponse(images=images, model=data.get("model", ""), raw=data)

    def _build_payload(self, request: ImageRequest) -> dict:
        """将 ImageRequest 映射为 OpenAI 兼容 payload。"""
        payload = {
            "model": request.model or self._model,
            "prompt": request.prompt,
            "n": request.n,
        }
        if request.size:
            payload["size"] = request.size
        if request.quality:
            payload["quality"] = request.quality
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.response_format == "base64":
            payload["response_format"] = "b64_json"
        else:
            payload["response_format"] = "url"
        payload.update(request.extra)
        return payload
```

### 7.2 MiniMaxImageClient（image/minimax.py）

```python
class MiniMaxImageClient(ImageClient):
    """MiniMax 图片生成客户端。

    适用于 MiniMax image-01 / image-01-live 模型。
    使用 MiniMax 原生 /v1/image_generation 端点（非 OpenAI 兼容）。

    v2 改进：通过 _http.post_json() 发起请求，异常由传输层统一映射。
    """

    def __init__(self, api_key: str | None, base_url: str, model: str = ""):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def generate(self, request: ImageRequest) -> ImageResponse:
        from agentkit.image._http import post_json

        url = f"{self._base_url}/v1/image_generation"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        resp = await post_json(
            url, self._build_payload(request),
            headers=headers, provider="minimax",
        )

        # 检查 MiniMax 特有的 base_resp 状态码
        base_resp = resp.get("base_resp", {})
        status_code = base_resp.get("status_code", 0)
        if status_code != 0:
            status_msg = base_resp.get("status_msg", "unknown")
            # MiniMax 业务错误码：1008=余额不足，1027=内容审核
            retryable = status_code in (1007, 1029)  # 限流类
            raise ImageGenerationError(
                f"MiniMax API 错误: {status_msg}",
                provider="minimax", status_code=status_code,
                reason=status_msg, retryable=retryable,
            )

        # 解析响应
        data = resp.get("data", {})
        images = []
        for url in data.get("image_urls", []):
            images.append(GeneratedImage(url=url))
        for b64 in data.get("image_base64", []):
            images.append(GeneratedImage(b64_json=b64))
        return ImageResponse(images=images, model=request.model, raw=resp)

    def _build_payload(self, request: ImageRequest) -> dict:
        payload = {
            "model": request.model or self._model,
            "prompt": request.prompt,
            "n": request.n,
        }
        if request.aspect_ratio:
            payload["aspect_ratio"] = request.aspect_ratio
        if request.seed is not None:
            payload["seed"] = request.seed
        payload["response_format"] = request.response_format
        if request.reference_images:
            payload["subject_reference"] = [
                {"type": "character", "image_file": url}
                for url in request.reference_images
            ]
        payload.update(request.extra)
        return payload
```

### 7.3 MockImageClient（image/mock.py）

```python
class MockImageClient(ImageClient):
    """测试用 Mock 图片生成客户端。

    不发网络请求，返回固定的占位图片。用法与 MockClient（LLM）对称。
    """

    def __init__(self, model: str = "mock-image"):
        self._model = model

    async def generate(self, request: ImageRequest) -> ImageResponse:
        return ImageResponse(
            images=[
                GeneratedImage(
                    url=f"mock://image-{i}.png",
                    content_type="image/png",
                    seed=request.seed or 42,
                    finish_reason="success",
                )
                for i in range(request.n)
            ],
            model=self._model,
            raw={"mock": True},
        )
```

---

## 8. ImageStep 设计

与 `LLMStep` 对称的 Step 实现。v2 核心改进：路径穿越防护、安全的本地保存、图片链式传递。

### 8.1 构造函数

```python
@register_step("image")
class ImageStep(BaseStep):
    """图片生成 Step：调用 ImageClient 生成图片，写入 Context。

    v2 改进：
        - 新增 output_url 输出端口：自动写入首个图片的 URL，便于下游
          Step 用 {{step_id.output_url}} 链式引用。
        - _save_local 增加路径穿越防护与文件名清洗。
        - _download 使用 _http.download() 流式下载 + 大小限制。

    Args:
        id:              Step 实例标识。
        prompt:          图像描述模板，支持 {{var}} / ${ENV}。
        model:           模型名覆盖；None 时用 provider 默认模型。
        provider:        提供商名（预设或自定义注册名）；None 时用全局默认。
        n:               生成数量，默认 1。
        size:            图片尺寸，如 "1024x1024"。
        aspect_ratio:    宽高比，如 "16:9"（MiniMax 使用）。
        quality:         渲染质量："low" | "medium" | "high"。
        seed:            随机种子。
        response_format: 返回格式："url" | "base64"。默认 "url"。
        reference_image: 参考图 URL 或 Context 变量（图生图）。
                         支持 str（单图）或 list[str]（多图）。
                         链式传递：reference_image: "{{prev_step.output_url}}"
        save_local:      是否下载/保存到本地文件。默认 False。
        output_dir:      本地保存目录；None 时用 config 默认值。
        output:          输出键名；结果（list[ImageRef]）通过 ctx.set 写入。
        image_client:    客户端注入（测试用 MockImageClient）。
        retry:           实例级重试策略。
        timeout:         实例级超时秒数。
    """

    type = "image"

    def __init__(
        self,
        id: str = "",
        prompt: str = "",
        model: str | None = None,
        provider: str | None = None,
        n: int = 1,
        size: str | None = None,
        aspect_ratio: str | None = None,
        quality: str | None = None,
        seed: int | None = None,
        response_format: str = "url",
        reference_image: str | list[str] | None = None,
        save_local: bool = False,
        output_dir: str | None = None,
        output: str | None = None,
        image_client: ImageClient | None = None,
        retry: RetryPolicy | None = None,
        timeout: float | None = None,
        *,
        inputs: list | None = None,
        outputs: list | None = None,
        strict_scope: bool = False,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(
            id=id, output=output, retry=retry, timeout=timeout,
            inputs=inputs, outputs=outputs, strict_scope=strict_scope,
        )
        self.prompt = prompt
        self.model = model
        self.provider = provider
        self.n = n
        self.size = size
        self.aspect_ratio = aspect_ratio
        self.quality = quality
        self.seed = seed
        self.response_format = response_format
        self.reference_image = reference_image
        self.save_local = save_local
        self.output_dir = output_dir
        self.image_client = image_client
        self.extra = extra or {}
        # trace scratch
        self._last_provider = ""
        self._last_model = ""
        self._last_n_generated = 0
```

### 8.2 run 方法

```python
async def run(self, ctx: Context) -> Context:
    """执行图片生成并写入 output。

    流程：
        1. 重置 trace scratch。
        2. 解析 prompt 模板（支持 {{var}} / ${ENV}）。
        3. 解析 reference_image（图生图，支持 Context 变量引用 + 链式传递）。
        4. 构建 ImageRequest（frozen dataclass）。
        5. 取 ImageClient（注入优先 → 提供商缓存 → 全局默认）。
        6. 调用 client.generate(request)。
        7. 转换 ImageResponse → list[ImageRef]。
        8. save_local=True 时下载/保存图片到本地（v2 带路径安全校验）。
        9. ctx.set(self.output, image_refs) + ctx.set(output + "_url", url)。
    """
    # 1. 重置 scratch
    self._last_provider = ""
    self._last_model = ""
    self._last_n_generated = 0

    # 2. 解析 prompt 模板
    prompt = self._render_str(self.prompt, ctx)

    # 3. 解析 reference_image（支持链式传递）
    ref_images = self._resolve_reference_images(ctx)

    # 4. 构建 ImageRequest
    request = ImageRequest(
        prompt=prompt,
        model=self.model or "",
        n=self.n,
        size=self.size,
        aspect_ratio=self.aspect_ratio,
        seed=self.seed,
        response_format=self.response_format,
        quality=self.quality,
        reference_images=ref_images,
        extra=dict(self.extra),
    )

    # 5. 取客户端
    client = self._get_client()

    # 6. 调用 generate
    response = await client.generate(request)
    self._last_provider = self.provider or "default"
    self._last_model = response.model or self.model or ""
    self._last_n_generated = len(response.images)

    # 7. 转换为 ImageRef 列表
    image_refs = self._to_image_refs(response)

    # 8. save_local 时下载到本地（v2 带安全校验）
    if self.save_local:
        image_refs = await self._save_local(image_refs, ctx)

    # 9. 写入 Context（主输出 + 便捷 URL 输出）
    if len(self.outputs) > 1:
        self._emit_dict_outputs(ctx, {"images": image_refs})
    elif self.output:
        ctx.set(self.output, image_refs)
        # 链式传递：自动写入 output_url，供下游 Step 引用
        if image_refs:
            ctx.set(f"{self.output}_url", image_refs[0].to_url())

    return ctx
```

### 8.3 参考图解析（支持链式传递）

```python
def _resolve_reference_images(self, ctx: Context) -> list[str] | None:
    """解析 reference_image，支持三种来源（链式传递核心）。

    来源 1 - 直接 URL 字符串：
        reference_image: "https://example.com/photo.jpg"

    来源 2 - Context 变量引用（LLM 输出 / 用户输入）：
        reference_image: "{{character_url}}"

    来源 3 - 上游 ImageStep 链式传递（v2 新增）：
        reference_image: "{{prev_step.output_url}}"
        # output_url 是 ImageStep 自动写入的便捷字段，值为 to_url() 结果

    多图场景：
        reference_image:
          - "{{step_a.output_url}}"
          - "{{step_b.output_url}}"

    解析后对每个 URL 调用 validate_url 校验安全性（防 SSRF）。

    Returns:
        list[str] | None: 解析后的 URL 列表；无参考图时为 None。
    """
    if self.reference_image is None:
        return None

    from agentkit.image._http import validate_url

    # 统一为 list 处理
    raw = self.reference_image
    if isinstance(raw, str):
        raw = [raw]

    urls: list[str] = []
    for item in raw:
        # 模板解析：{{var}} → Context 值
        resolved = self._render_str(item, ctx)
        if not resolved:
            continue
        # 安全校验每个 URL
        validate_url(resolved)
        urls.append(resolved)

    return urls if urls else None
```

### 8.4 本地保存（v2 安全改进）

```python
async def _save_local(self, refs: list[ImageRef], ctx: Context) -> list[ImageRef]:
    """下载 URL 图片到本地，或保存 base64 到文件。

    v2 安全改进：
        1. output_dir 转绝对路径后校验是否在 workspace_root 内（防路径穿越）
        2. self.id 经正则清洗，移除 ../ 等危险字符
        3. URL 下载使用 _http.download() 流式写入 + 大小限制
        4. base64 解码后检查大小，防止超大 base64 撑爆内存
    """
    import base64 as b64mod
    import os
    import re

    from agentkit.image._http import download

    # 1. 解析并校验输出目录（防路径穿越）
    #    v3 修复：v2 用 startswith(workspace_root) 存在旁路——
    #    "/data/workspace-evil" 同样满足 startswith("/data/workspace")。
    #    改用 os.path.commonpath 做严格的路径包含判断。
    output_dir = self.output_dir or str(
        get_default("default_image_download_dir")
    )
    output_dir = os.path.abspath(output_dir)
    workspace_root = os.path.abspath(
        str(get_default("workspace_root", "."))
    )
    if os.path.commonpath([output_dir, workspace_root]) != workspace_root:
        raise ImageGenerationError(
            f"输出目录 {output_dir!r} 不在工作空间 {workspace_root!r} 内，"
            f"疑似路径穿越攻击",
            reason="path_traversal_blocked", retryable=False,
        )
    os.makedirs(output_dir, exist_ok=True)

    # 2. 清洗 step id（文件名安全）
    safe_id = re.sub(r'[^a-zA-Z0-9_.-]', '_', self.id or 'image')
    # 防止清洗后仍以 . 开头（隐藏文件 / 目录穿越）
    safe_id = safe_id.lstrip('.')

    max_download = int(get_default("image_max_download_size", 20 * 1024 * 1024))

    saved_refs = []
    for i, ref in enumerate(refs):
        ext = ".png" if "png" in ref.content_type else ".jpg"
        filename = f"{safe_id}_{i}{ext}"
        filepath = os.path.join(output_dir, filename)

        if ref.b64_json:
            # base64 → 解码后校验大小 → 写入文件
            data = b64mod.b64decode(ref.b64_json)
            if len(data) > max_download:
                raise ImageGenerationError(
                    f"base64 数据过大: {len(data)} > {max_download}",
                    reason="file_too_large", retryable=False,
                )
            with open(filepath, "wb") as f:
                f.write(data)
            size = len(data)

        elif ref.url:
            # URL → 流式下载（带 SSRF 校验 + 大小限制）
            size = await download(ref.url, filepath, max_size=max_download)

        else:
            saved_refs.append(ref)
            continue

        saved_refs.append(ImageRef(
            url=ref.url,
            b64_json=None,  # 清除大块数据
            local_path=filepath,
            content_type=ref.content_type,
            size=size,
            seed=ref.seed,
            finish_reason=ref.finish_reason,
        ))
    return saved_refs
```

### 8.5 trace 回填

```python
def _enrich_trace(self, trace: StepTrace) -> None:
    """回填图片生成信息到 trace（供可观测性查看）。"""
    trace.tool_calls = [{
        "type": "image_generation",
        "provider": self._last_provider,
        "model": self._last_model,
        "n_generated": self._last_n_generated,
    }]
```

---

## 9. 错误处理与重试（v2 核心改进）

### 9.1 三层错误处理机制

v1 把错误处理分成"执行级重试"和"钩子决策"两层，但 HTTP 传输层的异常分类完全留白。v2 补齐了传输层这一层，形成清晰的三层结构：

| 层级 | 机制 | 触发场景 | 实现位置 |
|------|------|----------|----------|
| **传输层映射** | httpx 异常 → `ImageGenerationError`（带 `retryable`） | 所有 HTTP 请求异常 | `image/_http.py`（新增） |
| **执行级重试** | `BaseStep.execute` 按 `RetryPolicy` 退避重试，**仅重试 retryable=True** | 网络超时、5xx、429 | `steps/base.py`（已有，见 9.2） |
| **钩子决策** | `on_step_error` 返回 SKIP / DEFAULT / RAISE | 内容安全拦截、余额不足 | `core/hooks.py`（已有） |

### 9.2 BaseStep.execute 改动（最小化）

v1 的 `BaseStep.execute` 对所有异常一视同仁地重试，这会导致"内容审核拦截"这种永久性失败被无谓重试——图片生成单次调用成本远高于文本 token。

改动方案：在执行级重试循环中，检查异常是否为 `ImageGenerationError` 且 `retryable=False`，若是则跳过剩余重试，直接进入钩子决策。

```python
# steps/base.py execute() 中的重试循环改动（仅新增 3 行检查）：

for attempt in range(max_attempts):
    try:
        await asyncio.wait_for(
            self.run(ctx), timeout=self.effective_timeout()
        )
        # ... 成功逻辑不变 ...
        break
    except asyncio.TimeoutError as e:
        last_error = e
    except Exception as e:
        last_error = e
        # v2 新增：永久错误不重试，直接进入钩子决策
        if hasattr(e, 'retryable') and not e.retryable:
            break

    # 退避重试逻辑不变 ...
```

这个改动的边界很清晰：

- 仅检查异常是否有 `retryable` 属性且为 `False`，不影响 LLMStep 等现有 Step（它们的异常没有这个属性，`hasattr` 返回 `False`，走原有逻辑）
- LLMStep 若未来需要类似优化，只需让 `OutputContractError` 携带 `retryable=False` 即可复用
- 改动量仅 3 行，不破坏现有重试语义

### 9.3 ImageGenerationError 分类表

`_http.py` 的异常映射确保每个 `ImageGenerationError` 都有准确的 `retryable` 标志：

| 错误场景 | reason | retryable | 典型 status_code |
|----------|--------|-----------|------------------|
| 请求超时 | `timeout` | True | - |
| 连接失败 | `connect_error` | True | - |
| 网络错误（DNS/重置） | `network_error` | True | - |
| 限流 | `rate_limited` | True | 429 |
| 服务端错误 | `server_error` | True | 5xx |
| 参数错误 | `bad_request` | False | 400 |
| 内容安全拦截 | `moderation_blocked` | False | 400 |
| 余额不足 | `insufficient_quota` | False | 400 |
| 鉴权失败 | `authentication_failed` | False | 401 |
| 权限拒绝 | `permission_denied` | False | 403 |
| 模型不存在 | `not_found` | False | 404 |
| URL 不安全 | `invalid_url` / `blocked_host` / `private_ip_blocked` | False | - |
| 文件过大 | `file_too_large` | False | - |
| 路径穿越 | `path_traversal_blocked` | False | - |

### 9.4 钩子差异化处理示例

```python
# 客户端统一抛出 ImageGenerationError，携带结构化信息
# （retryable=False 的错误不会被 execute 重试，直接到钩子）

async def on_step_error(self, step, ctx, error):
    if isinstance(error, ImageGenerationError):
        if error.reason == "moderation_blocked":
            return ErrorAction.SKIP    # 内容安全 → 跳过
        if error.reason == "insufficient_quota":
            return ErrorAction.DEFAULT  # 余额不足 → 填 None 继续
        if error.reason in ("invalid_url", "blocked_host", "path_traversal_blocked"):
            return ErrorAction.RAISE    # 安全错误 → 立即终止
    return ErrorAction.RAISE             # 其他 → 抛出
```

### 9.5 config.py 新增配置项

```python
# config.py 新增默认值（不修改已有配置）
_defaults["default_image_provider"] = "minimax"
_defaults["default_image_download_dir"] = "output/images"
_defaults["image_max_download_size"] = 20 * 1024 * 1024  # 20MB
_defaults["workspace_root"] = "."  # 工作空间根目录（路径穿越校验基准）
```

---

## 10. 图片链式传递设计

这是用户在审核反馈中提出的功能问题：是否支持图片生成 → 传递给下一个图片生成模型作为参考图，以及图片生成 → 传递给视觉模型作为输入。

答案是完全支持，且无需任何额外机制——复用现有的模板引擎即可。

### 10.1 设计核心：ImageRef.to\_url() + output\_url 便捷字段

`ImageStep.run()` 在写入主输出 `ctx.set(self.output, image_refs)` 的同时，自动写入一个便捷字段 `ctx.set(f"{self.output}_url", image_refs[0].to_url())`。下游 Step 通过 `{{prev_step.output_url}}` 即可引用，无需知道 ImageRef 的内部结构。

`ImageRef.to_url()` 按优先级返回可用的 URL：

1. `self.url`（API 返回的原始 URL，可直接被图生图 API 消费）
2. `self.local_path` 转 `file://` URI（本地文件，可被本地服务消费）
3. `self.b64_json` 转 `data URI`（base64 内嵌，通用但体积大）

这覆盖了三种链式传递场景。

### 10.2 场景一：Image → Image（图生图链式）

上游 ImageStep 生成图片，下游 ImageStep 把它作为参考图：

```yaml
steps:
  # 1. 生成角色立绘
  - id: gen_character
    type: image
    prompt: "一个赛博朋克风格的女性角色，全身立绘"
    provider: minimax
    aspect_ratio: "3:4"
    output: character_images
    # 自动写入 character_images_url = "https://..."

  # 2. 基于角色立绘生成场景图（图生图）
  - id: gen_scene
    type: image
    prompt: "把角色放在霓虹灯街道背景中，雨夜氛围"
    provider: minimax
    aspect_ratio: "16:9"
    reference_image: "{{gen_character.output_url}}"  # 链式引用
    output: scene_images
```

### 10.3 场景二：Image → Vision（视觉理解）

上游 ImageStep 生成图片，下游 LLMStep（多模态模型）把它作为输入：

```yaml
steps:
  # 1. 生成图片
  - id: gen_diagram
    type: image
    prompt: "画一个微服务架构图，包含 API 网关、订单服务、支付服务"
    provider: aihubmix
    output: diagram_images

  # 2. 视觉模型分析图片内容
  - id: analyze_diagram
    type: llm
    agent: vision_analyst
    prompt: |
      请分析这张架构图，指出潜在的单点故障：
      {{gen_diagram.output_url}}
    output: analysis
```

LLMStep 的多模态输入通过 `LLMMessage.content` 的 `list[dict]` 格式实现（已有能力），`{{gen_diagram.output_url}}` 会被模板引擎替换为图片 URL，LLM 客户端把它包装成 `{"type": "image_url", "image_url": {"url": "..."}}` content part。

### 10.4 场景三：多图参考（多步链式）

```yaml
steps:
  - id: gen_bg
    type: image
    prompt: "赛博朋克城市背景，远景"
    provider: minimax
    aspect_ratio: "16:9"
    output: bg_images

  - id: gen_char
    type: image
    prompt: "女性角色立绘，全身"
    provider: minimax
    aspect_ratio: "3:4"
    output: char_images

  # 同时引用两张图作为参考
  - id: compose
    type: image
    prompt: "把角色合成到背景中，保持角色风格一致"
    provider: minimax
    reference_image:
      - "{{gen_bg.output_url}}"
      - "{{gen_char.output_url}}"
    output: final_images
```

### 10.5 链式传递的数据流

```
ImageStep A                    ImageStep B
  │                              │
  ├─ ctx.set("images_a", [       ├─ reference_image: "{{images_a.output_url}}"
  │     ImageRef(url="https://...")  │
  │   ])                         │
  ├─ ctx.set("images_a_url",     │
  │     "https://...")     ←─────┤ _resolve_reference_images()
  │                              │   → _render_str("{{images_a.output_url}}", ctx)
  │                              │   → "https://..."
  │                              │   → validate_url("https://...")  ← SSRF 校验
  │                              │
  │                              ├─ ImageRequest(reference_images=["https://..."])
  │                              │
  │                              └─ client.generate(request)
```

链式传递复用现有模板引擎（`resolve_template` / `resolve_value`），零新增机制。`_resolve_reference_images` 方法解析 `{{var}}` 后对每个 URL 调用 `validate_url` 校验，确保上游 LLM 输出污染不会变成 SSRF 通道。

---

## 11. YAML 配置

### 11.1 基本文生图

```yaml
# 3 行即可生成图片
- id: generate_cover
  type: image
  prompt: "设计一张{{topic}}主题的封面图，风格：{{style}}"
  provider: minimax
  output: cover_images
```

### 11.2 完整参数

```yaml
- id: generate_hero
  type: image
  prompt: |
    A cinematic wide shot of a futuristic city at sunset,
    cyberpunk aesthetic, neon reflections on wet streets
  provider: aihubmix
  model: gpt-image-1
  n: 2
  size: "1536x1024"
  quality: high
  response_format: url
  save_local: true
  output_dir: ./output/images
  output: hero_images
  timeout: 120
  retry:
    backoff: exponential
    base_seconds: 3
    count: 2
```

### 11.3 图生图（参考图 + 链式传递）

```yaml
# 从 Context 变量取参考图（LLM 输出 / 用户输入 / 上游 ImageStep）
- id: edit_character
  type: image
  prompt: "把背景改为图书馆窗户前，看向远方"
  provider: minimax
  model: image-01
  aspect_ratio: "16:9"
  reference_image: "{{character_url}}"  # 从 Context 取变量
  output: edited_images
```

### 11.4 提供商配置（YAML providers 段）

```yaml
# 在 YAML 工作流顶部声明自定义图片提供商
# base_url 和 api_key_env 会经过安全校验
image_providers:
  my_custom:
    base_url: https://api.my-custom-provider.com/v1
    api_key_env: MY_CUSTOM_API_KEY  # 必须匹配 ^[A-Z][A-Z0-9_]*$
    model: my-image-model
    provider_type: openai

steps:
  - id: gen
    type: image
    prompt: "A beautiful sunset"
    provider: my_custom
    output: result
```

### 11.5 与 LLM Step 串联

```yaml
steps:
  # 1. LLM 生成图片描述
  - id: write_prompt
    type: llm
    agent: creative_writer
    prompt: "为以下主题写一段图片生成 prompt：{{topic}}"
    output: image_prompt

  # 2. Image Step 根据 LLM 输出生成图片
  - id: generate
    type: image
    prompt: "{{image_prompt}}"
    provider: minimax
    aspect_ratio: "16:9"
    output: generated_images
```

---

## 12. 扩展点

### 12.1 已内建的扩展能力

| 扩展场景 | 方式 | 改动量 |
|----------|------|--------|
| 新增图片服务商 | 继承 `ImageClient` + `register_image_client(type, cls)` 注册 | 1 个新文件 + 1 行注册 |
| 新增预设提供商 | 在 `PRESET_IMAGE_PROVIDERS` 添加条目 | 1 行 |
| 自定义错误处理 | 重写 `on_step_error` 钩子 | 0 行框架改动 |
| 自定义本地保存 | 子类化 `ImageStep` 重写 `_save_local` | 0 行框架改动 |
| 提供商特有参数 | `extra` 字典透传 | 0 行框架改动 |
| 图片链式传递 | `{{step_id.output_url}}` | 0 行框架改动 |

### 12.2 未来扩展方向（不在本次范围）

- **AIMIXHUB Predictions 端点**：支持 Imagen / Qwen / Doubao 等模型（新增 `AihubmixPredictionsClient`，通过 `_http.post_json` + 轮询实现）
- **图片生成钩子**：在 `LifecycleHooks` 添加 `on_image_call` 回调（no-op 默认实现，不破坏现有 hooks）
- **异步任务轮询**：Flux 系列的异步两步请求（提交 + 轮询，复用 `_http.post_json` + `retryable=True` 的退避轮询）
- **流式进度**：部分 API 支持生成进度回调
- **ArtifactStore 集成**：在可视化运行时将图片写入 ArtifactStore 并发布事件

---

## 13. 实现计划

| 阶段 | 文件 | 内容 | 依赖 |
|------|------|------|------|
| **P1: 核心抽象** | `image/base.py` | ImageClient ABC + 数据类 + ImageGenerationError（含 retryable） | 无（仅标准库） |
| **P2: 安全传输层** | `image/_http.py` | URL 校验 + HTTP 请求 + 流式下载 + 异常映射 | P1 |
| **P3: Provider 系统** | `image/provider.py` + `image/__init__.py` | ImageProvider（含安全校验）+ 注册表 + 预设 + 工厂 | P1, P2 |
| **P4: 客户端实现** | `image/openai.py` + `image/minimax.py` + `image/mock.py` | 三个客户端（通过 `_http` 发请求） | P1, P2 |
| **P5: ImageStep** | `steps/image_step.py` + 修改 `steps/__init__.py` + 修改 `steps/base.py`（3 行） | ImageStep + 注册 + execute retryable 检查 | P1, P2, P3, P4 |
| **P6: 配置与集成** | 修改 `config.py` + `yaml/loader.py` | 新增 config 默认值 + YAML image\_providers 段加载 | P5 |
| **P7: 测试** | `tests/image/` | 单元测试 + 集成测试（MockImageClient + SSRF 路径穿越测试） | P5 |
| **P8: 示例** | `examples/image_generation.yaml` | 完整 YAML 示例（含链式传递） | P6 |

### 改动汇总

| 操作 | 文件 | 改动量 |
|------|------|--------|
| 新增 | `image/base.py` | ~140 行 |
| 新增 | `image/_http.py` | ~220 行 |
| 新增 | `image/provider.py` | ~210 行 |
| 新增 | `image/__init__.py` | ~80 行 |
| 新增 | `image/openai.py` | ~130 行 |
| 新增 | `image/minimax.py` | ~140 行 |
| 新增 | `image/mock.py` | ~40 行 |
| 新增 | `steps/image_step.py` | ~280 行 |
| 修改 | `steps/__init__.py` | +1 行 import |
| 修改 | `steps/base.py` | +3 行（retryable 检查） |
| 修改 | `config.py` | +4 行默认值 |
| 修改 | `yaml/loader.py` | ~20 行（image\_providers 段加载） |
| **总计** | **12 个文件** | **~1240 行新增 / ~28 行修改** |

相比 v1，v2 新增了 `image/_http.py`（~220 行）用于集中处理安全与传输，`steps/base.py` 增加 3 行 retryable 检查，总改动量增加约 25%，但换来了完整的安全防护与清晰的错误分类边界。

---

## 附录：v1 → v2 问题修复对照

### 问题 1：Provider 零校验 → 密钥泄露 / SSRF

| v1 问题 | v2 修复 | v3 修复 |
|---------|---------|---------|
| `api_key_env` 任意字符串，可读取 `AWS_SECRET_ACCESS_KEY` | `validate_api_key_env()` 正则校验 `^[A-Z][A-Z0-9_]*$`，在 `ImageProvider.__post_init__` 中执行 | — |
| `base_url` 任意字符串，可指向内网/元数据接口 | `validate_url()` 校验 scheme + 拦截 `169.254.169.254` 等元数据主机 + 拦截私网 IP | v2 仅校验字面量 IP，域名跳过检查；v3 新增 DNS 解析 + `_SSRFSafeTransport` 双层防护，堵死 DNS rebinding |

### 问题 2：路径穿越 + 下载无限制

| v1 问题 | v2 修复 | v3 修复 |
|---------|---------|---------|
| `output_dir` 无路径穿越校验 | `os.path.abspath` 后校验 `startswith(workspace_root)` | `startswith` 存在旁路（`/workspace-evil` 满足 `/workspace` 前缀），改用 `os.path.commonpath` 严格判断 |
| `self.id` 可含 `../../../` | 正则 `[^a-zA-Z0-9_.-]` → `_` 清洗 + `lstrip('.')` | — |
| `_download()` 未定义，疑似整段读入内存 | `_http.download()` 流式分块写入（8KB）+ Content-Length 预检 + 累计字节数上限 | — |
| reference\_image URL 可被上游 LLM 污染 → SSRF | `_resolve_reference_images` 对每个解析后的 URL 调用 `validate_url` | — |

### 问题 3：错误分类边界不清 + HTTP 层未设计

| v1 问题 | v2 修复 | v3 修复 |
|---------|---------|---------|
| `_post()` / `_download()` 从未定义 | 新增 `image/_http.py`，`post_json()` + `download()` 集中实现 | — |
| `ImageGenerationError` 无 retryable 标志 | 新增 `retryable: bool` 字段，由 `_http.py` 异常映射自动设置 | — |
| execute 对所有异常一视同仁重试，永久错误浪费成本 | `BaseStep.execute` 新增 3 行：`if hasattr(e, 'retryable') and not e.retryable: break` | — |
| HTTP 传输层异常与自定义异常边界不清 | `_http.py` 把 httpx 所有异常统一映射为 `ImageGenerationError`，客户端实现只需处理 `ImageGenerationError` | — |
| —（v2 新引入）每次请求新建 `AsyncClient`，无连接复用 | — | v3 改为模块级共享 `_shared_client`，`max_connections=20` + `max_keepalive_connections=10` |
| —（v2 新引入）无并发/限流，重试风暴风险 | — | v3 新增 per-provider `asyncio.Semaphore`，`post_json` 默认限 5 并发 |

---

## 附录：v2 → v3 问题修复对照

### 问题 1：SSRF 防护对域名完全失效（DNS rebinding）

| v2 问题 | v3 修复 |
|---------|---------|
| `_check_private_ip` 对非 IP 格式的 hostname 直接 `return`，域名跳过私网检查 | `validate_url` 新增 DNS 解析：对所有 A/AAAA 记录做 `_check_ip` 校验 |
| 攻击者用自有域名指向 `127.0.0.1` / `169.254.169.254` 即可绕过 | 新增 `_SSRFSafeTransport(httpx.AsyncBaseTransport)`：在实际 TCP 连接前异步解析 DNS 并二次校验，将 TOCTOU 窗口压缩到微秒级 |
| 文档 10.5 节声称"已修复 SSRF"但仅覆盖字面量 IP | 双层防护：`validate_url`（静态校验）+ `_SSRFSafeTransport`（连接时校验），域名场景不再被跳过 |

### 问题 2：路径穿越校验用字符串前缀匹配，存在旁路

| v2 问题 | v3 修复 |
|---------|---------|
| `output_dir.startswith(workspace_root)` 可被 `/data/workspace-evil` 等同前缀路径绕过 | 改用 `os.path.commonpath([output_dir, workspace_root]) != workspace_root`，做严格的路径包含判断 |
| 防护代码本身造成"已做校验"的错觉 | 旁路已堵死，`commonpath` 按 path component 级别比较 |

### 问题 3：声称的"连接池复用"与实现矛盾 + 无并发/限流控制

| v2 问题 | v3 修复 |
|---------|---------|
| `post_json` / `download` 每次 `async with httpx.AsyncClient()` 新建连接，每次重做 TCP+TLS 握手 | 模块级共享 `_shared_client: httpx.AsyncClient`，`max_connections=20` + `max_keepalive_connections=10` |
| 文档声称"连接池复用"但代码每次销毁 | 共享 client 由 `_get_shared_client()` 懒初始化，所有请求复用同一连接池 |
| 高并发 fan-out + 自动重试叠加 → 重试风暴 | `post_json` 新增 `max_concurrency` 参数 + per-provider `asyncio.Semaphore`，默认限 5 并发 |

### 问题 4：create_image_client 工厂不是真正的可扩展注册机制

| v2 问题 | v3 修复 |
|---------|---------|
| `if provider_type == "minimax": ... else: OpenAIImageClient` 硬编码 if/else | `_CLIENT_REGISTRY: dict[str, type[ImageClient]]` + `register_image_client()` 注册函数 |
| `else` 兜底分支：未知 provider_type 静默当 OpenAI 处理 | `_CLIENT_REGISTRY.get()` 查找失败时显式抛 `ValueError`，列出已注册类型 |
| `provider_type` 无 `__post_init__` 校验，拼写错误延迟到运行时 | `ImageProvider.__post_init__` 新增 `provider_type` 合法性校验，注册时即报错 |
| 新增非 OpenAI/MiniMax 协议服务商须改工厂函数 | `register_image_client("stability", StabilityClient)` 一行注册，工厂函数零改动 |
