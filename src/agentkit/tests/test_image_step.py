"""ImageStep:图片生成节点的完整测试套件。

覆盖:
    - 基本文生图：MockImageClient → ImageRef 写入 Context
    - 模板渲染：prompt 中的 {{var}} 解析
    - 链式传递：output_url 便捷字段 + reference_image 引用上游
    - save_local：本地保存 + 路径穿越防护
    - 错误处理：retryable 标志 + 不重试永久错误
    - trace 回填：_enrich_trace 填充提供商 / 模型 / 生成数量
    - SSRF 防护：reference_image URL 校验
    - MockImageClient 行为：预设响应 / 自动生成 / history 记录
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agentkit.config import set_default
from agentkit.core.context import Context
from agentkit.image.base import (
    GeneratedImage,
    ImageGenerationError,
    ImageRef,
    ImageRequest,
    ImageResponse,
)
from agentkit.image.mock import MockImageClient
from agentkit.steps.image_step import ImageStep
from agentkit.tests.conftest import RecordingHooks


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _make_step(
    mock_client: MockImageClient | None = None,
    **kw,
) -> ImageStep:
    """构造带 MockImageClient 的 ImageStep。"""
    defaults = dict(
        id="gen1",
        prompt="A beautiful sunset",
        output="images",
        image_client=mock_client or MockImageClient(),
    )
    defaults.update(kw)
    return ImageStep(**defaults)


# ===========================================================================
# 基本文生图
# ===========================================================================
async def test_basic_text_to_image():
    """MockImageClient → ImageRef 写入 Context。"""
    mc = MockImageClient()
    step = _make_step(mc)
    ctx = Context()
    await step.execute(ctx)

    images = ctx.get("images")
    assert len(images) == 1
    # Context 返回 ReadOnlyProxy 代理，验证行为而非类型
    assert images[0].url is not None
    assert mc.call_count == 1


async def test_multiple_images():
    """n=3 → 生成 3 张图片。"""
    mc = MockImageClient()
    step = _make_step(mc, n=3)
    ctx = Context()
    await step.execute(ctx)

    images = ctx.get("images")
    assert len(images) == 3


async def test_prompt_template_rendering():
    """prompt 中的 {{var}} 被正确渲染。"""
    mc = MockImageClient()
    step = _make_step(mc, prompt="A {{animal}} in the {{scene}}")
    ctx = Context()
    ctx.set("animal", "cat")
    ctx.set("scene", "garden")
    await step.execute(ctx)

    # MockImageClient 记录了入参
    assert mc.history[0]["prompt"] == "A cat in the garden"


# ===========================================================================
# 链式传递
# ===========================================================================
async def test_output_url_convenience_field():
    """run 自动写入 {output}_url 便捷字段。"""
    mc = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(url="https://example.com/img.png")]),
    ])
    step = _make_step(mc)
    ctx = Context()
    await step.execute(ctx)

    assert ctx.get("images_url") == "https://example.com/img.png"


async def test_chain_image_to_image():
    """上游 ImageStep 的 output_url 被下游 reference_image 引用。"""
    # 上游：生成图片
    mc1 = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(url="https://cdn.example.com/char.png")]),
    ])
    step1 = _make_step(mc1, id="gen_char", output="char_images")
    ctx = Context()
    await step1.execute(ctx)
    assert ctx.get("char_images_url") == "https://cdn.example.com/char.png"

    # 下游：引用上游图片做图生图
    mc2 = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(url="https://cdn.example.com/scene.png")]),
    ])
    step2 = _make_step(
        mc2, id="gen_scene",
        prompt="Put character in a neon city",
        reference_image="{{char_images_url}}",
        output="scene_images",
    )
    await step2.execute(ctx)

    # 下游收到了上游的 URL 作为参考图
    assert mc2.history[0]["reference_images"] == ["https://cdn.example.com/char.png"]
    assert ctx.get("scene_images_url") == "https://cdn.example.com/scene.png"


async def test_chain_multiple_reference_images():
    """多图参考：reference_image 为 list 时全部解析。"""
    mc = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(url="https://cdn.example.com/composed.png")]),
    ])
    step = _make_step(
        mc,
        reference_image=[
            "https://cdn.example.com/bg.png",
            "https://cdn.example.com/char.png",
        ],
    )
    ctx = Context()
    await step.execute(ctx)

    assert mc.history[0]["reference_images"] == [
        "https://cdn.example.com/bg.png",
        "https://cdn.example.com/char.png",
    ]


async def test_chain_reference_image_from_context_variable():
    """reference_image 从 Context 变量解析（非链式，用户输入场景）。"""
    mc = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(url="https://cdn.example.com/out.png")]),
    ])
    step = _make_step(mc, reference_image="{{user_image}}")
    ctx = Context()
    ctx.set("user_image", "https://user.example.com/upload.jpg")
    await step.execute(ctx)

    assert mc.history[0]["reference_images"] == ["https://user.example.com/upload.jpg"]


# ===========================================================================
# save_local
# ===========================================================================
async def test_save_local_url_image(tmp_path):
    """save_local=True → URL 图片下载到本地（Mock 不走网络，用 b64 测试）。"""
    # 用 b64_json 模拟可保存的图片数据
    import base64
    img_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # 模拟 PNG 头 + 数据
    b64_data = base64.b64encode(img_data).decode()

    mc = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(b64_json=b64_data, content_type="image/png")]),
    ])
    # 设置 workspace_root 覆盖 tmp_path，使 output_dir 在工作空间内
    set_default("workspace_root", str(tmp_path))
    step = _make_step(
        mc, save_local=True,
        output_dir=str(tmp_path / "images"),
    )
    ctx = Context()
    await step.execute(ctx)

    images = ctx.get("images")
    assert images[0].local_path is not None
    assert os.path.exists(images[0].local_path)
    assert images[0].b64_json is None  # 已清除
    assert images[0].size == len(img_data)
    # 文件名包含 step id
    assert "gen1_0" in images[0].local_path


async def test_save_local_path_traversal_blocked(tmp_path):
    """output_dir 在 workspace_root 外 → path_traversal_blocked。"""
    mc = MockImageClient()
    # 设置 workspace_root 为 tmp_path，外部目录在其父级
    set_default("workspace_root", str(tmp_path))
    external_dir = str(tmp_path.parent / "evil_output")
    step = _make_step(mc, save_local=True, output_dir=external_dir)
    ctx = Context()

    with pytest.raises(ImageGenerationError) as exc_info:
        await step.execute(ctx)
    assert exc_info.value.reason == "path_traversal_blocked"
    assert exc_info.value.retryable is False


async def test_save_local_filename_sanitization(tmp_path):
    """step id 含危险字符 → 文件名被清洗。"""
    import base64
    img_data = b"\x89PNG" + b"\x00" * 10
    b64_data = base64.b64encode(img_data).decode()

    mc = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(b64_json=b64_data, content_type="image/png")]),
    ])
    # 设置 workspace_root 覆盖 tmp_path，使 output_dir 在工作空间内
    set_default("workspace_root", str(tmp_path))
    step = _make_step(
        mc, id="../../../etc/passwd",  # 危险 id
        save_local=True,
        output_dir=str(tmp_path / "images"),
    )
    ctx = Context()
    await step.execute(ctx)

    images = ctx.get("images")
    filepath = images[0].local_path
    # 危险字符被替换为 _
    assert ".." not in os.path.basename(filepath)
    assert "/" not in os.path.basename(filepath)


# ===========================================================================
# 错误处理
# ===========================================================================
async def test_non_retryable_error_no_retry():
    """retryable=False 的错误不被重试（直接进入钩子决策）。"""
    mc = MockImageClient()
    # 替换 generate 方法，总是抛永久错误
    async def fail_generate(request):
        raise ImageGenerationError(
            "Content moderation blocked",
            reason="moderation_blocked",
            retryable=False,
        )
    mc.generate = fail_generate

    step = _make_step(
        mc,
        retry=__import__("agentkit.config", fromlist=["RetryPolicy"]).RetryPolicy(
            count=3, backoff="fixed", base_seconds=0.01,
        ),
    )
    ctx = Context()

    with pytest.raises(ImageGenerationError) as exc_info:
        await step.execute(ctx)
    assert exc_info.value.reason == "moderation_blocked"
    # 不应重试：trace 的 retry_count 应为 0
    traces = ctx.get_traces()
    assert traces[-1].retry_count == 0


async def test_retryable_error_retried():
    """retryable=True 的错误会被重试。"""
    call_count = 0

    class FlakyClient(MockImageClient):
        async def generate(self, request):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ImageGenerationError(
                    "Timeout", reason="timeout", retryable=True,
                )
            return await super().generate(request)

    mc = FlakyClient()
    step = _make_step(
        mc,
        retry=__import__("agentkit.config", fromlist=["RetryPolicy"]).RetryPolicy(
            count=3, backoff="fixed", base_seconds=0.01,
        ),
    )
    ctx = Context()
    await step.execute(ctx)

    assert call_count == 3
    traces = ctx.get_traces()
    assert traces[-1].status == "success"
    assert traces[-1].retry_count == 2  # 重试了 2 次


async def test_no_client_available_error():
    """未注入客户端且全局默认不可用 → no_client_available。"""
    from agentkit.image import clear_default_image_client, clear_provider_client_cache
    clear_default_image_client()
    clear_provider_client_cache()

    step = ImageStep(
        id="no_client",
        prompt="test",
        output="images",
        # 不注入 image_client，不指定 provider
    )
    ctx = Context()

    with pytest.raises(ImageGenerationError) as exc_info:
        await step.execute(ctx)
    # 客户端创建成功但缺少 API Key，generate 时报 missing_api_key
    assert exc_info.value.reason == "missing_api_key"


# ===========================================================================
# trace 回填
# ===========================================================================
async def test_trace_enrichment():
    """_enrich_trace 填充 provider / model / n_generated。"""
    mc = MockImageClient(responses=[
        ImageResponse(
            images=[GeneratedImage(url="https://example.com/1.png")],
            model="image-01",
        ),
    ])
    step = _make_step(mc, provider="minimax")
    ctx = Context()
    trace = await step.execute(ctx)

    assert trace.tool_calls[0]["type"] == "image_generation"
    assert trace.tool_calls[0]["provider"] == "minimax"
    assert trace.tool_calls[0]["model"] == "image-01"
    assert trace.tool_calls[0]["n_generated"] == 1


async def test_trace_with_hooks():
    """RecordingHooks 记录 before_step / after_step 事件。"""
    mc = MockImageClient()
    step = _make_step(mc)
    hooks = RecordingHooks()
    ctx = Context()
    await step.execute(ctx, hooks)

    assert "before_step:gen1" in hooks.events
    assert "after_step:gen1" in hooks.events


# ===========================================================================
# SSRF 防护
# ===========================================================================
async def test_ssrf_reference_image_blocked():
    """reference_image 指向私网地址 → ImageGenerationError。"""
    mc = MockImageClient()
    step = _make_step(mc, reference_image="http://127.0.0.1:8080/evil.jpg")
    ctx = Context()

    with pytest.raises(ImageGenerationError) as exc_info:
        await step.execute(ctx)
    assert exc_info.value.reason == "private_ip_blocked"
    assert exc_info.value.retryable is False


async def test_ssrf_metadata_host_blocked():
    """reference_image 指向云元数据接口 → blocked_host。"""
    mc = MockImageClient()
    step = _make_step(mc, reference_image="http://169.254.169.254/latest/meta-data/")
    ctx = Context()

    with pytest.raises(ImageGenerationError) as exc_info:
        await step.execute(ctx)
    assert exc_info.value.reason in ("blocked_host", "private_ip_blocked")


async def test_ssrf_non_http_scheme_blocked():
    """reference_image 使用非 HTTP scheme → invalid_url_scheme。"""
    mc = MockImageClient()
    step = _make_step(mc, reference_image="file:///etc/passwd")
    ctx = Context()

    with pytest.raises(ImageGenerationError) as exc_info:
        await step.execute(ctx)
    assert exc_info.value.reason == "invalid_url_scheme"


# ===========================================================================
# MockImageClient 行为
# ===========================================================================
async def test_mock_client_default_generation():
    """无预设响应时自动生成占位图片。"""
    mc = MockImageClient()
    resp = await mc.generate(ImageRequest(prompt="test", n=2))

    assert len(resp.images) == 2
    assert resp.images[0].url == "mock://image-0.png"
    assert resp.images[1].url == "mock://image-1.png"


async def test_mock_client_preset_responses():
    """预设响应按序返回。"""
    mc = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(url="https://a.com/1.png")]),
        ImageResponse(images=[GeneratedImage(url="https://b.com/2.png")]),
    ])
    r1 = await mc.generate(ImageRequest(prompt="first"))
    r2 = await mc.generate(ImageRequest(prompt="second"))

    assert r1.images[0].url == "https://a.com/1.png"
    assert r2.images[0].url == "https://b.com/2.png"
    assert mc.call_count == 2


async def test_mock_client_history_recording():
    """history 完整记录每次 generate 的入参。"""
    mc = MockImageClient()
    await mc.generate(ImageRequest(
        prompt="A cat",
        model="image-01",
        n=2,
        size="1024x1024",
        aspect_ratio="16:9",
        seed=42,
    ))

    record = mc.history[0]
    assert record["prompt"] == "A cat"
    assert record["model"] == "image-01"
    assert record["n"] == 2
    assert record["size"] == "1024x1024"
    assert record["aspect_ratio"] == "16:9"
    assert record["seed"] == 42


async def test_mock_client_exhausted():
    """预设响应耗尽 → RuntimeError。"""
    mc = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(url="https://a.com/1.png")]),
    ])
    await mc.generate(ImageRequest(prompt="first"))

    with pytest.raises(RuntimeError, match="响应已耗尽"):
        await mc.generate(ImageRequest(prompt="second"))


# ===========================================================================
# ImageRef.to_url() 链式传递核心方法
# ===========================================================================
def test_imageref_to_url_prefers_url():
    """to_url() 优先返回 url。"""
    ref = ImageRef(url="https://example.com/img.png", b64_json="abc", local_path="/tmp/x.png")
    assert ref.to_url() == "https://example.com/img.png"


def test_imageref_to_url_falls_back_to_local_path():
    """无 url 时返回 file:// URI。"""
    import os
    import pathlib
    # 使用绝对路径，兼容 Windows 与 Unix
    local = os.path.abspath(os.path.join(os.sep, "tmp", "images", "test.png"))
    ref = ImageRef(b64_json="abc", local_path=local)
    assert ref.to_url() == pathlib.Path(local).as_uri()


