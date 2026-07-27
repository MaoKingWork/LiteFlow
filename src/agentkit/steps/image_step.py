"""steps.image_step —— ImageStep:图片生成节点。

本模块实现 AgentKit 中的图片生成 Step（``type: image``），与 ``LLMStep``
对称。它把一个图片生成 prompt 模板转化为一次图片生成 API 调用，最终把
生成的图片引用（``list[ImageRef]``）按 ``output`` 写入 Context。

设计原则：
    - 高度模块化：仅依赖 ``steps.base`` / ``image.base`` / ``image._http`` /
      ``core.template``；``image`` 子包的客户端与提供商解析延迟到 ``run``
      内导入，避免模块加载期触发 httpx 依赖。
    - 安全：``_save_local`` 做路径穿越防护与文件名清洗；``_resolve_reference_images``
      对每个 URL 做 SSRF 校验；``_http.download`` 流式下载带大小限制。
    - 链式传递：自动写入 ``{output}_url`` 便捷字段，下游 Step 通过
      ``{{step_id.output_url}}`` 即可引用本 Step 生成的图片。
    - 可观测：通过重写 ``_enrich_trace`` 把提供商 / 模型 / 生成数量回填到 trace。
    - 可拓展：客户端与提供商均可注入，便于测试隔离。

公开 API：
    - ImageStep: 图片生成 Step 实现
"""

from __future__ import annotations

import base64 as b64mod
import os
import re
from typing import TYPE_CHECKING, Any

from agentkit.config import RetryPolicy, get_default
from agentkit.image.base import (
    GeneratedImage,
    ImageClient,
    ImageGenerationError,
    ImageRef,
    ImageRequest,
    ImageResponse,
)
from agentkit.steps.base import BaseStep, StepTrace, register_step

if TYPE_CHECKING:
    from agentkit.core.context import Context


__all__ = ["ImageStep"]


