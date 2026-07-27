"""image.openai —— OpenAI 兼容图片生成客户端实现。

本模块提供 ``OpenAIImageClient``，通过 ``_http.post_json`` 异步调用
OpenAI 兼容的 Images API（``POST {base_url}/images/generations``），
适用于 AIMIXHUB (gpt-image-1)、StepFun (step-1x-medium) 以及 OpenAI 官方
等兼容 OpenAI Images API 规范的服务商。

设计原则（与 ``llm/openai.py`` 对称）：
    - 高度模块化：仅依赖 ``httpx``（经 ``_http.py``）与本包 ``image.base``。
    - 安全传输：所有 HTTP 请求经 ``_http.post_json``，SSRF / 异常映射由传输层统一处理。
    - 可拓展：新增非 OpenAI 兼容服务商可参考本实现继承 ``ImageClient``。

请求映射：
    ``ImageRequest`` → OpenAI images.generate payload
    - ``prompt``          → ``prompt``
    - ``model``           → ``model``
    - ``n``               → ``n``
    - ``size``            → ``size``
    - ``quality``         → ``quality``
    - ``response_format`` → ``response_format`` (``url`` / ``b64_json``)
    - ``seed``            → 透传到请求体
    - ``reference_images`` → 切换到 ``/images/edits`` 端点（图生图）
    - ``extra``           → 合并到请求体
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


class OpenAIImageClient(ImageClient):
    """OpenAI 兼容图片生成客户端。

    适用于 AIMIXHUB (gpt-image-1)、StepFun (step-1x-medium) 等
    兼容 OpenAI ``/v1/images/generations`` 接口的服务商。

    所有 HTTP 请求经 ``_http.post_json`` 发起，异常由传输层统一映射为
    ``ImageGenerationError``（含 ``retryable`` 标志位）。

    Args:
        api_key:  API Key。为 None 时调用方应确保从环境变量读取后传入。
        base_url: API 根地址（如 ``"https://aihubmix.com/v1"``）。
        model:    默认模型名。``ImageRequest.model`` 为空时使用。

    用法示例::

        client = OpenAIImageClient(
            api_key="sk-...",
            base_url="https://aihubmix.com/v1",
            model="gpt-image-1",
        )
        resp = await client.generate(ImageRequest(prompt="A cat"))
        print(resp.images[0].url)
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        model: str = "",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def generate(self, request: ImageRequest) -> ImageResponse:
        """调用 OpenAI 兼容图片生成 API。

        文生图走 ``/images/generations``，图生图（有 reference_images）走
        ``/images/edits``。

        Args:
            request: 图片生成请求。

        Returns:
            ImageResponse: 生成结果。

        Raises:
            ImageGenerationError: 生成失败（由 ``_http.post_json`` 映射）。
        """
        # 校验 api_key
        if not self._api_key:
            raise ImageGenerationError(
                "OpenAIImageClient 缺少 api_key：未显式传入且环境变量未设置",
                provider="openai", reason="missing_api_key", retryable=False,
            )

        payload = self._build_payload(request)

        # 选择端点：文生图 /images/generations，图生图 /images/edits
        if request.reference_images:
            url = f"{self._base_url}/images/edits"
            return await self._post_edit(url, payload, request)
        url = f"{self._base_url}/images/generations"
        return await self._post_generate(url, payload, request)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        """构造请求头。"""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, request: ImageRequest) -> dict[str, Any]:
        """将 ``ImageRequest`` 映射为 OpenAI 兼容 payload。

        Args:
            request: 图片生成请求。

        Returns:
            dict: OpenAI Images API 请求体。
        """
        payload: dict[str, Any] = {
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
        # 合并提供商特有参数（透传，不解析）
        payload.update(request.extra)
        return payload

    async def _post_generate(
        self, url: str, payload: dict[str, Any]
    ) -> ImageResponse:
        """文生图：POST JSON → 解析响应。

        Args:
            url:     ``/images/generations`` 端点 URL。
            payload: 请求体。

        Returns:
            ImageResponse: 生成结果。
        """
        data = await post_json(
            url, payload,
            headers=self._headers(), provider="openai",
        )

        images: list[GeneratedImage] = []
        for item in data.get("data", []):
            images.append(GeneratedImage(
                url=item.get("url"),
                b64_json=item.get("b64_json"),
                seed=item.get("seed"),
                finish_reason=item.get("finish_reason"),
            ))
        return ImageResponse(
            images=images,
            model=data.get("model", ""),
            created=data.get("created"),
            raw=data,
            usage=data.get("usage"),
        )

    async def _post_edit(
        self,
        url: str,
        payload: dict[str, Any],
        request: ImageRequest,
    ) -> ImageResponse:
        """图生图：POST JSON（含参考图 URL）→ 解析响应。

        部分兼容 API 接受 JSON 中的参考图 URL 字段。对于严格要求 multipart
        上传的 API，子类可重写此方法改用 ``multipart/form-data``。

        Args:
            url:     ``/images/edits`` 端点 URL。
            payload: 请求体（已含基础字段）。
            request: 原始请求（取 reference_images）。

        Returns:
            ImageResponse: 生成结果。
        """
        # 把参考图 URL 列表加入 payload
        payload["image"] = request.reference_images

        data = await post_json(
            url, payload,
            headers=self._headers(), provider="openai",
        )

        images: list[GeneratedImage] = []
        for item in data.get("data", []):
            images.append(GeneratedImage(
                url=item.get("url"),
                b64_json=item.get("b64_json"),
                seed=item.get("seed"),
                finish_reason=item.get("finish_reason"),
            ))
        return ImageResponse(
            images=images,
            model=data.get("model", ""),
            created=data.get("created"),
            raw=data,
            usage=data.get("usage"),
        )


__all__ = ["OpenAIImageClient"]