def test_imageref_to_url_falls_back_to_data_uri():
    """无 url 和 local_path 时返回 data URI。"""
    ref = ImageRef(b64_json="abc123", content_type="image/png")
    assert ref.to_url() == "data:image/png;base64,abc123"


def test_imageref_to_url_none():
    """无任何数据时返回 None。"""
    ref = ImageRef()
    assert ref.to_url() is None


def test_imageref_to_dict():
    """to_dict() 序列化为 dict。"""
    ref = ImageRef(
        url="https://example.com/img.png",
        content_type="image/jpeg",
        size=1024,
        seed=42,
    )
    d = ref.to_dict()
    assert d["url"] == "https://example.com/img.png"
    assert d["content_type"] == "image/jpeg"
    assert d["size"] == 1024
    assert d["seed"] == 42


# ===========================================================================
# YAML 加载集成
# ===========================================================================
async def test_yaml_image_step_loading(tmp_path):
    """YAML 中 type: image 被 _compile_step 正确编译。"""
    from agentkit.yaml.loader import load_workflow_from_dict
    from agentkit.image import set_default_image_client

    # 注入 MockImageClient 避免真实 API 调用
    mc = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(url="https://example.com/gen.png")]),
    ])
    set_default_image_client(mc)

    yaml_config = {
        "name": "test_image_workflow",
        "steps": [
            {
                "id": "gen",
                "type": "image",
                "prompt": "A {{topic}} image",
                "aspect_ratio": "16:9",
                "output": "result",
            },
        ],
    }

    wf = load_workflow_from_dict(yaml_config)
    assert len(wf.steps) == 1
    assert isinstance(wf.steps[0], ImageStep)
    assert wf.steps[0].aspect_ratio == "16:9"

    # 执行验证
    from agentkit.core.context import Context
    ctx = Context()
    ctx.set("topic", "sunset")
    await wf.steps[0].execute(ctx)
    assert ctx.get("result_url") == "https://example.com/gen.png"


