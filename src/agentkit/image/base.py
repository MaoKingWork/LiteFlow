"""image.base —— 图片生成客户端抽象基类与核心数据类。

本模块定义 AgentKit 与图片生成服务交互的统一抽象层，是整个 ``image``
子包的基石。所有具体的图片生成提供商实现（如 ``openai.OpenAIImageClient``、
``minimax.MiniMaxImageClient``、``mock.MockImageClient``）均继承自
``ImageClient`` 并实现 ``generate`` 方法。

设计原则（与 ``llm/base.py`` 完全对称）：
    - 高度模块化：仅依赖 Python 标准库（abc / dataclasses / typing），
      不引入 httpx 等第三方依赖，可被任意子模块安全导入。
    - 无循环依赖：本模块不依赖 agentkit 内任何其他子模块。
    - 可拓展：新增图片服务商只需继承 ``ImageClient`` 并实现 ``generate``。
    - 类型注解完整，便于 IDE 静态检查与自动补全。

数据模型说明：
    - ``ImageRequest``:       图片生成请求（frozen，防止运行期篡改）
    - ``GeneratedImage``:     单张生成图片的结果
    - ``ImageResponse``:      图片生成响应
    - ``ImageRef``:           写入 Context 的图片引用（最终输出，支持链式传递）
    - ``ImageGenerationError``: 图片生成失败的统一异常（含 retryable 标志位）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# ImageRequest —— 图片生成请求（frozen，防止运行期篡改）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ImageRequest:
    """图片生成请求。

    统一描述文生图与图生图的参数，各客户端实现负责映射到具体 API 格式。

    Attributes:
        prompt:            图像描述文本（必填）。
        model:             模型名。为空时由客户端使用其默认模型。
        n:                 生成数量，默认 1。
        size:              图片尺寸，如 ``"1024x1024"``。None 表示由 API 决定。
        aspect_ratio:      宽高比，如 ``"16:9"``。部分 API（MiniMax）使用此字段而非 size。
        seed:              随机种子。None 表示随机。
        response_format:   返回格式：``"url"`` | ``"base64"``。默认 ``"url"``。
        quality:           渲染质量：``"low"`` | ``"medium"`` | ``"high"`` | None。
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


# ---------------------------------------------------------------------------
# GeneratedImage —— 单张生成图片
# ---------------------------------------------------------------------------
@dataclass
class GeneratedImage:
    """单张生成图片的结果。

    ``url`` 和 ``b64_json`` 通常二选一（由 ``response_format`` 决定）。

    Attributes:
        url:            图片 URL（可能有过期时间）。
        b64_json:       Base64 编码的图片数据。
        content_type:   MIME 类型，如 ``"image/png"``。
        seed:           生成时使用的种子。
        finish_reason:  结束原因：``"success"`` | ``"content_filtered"`` 等。
    """

    url: str | None = None
    b64_json: str | None = None
    content_type: str = "image/png"
    seed: int | None = None
    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# ImageResponse —— 生成响应
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# ImageRef —— 写入 Context 的图片引用（最终输出）
# ---------------------------------------------------------------------------
@dataclass
class ImageRef:
    """图片引用（写入 Context 供下游 Step 使用）。

    设计为可序列化的轻量结构，避免在 Context 中存储大块 base64。
    当 ``save_local=True`` 时，``local_path`` 指向本地文件，``b64_json`` 被清除。

    链式传递：下游 Step 通过 ``reference_image: "{{prev_step.output_url}}"``
    即可引用本 Step 生成的图片。``to_url()`` 方法按优先级返回可用的 URL：
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
            1. ``self.url``（API 返回的原始 URL，可直接被图生图 API 消费）
            2. ``self.local_path`` 转 ``file://`` URI（本地文件，可被本地服务消费）
            3. ``self.b64_json`` 转 data URI（base64 内嵌，通用但体积大）

        Returns:
            str | None: 图片 URL；无任何可用数据时返回 None。
        """
        if self.url:
            return self.url
        if self.local_path:
            import os
            import pathlib

            # 先转绝对路径，避免 Windows 上相对路径调 as_uri() 抛 ValueError
            abs_path = os.path.abspath(self.local_path)
            return pathlib.Path(abs_path).as_uri()
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


# ---------------------------------------------------------------------------
# ImageGenerationError —— 图片生成失败的统一异常（含 retryable 标志位）
# ---------------------------------------------------------------------------
class ImageGenerationError(Exception):
    """图片生成失败的统一异常。

    ``retryable`` 标志位让 ``BaseStep.execute`` 的重试逻辑能区分"瞬时错误
    （值得重试）"与"永久错误（重试只会浪费钱和延迟）"。

    分类规则（由 ``_http.py`` 的异常映射自动设置）：
        - ``retryable=True``:  网络超时、连接重置、429 限流、5xx 服务端错误。
        - ``retryable=False``: 400 参数错误、401 鉴权失败、403 内容安全拦截、
                               余额不足、模型不存在等。

    图片生成单次调用成本远高于文本 token，对永久错误的重试是实打实的
    金钱与延迟浪费。``retryable`` 标志位让重试策略精准命中瞬时错误。

    Attributes:
        provider:    提供商名。
        status_code: API 返回的 HTTP 状态码（如有）。
        reason:      失败原因简述（如 ``"moderation_blocked"`` / ``"timeout"``）。
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


# ---------------------------------------------------------------------------
# ImageClient —— 图片生成客户端抽象基类
# ---------------------------------------------------------------------------
class ImageClient(ABC):
    """图片生成客户端抽象基类。

    所有图片生成服务商客户端必须继承本类并实现 ``generate`` 方法。
    ``ImageStep`` 通过本接口与具体提供商解耦。

    实现方约定：
        - ``generate`` 为协程，支持异步并发调用。
        - 返回 ``ImageResponse``，其中 ``raw`` 保留原始响应便于调试。
        - HTTP 传输层异常由 ``_http.py`` 统一映射为 ``ImageGenerationError``，
          实现方只需调用 ``_http.post_json()`` / ``_http.download()``，无需自行
          处理 httpx 异常。
        - 客户端实例可复用（httpx 连接池由 ``_http.py`` 管理）。
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


__all__ = [
    "ImageRequest",
    "GeneratedImage",
    "ImageResponse",
    "ImageRef",
    "ImageGenerationError",
    "ImageClient",
]
