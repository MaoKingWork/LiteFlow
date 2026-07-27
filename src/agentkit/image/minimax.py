"""image.minimax —— MiniMax 图片生成客户端实现。

本模块提供 ``MiniMaxImageClient``，通过 ``_http.post_json`` 异步调用
MiniMax 原生图片生成 API（``POST {base_url}/v1/image_generation``），
适用于 MiniMax image-01 / image-01-live 等模型。

设计原则（与 ``llm/openai.py`` 对称）：
    - 高度模块化：仅依赖 ``httpx``（经 ``_http.py``）与本包 ``image.base``。
    - 安全传输：所有 HTTP 请求经 ``_http.post_json``，SSRF / 异常映射由传输层统一处理。
    - 原生 API：使用 MiniMax 自有端点与响应格式（非 OpenAI 兼容）。

请求映射：
    ``ImageRequest`` → MiniMax image_generation payload
    - ``prompt``          → ``prompt``
    - ``model``           → ``model``
    - ``n``               → ``n``（若 API 要求可经 extra 覆盖）
    - ``aspect_ratio``    → ``aspect_ratio``
    - ``seed``            → ``seed``
    - ``response_format`` → ``response_format``
    - ``reference_images`` → ``subject_reference``（图生图）
    - ``extra``           → 合并到请求体

响应解析：
    MiniMax 响应包含 ``base_resp.status_code``（0=成功）和
    ``data.image_urls`` / ``data.image_base64``。
"""

from __future__ import annotations

from typing import Any

from agentkit.image._http import post_json
from agentkit.image.base import (
    GeneratedImage,
    ImageClient,
    ImageGenerationError,
    ImageRequest,
    ImageResponse,
)


class MiniMaxImageClient(ImageClient):
    """MiniMax 图片生成客户端。

    适用于 MiniMax image-01 / image-01-live 模型。
    使用 MiniMax 原生 ``/v1/image_generation`` 端点（非 OpenAI 兼容）。

    所有 HTTP 请求经 ``_http.post_json`` 发起，异常由传输层统一映射为
    ``ImageGenerationError``（含 ``retryable`` 标志位）。

    Args:
        api_key:  API Key。为 None 时调用方应确保从环境变量读取后传入。
        base_url: API 根地址（如 ``"https://api.minimaxi.com"``）。
        model:    默认模型名。``ImageRequest.model`` 为空时使用。

    用法示例::

        client = MiniMaxImageClient(
            api_key="...",
            base_url="https://api.minimaxi.com",
            model="image-01",
        )
        resp = await client.generate(ImageRequest(
            prompt="A cat", aspect_ratio="16:9",
        ))
        print(resp.images[0].url)
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.minimaxi.com",
        model: str = "",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def generate(self, request: ImageRequest) -> ImageResponse:
        """调用 MiniMax 图片生成 API。

        Args:
            request: 图片生成请求。

        Returns:
            ImageResponse: 生成结果。

        Raises:
            ImageGenerationError: 生成失败（由 ``_http.post_json`` 映射或
                                  MiniMax 业务错误码触发）。
        """
        # 校验 api_key
        if not self._api_key:
            raise ImageGenerationError(
                "MiniMaxImageClient 缺少 api_key：未显式传入且环境变量未设置",
                provider="minimax", reason="missing_api_key", retryable=False,
            )

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
            # MiniMax 业务错误码：限流类视为可重试，其他为永久错误
            retryable = status_code in (1007, 1029)
            raise ImageGenerationError(
                f"MiniMax API 错误: {status_msg}",
                provider="minimax", status_code=status_code,
                reason=status_msg, retryable=retryable,
            )

        # 解析响应
        data = resp.get("data", {})
        images: list[GeneratedImage] = []
        for img_url in data.get("image_urls", []):
            images.append(GeneratedImage(url=img_url))
        for b64 in data.get("image_base64", []):
            images.append(GeneratedImage(b64_json=b64))

        return ImageResponse(
            images=images,
            model=request.model or self._model,
            raw=resp,
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _build_payload(self, request: ImageRequest) -> dict[str, Any]:
        """将 ``ImageRequest`` 映射为 MiniMax payload。

        Args:
            request: 图片生成请求。

        Returns:
            dict: MiniMax image_generation 请求体。
        """
        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "prompt": request.prompt,
        }
        if request.n > 1:
            payload["n"] = request.n
        if request.aspect_ratio:
            payload["aspect_ratio"] = request.aspect_ratio
        if request.seed is not None:
            payload["seed"] = request.seed
        payload["response_format"] = request.response_format
        # 图生图：MiniMax 使用 subject_reference 字段
        if request.reference_images:
            payload["subject_reference"] = [
                {"type": "character", "image_file": url}
                for url in request.reference_images
            ]
        # 合并提供商特有参数（透传，不解析）
        payload.update(request.extra)
        return payload


__all__ = ["MiniMaxImageClient"]