async def test_yaml_image_providers_section(tmp_path):
    """YAML image_providers 段注册自定义提供商。"""
    from agentkit.yaml.loader import load_workflow_from_dict
    from agentkit.image.provider import get_image_provider

    yaml_config = {
        "name": "test_providers",
        "image_providers": {
            "my_custom": {
                "base_url": "https://api.my-custom-provider.com/v1",
                "api_key_env": "MY_CUSTOM_API_KEY",
                "model": "my-image-model",
                "provider_type": "openai",
            },
        },
        "steps": [],
    }

    load_workflow_from_dict(yaml_config)

    provider = get_image_provider("my_custom")
    assert provider.base_url == "https://api.my-custom-provider.com/v1"
    assert provider.api_key_env == "MY_CUSTOM_API_KEY"
    assert provider.model == "my-image-model"


async def test_yaml_image_step_with_reference_image():
    """YAML 中 reference_image 被正确编译。"""
    from agentkit.yaml.loader import load_workflow_from_dict
    from agentkit.image import set_default_image_client

    mc = MockImageClient(responses=[
        ImageResponse(images=[GeneratedImage(url="https://example.com/edited.png")]),
    ])
    set_default_image_client(mc)

    yaml_config = {
        "name": "test_ref",
        "steps": [
            {
                "id": "edit",
                "type": "image",
                "prompt": "Edit this image",
                "reference_image": "{{input_url}}",
                "output": "edited",
            },
        ],
    }

    wf = load_workflow_from_dict(yaml_config)
    step = wf.steps[0]
    assert step.reference_image == "{{input_url}}"

    ctx = Context()
    ctx.set("input_url", "https://cdn.example.com/source.jpg")
    await step.execute(ctx)
    assert mc.history[0]["reference_images"] == ["https://cdn.example.com/source.jpg"]