@register_step("image")
class ImageStep(BaseStep):
    """图片生成 Step：调用 ``ImageClient`` 生成图片，写入 Context。

    与 ``LLMStep`` 对称的 Step 实现。把 prompt 模板渲染后调用图片生成 API，
    将返回的 ``ImageResponse`` 转换为 ``list[ImageRef]`` 写入 Context。

    链式传递：``run`` 在写入主输出 ``ctx.set(self.output, image_refs)`` 的同时，
    自动写入便捷字段 ``ctx.set(f"{self.output}_url", image_refs[0].to_url())``。
    下游 Step 通过 ``{{step_id.output_url}}`` 即可引用，无需了解 ``ImageRef``
    的内部结构。

    Args:
        id:              Step 实例标识（用于 trace / 日志）。
        prompt:          图像描述模板，支持 ``{{var}}`` / ``${ENV}``。
        model:           模型名覆盖；None 时用 provider 默认模型。
        provider:        提供商名（预设或自定义注册名）；None 时用全局默认。
        n:               生成数量，默认 1。
        size:            图片尺寸，如 ``"1024x1024"``。
        aspect_ratio:    宽高比，如 ``"16:9"``（MiniMax 使用）。
        quality:         渲染质量：``"low"`` | ``"medium"`` | ``"high"``。
        seed:            随机种子。
        response_format: 返回格式：``"url"`` | ``"base64"``。默认 ``"url"``。
        reference_image: 参考图 URL 或 Context 变量（图生图）。
                         支持 ``str``（单图）或 ``list[str]``（多图）。
                         链式传递：``reference_image: "{{prev_step.output_url}}"``。
        save_local:      是否下载 / 保存到本地文件。默认 ``False``。
        output_dir:      本地保存目录；None 时用 config 默认值。
        output:          输出键名；结果（``list[ImageRef]``）通过 ``ctx.set`` 写入。
        image_client:    客户端注入（测试用 ``MockImageClient``）。
        retry:           实例级重试策略。
        timeout:         实例级超时秒数。
        inputs:          显式输入端口声明。
        outputs:         显式输出端口声明（与 output 互斥）。
        strict_scope:    是否封闭输入作用域。
        extra:           提供商特有参数（透传到 ``ImageRequest.extra``）。

    用法示例::

        step = ImageStep(
            id="gen_cover",
            prompt="设计一张{{topic}}主题的封面图",
            provider="minimax",
            aspect_ratio="16:9",
            output="cover_images",
            image_client=MockImageClient(),
        )
        ctx = Context()
        ctx.set("topic", "AI")
        await step.execute(ctx)
        # ctx.get("cover_images") → [ImageRef(url="...")]
        # ctx.get("cover_images_url") → "..."
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
    ) -> None:
        super().__init__(
            id=id, output=output, retry=retry, timeout=timeout,
            inputs=inputs, outputs=outputs, strict_scope=strict_scope,
        )
        self.prompt: str = prompt
        self.model: str | None = model
        self.provider: str | None = provider
        self.n: int = n
        self.size: str | None = size
        self.aspect_ratio: str | None = aspect_ratio
        self.quality: str | None = quality
        self.seed: int | None = seed
        self.response_format: str = response_format
        self.reference_image: str | list[str] | None = reference_image
        self.save_local: bool = save_local
        self.output_dir: str | None = output_dir
        self.image_client: ImageClient | None = image_client
        self.extra: dict[str, Any] = extra or {}
        # trace scratch（run 期间填充，_enrich_trace 回填到 trace）
        self._last_provider: str = ""
        self._last_model: str = ""
        self._last_n_generated: int = 0

    # ------------------------------------------------------------------
    # run —— 核心执行逻辑
    # ------------------------------------------------------------------
    async def run(self, ctx: Context) -> Context:
        """执行图片生成并写入 output。

        流程：
            1. 重置 trace scratch。
            2. 解析 prompt 模板（支持 ``{{var}}`` / ``${ENV}``）。
            3. 解析 reference_image（图生图，支持 Context 变量引用 + 链式传递）。
            4. 构建 ``ImageRequest``（frozen dataclass）。
            5. 取 ``ImageClient``（注入优先 → 提供商缓存 → 全局默认）。
            6. 调用 ``client.generate(request)``。
            7. 转换 ``ImageResponse`` → ``list[ImageRef]``。
            8. ``save_local=True`` 时下载 / 保存图片到本地（带路径安全校验）。
            9. ``ctx.set(self.output, image_refs)`` +
               ``ctx.set(f"{self.output}_url", url)``（链式传递便捷字段）。

        Args:
            ctx: 当前上下文。

        Returns:
            Context: 同一上下文。

        Raises:
            ImageGenerationError: 生成失败（网络 / 鉴权 / 内容安全等）。
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

        # 8. save_local 时下载到本地（带安全校验）
        if self.save_local:
            image_refs = await self._save_local(image_refs)

        # 9. 写入 Context（主输出 + 便捷 URL 输出）
        if len(self.outputs) > 1:
            self._emit_dict_outputs(ctx, {"images": image_refs})
        elif self.output:
            ctx.set(self.output, image_refs)
            # 链式传递：自动写入 output_url，供下游 Step 引用
            if image_refs:
                ctx.set(f"{self.output}_url", image_refs[0].to_url())

        return ctx

    # ------------------------------------------------------------------
    # _get_client —— 取图片生成客户端
    # ------------------------------------------------------------------
    def _get_client(self) -> ImageClient:
        """获取图片生成客户端（优先级：注入 > 提供商缓存 > 全局默认）。

        Returns:
            ImageClient: 可用的客户端实例。

        Raises:
            ImageGenerationError: 无法获取客户端（未配置且全局默认创建失败）。
        """
        # 1. 注入优先（测试场景）
        if self.image_client is not None:
            return self.image_client

        # 2. 按 provider 路由（指定了提供商名）
        if self.provider:
            from agentkit.image import get_client_for_provider

            return get_client_for_provider(self.provider)

        # 3. 全局默认
        from agentkit.image import get_default_image_client

        client = get_default_image_client()
        if client is None:
            raise ImageGenerationError(
                "无法获取图片生成客户端：未注入 image_client，且全局默认"
                "客户端创建失败（API Key 未配置或提供商不可用）",
                reason="no_client_available",
                retryable=False,
            )
        return client

    # ------------------------------------------------------------------
    # _resolve_reference_images —— 参考图解析（支持链式传递）
    # ------------------------------------------------------------------
    def _resolve_reference_images(
        self, ctx: Context
    ) -> list[str] | None:
        """解析 reference_image，支持三种来源（链式传递核心）。

        来源 1 - 直接 URL 字符串::

            reference_image: "https://example.com/photo.jpg"

        来源 2 - Context 变量引用（LLM 输出 / 用户输入）::

            reference_image: "{{character_url}}"

        来源 3 - 上游 ImageStep 链式传递::

            reference_image: "{{prev_step.output_url}}"

        多图场景::

            reference_image:
              - "{{step_a.output_url}}"
              - "{{step_b.output_url}}"

        解析后对每个 URL 调用 ``validate_url`` 校验安全性（防 SSRF）。

        Args:
            ctx: 当前上下文。

        Returns:
            list[str] | None: 解析后的 URL 列表；无参考图时为 None。
        """
        if self.reference_image is None:
            return None

        from agentkit.image._http import validate_url

        # 统一为 list 处理
        raw: str | list[str] = self.reference_image
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

    # ------------------------------------------------------------------
    # _to_image_refs —— 响应转换
    # ------------------------------------------------------------------
    def _to_image_refs(
        self, response: ImageResponse
    ) -> list[ImageRef]:
        """把 ``ImageResponse`` 转换为 ``list[ImageRef]``。

        ``ImageRef`` 是写入 Context 的轻量结构，避免在 Context 中存储大块
        base64（``save_local=True`` 时会清除 ``b64_json``）。

        Args:
            response: 图片生成响应。

        Returns:
            list[ImageRef]: 图片引用列表。
        """
        refs: list[ImageRef] = []
        for img in response.images:
            refs.append(ImageRef(
                url=img.url,
                b64_json=img.b64_json,
                content_type=img.content_type,
                seed=img.seed,
                finish_reason=img.finish_reason,
            ))
        return refs

    # ------------------------------------------------------------------
    # _save_local —— 本地保存（带安全校验）
    # ------------------------------------------------------------------
    async def _save_local(
        self, refs: list[ImageRef]
    ) -> list[ImageRef]:
        """下载 URL 图片到本地，或保存 base64 到文件。

        安全措施：
            1. ``output_dir`` 转绝对路径后校验是否在 ``workspace_root`` 内
               （防路径穿越，用 ``os.path.commonpath`` 严格判断）。
            2. ``self.id`` 经正则清洗，移除 ``../`` 等危险字符。
            3. URL 下载使用 ``_http.download()`` 流式写入 + 大小限制。
            4. base64 解码后检查大小，防止超大 base64 撑爆内存。

        Args:
            refs: 原始图片引用列表。

        Returns:
            list[ImageRef]: 保存后的引用列表（``local_path`` 有值，``b64_json`` 清除）。
        """
        from agentkit.image._http import download

        # 1. 解析并校验输出目录（防路径穿越）
        output_dir = self.output_dir or str(
            get_default("default_image_download_dir")
        )
        output_dir = os.path.abspath(output_dir)
        workspace_root = os.path.abspath(
            str(get_default("workspace_root"))
        )
        # os.path.commonpath 在 Windows 跨盘符时抛 ValueError，
        # 跨盘符本身即意味着目标不在 workspace_root 内，视为路径穿越。
        try:
            within = (
                os.path.commonpath([output_dir, workspace_root])
                == workspace_root
            )
        except ValueError:
            within = False
        if not within:
            raise ImageGenerationError(
                f"输出目录 {output_dir!r} 不在工作空间 {workspace_root!r} 内，"
                f"疑似路径穿越攻击",
                reason="path_traversal_blocked",
                retryable=False,
            )
        os.makedirs(output_dir, exist_ok=True)

        # 2. 清洗 step id（文件名安全）
        # 仅允许字母/数字/下划线/连字符，其余全部替换为 _
        # 不允许 . 以防 .. 路径穿越（扩展名由下方单独添加）
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", self.id or "image")
        # 防止清洗后仍以 . 开头（隐藏文件 / 目录穿越）
        safe_id = safe_id.lstrip(".")

        max_download = int(get_default("image_max_download_size"))

        saved_refs: list[ImageRef] = []
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
                        reason="file_too_large",
                        retryable=False,
                    )
                with open(filepath, "wb") as f:
                    f.write(data)
                size = len(data)

            elif ref.url:
                # URL → 流式下载（带 SSRF 校验 + 大小限制）
                size = await download(
                    ref.url, filepath, max_size=max_download
                )

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

    # ------------------------------------------------------------------
    # _enrich_trace —— trace 回填
    # ------------------------------------------------------------------
    def _enrich_trace(self, trace: StepTrace) -> None:
        """回填图片生成信息到 trace（供可观测性查看）。

        把 ``run`` 期间暂存的提供商 / 模型 / 生成数量写入 ``trace.tool_calls``，
        供可观测性与检查点使用。

        Args:
            trace: 当前执行的轨迹。
        """
        if self._last_model or self._last_n_generated:
            trace.tool_calls = [{
                "type": "image_generation",
                "provider": self._last_provider,
                "model": self._last_model,
                "n_generated": self._last_n_generated,
            }]
