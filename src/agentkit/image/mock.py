"""image.mock —— 测试用 Mock 图片生成客户端，不消耗资源不发网络请求。

本模块提供 ``MockImageClient``，用于在单元测试 / 本地调试中替代真实图片
生成客户端。返回固定的占位图片，用法与 ``llm.mock.MockClient`` 对称。

设计原则：
    - 零网络零成本：所有调用纯内存完成，可在 CI 中无密钥运行。
    - 可断言：``history`` 完整记录每次 ``generate`` 的入参，便于测试断言。
    - 可组合：``add_images`` 支持运行时追加预设响应，便于动态测试场景。
    - 仅依赖 ``image.base``，无循环依赖。
"""

from __future__ import annotations

from typing import Any

from agentkit.image.base import (
    GeneratedImage,
    ImageClient,
    ImageRequest,
    ImageResponse,
)


class MockImageClient(ImageClient):
    """测试用 Mock 图片生成客户端。

    按预设响应序列依次返回 ``generate`` 调用结果，并将每次调用入参记录到
    ``history``，便于在测试中精确断言 ``ImageStep`` 等组件的请求构造是否正确。

    Args:
        responses:    预设的 ``ImageResponse`` 序列。按 ``generate`` 调用顺序
                      返回；耗尽后再调用抛 ``RuntimeError``。
        call_count:   初始调用计数，默认 0。
        default_url:  无预设响应时生成的占位 URL 模板（含 ``{i}`` 占位符）。

    用法示例::

        # 预设响应
        mc = MockImageClient(responses=[
            ImageResponse(images=[GeneratedImage(url="https://example.com/1.png")]),
        ])
        resp = await mc.generate(ImageRequest(prompt="A cat"))
        assert resp.images[0].url == "https://example.com/1.png"
        assert mc.call_count == 1

        # 默认行为：返回 mock:// URL
        mc = MockImageClient()
        resp = await mc.generate(ImageRequest(prompt="test", n=2))
        assert len(resp.images) == 2
        assert resp.images[0].url == "mock://image-0.png"

        # 断言入参
        assert mc.history[0]["prompt"] == "test"
    """

    def __init__(
        self,
        responses: list[ImageResponse] | None = None,
        call_count: int = 0,
        default_url: str = "mock://image-{i}.png",
    ) -> None:
        self.responses: list[ImageResponse] = list(responses) if responses else []
        self.call_count: int = call_count
        self.default_url: str = default_url
        # history 记录每次 generate 的入参，供测试断言
        self.history: list[dict[str, Any]] = []

    def add_response(self, resp: ImageResponse) -> None:
        """追加一个响应到序列末尾。

        适用于测试中动态决定后续响应的场景。
        """
        self.responses.append(resp)

    async def generate(self, request: ImageRequest) -> ImageResponse:
        """返回预设响应并记录入参。

        有预设响应时按序返回；无预设响应时自动生成 ``n`` 张占位图片。

        每次调用递增 ``call_count``，返回 ``responses[call_count - 1]``。
        若响应已耗尽（``call_count > len(responses)``）且无自动生成能力，
        抛 ``RuntimeError``。
        """
        self.call_count += 1
        # 完整记录入参，便于测试断言 ImageStep 的请求构造
        self.history.append({
            "prompt": request.prompt,
            "model": request.model,
            "n": request.n,
            "size": request.size,
            "aspect_ratio": request.aspect_ratio,
            "seed": request.seed,
            "response_format": request.response_format,
            "quality": request.quality,
            "reference_images": request.reference_images,
            "extra": dict(request.extra),
        })

        # 有预设响应：按序返回
        if self.responses:
            if self.call_count > len(self.responses):
                raise RuntimeError(
                    f"MockImageClient 响应已耗尽: call_count={self.call_count}, "
                    f"len(responses)={len(self.responses)}"
                )
            return self.responses[self.call_count - 1]

        # 无预设响应：自动生成占位图片
        images = [
            GeneratedImage(
                url=self.default_url.format(i=i),
                content_type="image/png",
                seed=request.seed if request.seed is not None else 42,
                finish_reason="success",
            )
            for i in range(request.n)
        ]
        return ImageResponse(
            images=images,
            model=request.model or "mock-image",
            raw={"mock": True, "call_count": self.call_count},
        )


__all__ = ["MockImageClient"]
