"""llm.media —— 多模态 content part 构造助手(可选工具)。

本模块提供一组纯函数,用于构造 OpenAI / MiMo 兼容的多模态 content part dict。
用户也可直接手写 dict,本模块不参与核心链路,仅降低重复代码。

返回值均为原生 dict(如 ``{"type": "image_url", "image_url": {"url": ...}}``),
与 OpenAI SDK / LangChain / LiteLLM 原生格式一致,可直接放入
``LLMMessage.content`` 的 list 中。

MiMo 多模态限制(来自 API 文档):
    - 图片: JPEG/PNG/GIF/WebP/BMP,base64 50MB,URL 50MB
    - 音频: MP3/WAV/FLAC/M4A/OGG,base64 50MB,URL 100MB
    - 视频: MP4/MOV/AVI/WMV,base64 50MB,URL 300MB
    - 视频 fps: 0.1–10,默认 2。``None`` 表示不传(用 API 默认)。
    - media_resolution: "default" | "max",``None`` 表示不传。

公开 API:
    - text:               构造文本 part
    - image_url:          构造图片 URL part
    - image_base64:       从文件构造图片 base64 part
    - audio_url:          构造音频 URL part
    - audio_base64:       从文件构造音频 base64 part
    - video_url:          构造视频 URL part(可带 fps / media_resolution)
    - video_base64:       从文件构造视频 base64 part
    - estimate_image_tokens: 图片 token 估算
    - estimate_video_tokens: 视频 token 估算
    - estimate_audio_tokens: 音频 token 估算
"""

from __future__ import annotations

import base64 as _b64
import os
from typing import Any

__all__ = [
    "text",
    "image_url",
    "image_base64",
    "audio_url",
    "audio_base64",
    "video_url",
    "video_base64",
    "estimate_image_tokens",
    "estimate_video_tokens",
    "estimate_audio_tokens",
]

# MiMo base64 大小限制(字节)
_MAX_BASE64_BYTES = 50 * 1024 * 1024  # 50MB


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _read_and_encode(path: str, mime: str | None = None) -> tuple[str, str]:
    """读取文件并编码为 base64 data URI。

    Args:
        path: 本地文件路径。
        mime: MIME 类型;``None`` 时从扩展名推断。

    Returns:
        tuple[str, str]: (data_uri, detected_mime)

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件超过 base64 大小限制。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"文件不存在: {path}")

    size = os.path.getsize(path)
    if size > _MAX_BASE64_BYTES:
        raise ValueError(
            f"文件大小 {size / 1024 / 1024:.1f}MB 超过 base64 限制 "
            f"{_MAX_BASE64_BYTES / 1024 / 1024:.0f}MB: {path}"
        )

    if mime is None:
        mime = _guess_mime(path)

    with open(path, "rb") as f:
        data = f.read()
    encoded = _b64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}", mime


_MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".m4a": "audio/mp4", ".ogg": "audio/ogg",
    ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv",
}


def _guess_mime(path: str) -> str:
    """从文件扩展名推断 MIME 类型。"""
    ext = os.path.splitext(path)[1].lower()
    return _MIME_MAP.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# 文本 part
# ---------------------------------------------------------------------------
def text(text: str) -> dict[str, Any]:
    """构造文本 content part。

    Args:
        text: 文本内容。

    Returns:
        dict: ``{"type": "text", "text": ...}``
    """
    return {"type": "text", "text": text}


# ---------------------------------------------------------------------------
# 图片 part
# ---------------------------------------------------------------------------
def image_url(url: str) -> dict[str, Any]:
    """构造图片 URL content part。

    Args:
        url: 图片 URL 或 data URI。

    Returns:
        dict: ``{"type": "image_url", "image_url": {"url": ...}}``
    """
    return {"type": "image_url", "image_url": {"url": url}}


def image_base64(path: str, mime: str | None = None) -> dict[str, Any]:
    """从本地文件构造图片 base64 content part。

    Args:
        path: 本地图片文件路径。
        mime: MIME 类型;``None`` 时从扩展名推断。

    Returns:
        dict: ``{"type": "image_url", "image_url": {"url": "data:...;base64,..."}}``

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件超过 50MB 限制。
    """
    data_uri, _ = _read_and_encode(path, mime)
    return image_url(data_uri)


# ---------------------------------------------------------------------------
# 音频 part
# ---------------------------------------------------------------------------
def audio_url(url: str) -> dict[str, Any]:
    """构造音频 URL content part。

    Args:
        url: 音频 URL。

    Returns:
        dict: ``{"type": "input_audio", "input_audio": {"data": url}}``
    """
    return {"type": "input_audio", "input_audio": {"data": url}}


def audio_base64(path: str, mime: str | None = None) -> dict[str, Any]:
    """从本地文件构造音频 base64 content part。

    Args:
        path: 本地音频文件路径。
        mime: MIME 类型;``None`` 时从扩展名推断。

    Returns:
        dict: ``{"type": "input_audio", "input_audio": {"data": "data:...;base64,..."}}``

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件超过 50MB 限制。
    """
    data_uri, _ = _read_and_encode(path, mime)
    return audio_url(data_uri)


# ---------------------------------------------------------------------------
# 视频 part
# ---------------------------------------------------------------------------
def video_url(
    url: str,
    fps: float | None = None,
    media_resolution: str | None = None,
) -> dict[str, Any]:
    """构造视频 URL content part。

    ``fps`` 与 ``media_resolution`` 为 ``None`` 时不传该键,由 API 使用默认值
    (fps=2, media_resolution="default")。提高 ``fps`` 会显著增加 token 与费用。

    Args:
        url:             视频 URL。
        fps:             采样帧率(0.1–10)。``None`` = API 默认。
        media_resolution: 分辨率模式("default" | "max")。``None`` = API 默认。

    Returns:
        dict: ``{"type": "video_url", "video_url": {"url": ...}, ...}``
    """
    part: dict[str, Any] = {"type": "video_url", "video_url": {"url": url}}
    if fps is not None:
        part["fps"] = fps
    if media_resolution is not None:
        part["media_resolution"] = media_resolution
    return part


def video_base64(
    path: str,
    mime: str | None = None,
    fps: float | None = None,
    media_resolution: str | None = None,
) -> dict[str, Any]:
    """从本地文件构造视频 base64 content part。

    Args:
        path:             本地视频文件路径。
        mime:             MIME 类型;``None`` 时从扩展名推断。
        fps:              采样帧率(0.1–10)。``None`` = API 默认。
        media_resolution: 分辨率模式("default" | "max")。``None`` = API 默认。

    Returns:
        dict: 视频 content part dict。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件超过 50MB 限制。
    """
    data_uri, _ = _read_and_encode(path, mime)
    return video_url(data_uri, fps=fps, media_resolution=media_resolution)


# ---------------------------------------------------------------------------
# Token 估算(基于 MiMo 官方文档公式)
# ---------------------------------------------------------------------------
def estimate_image_tokens(width: int, height: int, detail: str = "default") -> int:
    """估算图片消耗的 token 数。

    基于 MiMo 官方文档公式:
        - detail=default: ``ceil(width/512) * ceil(height/512) * 120 + 85``
        - detail=max:     按更高分辨率分块计算,近似为 default 的 2-4 倍。

    Args:
        width:  图片宽度(像素)。
        height: 图片高度(像素)。
        detail: 分辨率模式:"default" | "max"。

    Returns:
        int: 估算 token 数。
    """
    import math

    tiles = math.ceil(width / 512) * math.ceil(height / 512)
    base = tiles * 120 + 85
    if detail == "max":
        return base * 2  # 近似:max 模式分块更多
    return base


def estimate_video_tokens(duration_seconds: float, fps: float = 2.0) -> int:
    """估算视频消耗的 token 数。

    基于 MiMo 官方文档:每帧约 320 token(近似值)。

    Args:
        duration_seconds: 视频时长(秒)。
        fps:              采样帧率。

    Returns:
        int: 估算 token 数。
    """
    frames = int(duration_seconds * fps)
    return frames * 320 + 85  # +85 基础开销


def estimate_audio_tokens(duration_seconds: float) -> int:
    """估算音频消耗的 token 数。

    基于 MiMo 官方文档:每秒约 25 token(近似值)。

    Args:
        duration_seconds: 音频时长(秒)。

    Returns:
        int: 估算 token 数。
    """
    return int(duration_seconds * 25) + 85
