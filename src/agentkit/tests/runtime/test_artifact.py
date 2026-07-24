"""ArtifactStore：五步写序协议 + 配额 + 事件发布。

出口标准（对齐 §5.6）：
    - 五步写序：tmp → fsync → rename → publish event → distribute
    - 客户端收到 artifact_produced 时文件必然存在（rename 先于 publish）
    - 配额检查在写盘前，超限抛 ArtifactQuotaError 不发事件
    - artifact_id 不可重复（同 run 内）
    - md5 / size / summary 自动计算
    - 不挂 EventBus 时不发事件（仅落盘，测试用）
"""
from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from agentkit.runtime.artifact import (
    ArtifactQuotaError,
    ArtifactRef,
    ArtifactStore,
)
from agentkit.runtime.event import EventBus, EventLog, EventType, RunEvent


# ---------------------------------------------------------------------------
# ArtifactRef 数据类
# ---------------------------------------------------------------------------
def test_artifact_ref_to_dict():
    """ArtifactRef.to_dict 返回完整字段 dict（作为事件 payload）。"""
    ref = ArtifactRef(
        id="art_abc",
        run_id="run_1",
        step_id="step_x",
        uri="output/runs/run_1/artifacts/step_x/art_abc",
        content_type="text/markdown",
        size=100,
        md5="d41d8cd98f00b204e9800998ecf8427e",
        summary="hello",
        ts=1721300000.0,
    )
    d = ref.to_dict()
    assert d["id"] == "art_abc"
    assert d["run_id"] == "run_1"
    assert d["size"] == 100
    assert d["content_type"] == "text/markdown"
    # 所有字段都在
    assert set(d.keys()) == {
        "id", "run_id", "step_id", "uri", "content_type",
        "size", "md5", "summary", "ts",
    }


# ---------------------------------------------------------------------------
# save：五步写序
# ---------------------------------------------------------------------------
async def test_save_creates_file_with_content(tmp_path):
    """save 后文件存在且内容正确（rename 后的最终路径）。"""
    store = ArtifactStore("run_1", base_dir=str(tmp_path))
    content = b"hello artifact"
    ref = await store.save("step_1", content, content_type="text/plain")

    assert os.path.isfile(ref.uri)
    with open(ref.uri, "rb") as f:
        assert f.read() == content
    # tmp 已被 rename 消除
    tmp_path_file = ref.uri + ".tmp"
    assert not os.path.exists(tmp_path_file)


async def test_save_str_content_encoded_utf8(tmp_path):
    """str 内容按 utf-8 编码落盘。"""
    store = ArtifactStore("run_2", base_dir=str(tmp_path))
    text = "你好,世界"
    ref = await store.save("step_1", text, content_type="text/markdown")

    with open(ref.uri, "rb") as f:
        assert f.read() == text.encode("utf-8")
    assert ref.size == len(text.encode("utf-8"))


async def test_save_computes_md5_and_size(tmp_path):
    """save 自动计算 md5 和 size。"""
    store = ArtifactStore("run_3", base_dir=str(tmp_path))
    content = b"abc"
    ref = await store.save("step_1", content)

    expected_md5 = hashlib.md5(content).hexdigest()
    assert ref.md5 == expected_md5
    assert ref.size == len(content)


async def test_save_auto_summary(tmp_path):
    """save 未提供 summary 时自动从内容生成 200 字符摘要。"""
    store = ArtifactStore("run_4", base_dir=str(tmp_path))
    text = "This is a long report. " * 20  # > 200 chars
    ref = await store.save("step_1", text)

    # 摘要为前 200 字符 + "..."
    assert ref.summary.endswith("...")
    assert len(ref.summary) == 203  # 200 + "..."


async def test_save_custom_summary(tmp_path):
    """save 提供自定义 summary 时不自动生成。"""
    store = ArtifactStore("run_5", base_dir=str(tmp_path))
    ref = await store.save("step_1", "content", summary="custom summary")
    assert ref.summary == "custom summary"


async def test_save_custom_artifact_id(tmp_path):
    """save 支持自定义 artifact_id。"""
    store = ArtifactStore("run_6", base_dir=str(tmp_path))
    ref = await store.save("step_1", "content", artifact_id="my_custom_id")
    assert ref.id == "my_custom_id"
    assert ref.uri.endswith("my_custom_id")


async def test_save_auto_generated_artifact_id(tmp_path):
    """save 未提供 artifact_id 时自动生成 art_<uuid hex>。"""
    store = ArtifactStore("run_7", base_dir=str(tmp_path))
    ref = await store.save("step_1", "content")
    assert ref.id.startswith("art_")
    assert len(ref.id) == len("art_") + 12  # art_ + 12 hex chars


async def test_save_uri_uses_forward_slash(tmp_path):
    """uri 跨平台统一使用正斜杠（Windows 反斜杠被替换）。"""
    store = ArtifactStore("run_8", base_dir=str(tmp_path))
    ref = await store.save("step_1", "content")
    assert "\\" not in ref.uri


async def test_save_duplicate_artifact_id_raises(tmp_path):
    """同 run 内 artifact_id 不可重复。"""
    store = ArtifactStore("run_9", base_dir=str(tmp_path))
    await store.save("step_1", "content1", artifact_id="dup_id")
    with pytest.raises(ValueError, match="artifact_id 'dup_id' 已存在"):
        await store.save("step_1", "content2", artifact_id="dup_id")


# ---------------------------------------------------------------------------
# 配额
# ---------------------------------------------------------------------------
async def test_save_rejects_oversized_single(tmp_path):
    """单 artifact 超过 max_size 抛 ArtifactQuotaError，不写盘不发事件。"""
    store = ArtifactStore("run_10", base_dir=str(tmp_path), max_size=10)
    with pytest.raises(ArtifactQuotaError, match="超过单 artifact 上限"):
        await store.save("step_1", b"x" * 100)
    # 不写盘
    assert not os.path.exists(os.path.join(str(tmp_path), "run_10", "artifacts"))


async def test_save_rejects_exceeding_total(tmp_path):
    """run 总量超过 max_total 抛 ArtifactQuotaError。"""
    store = ArtifactStore("run_11", base_dir=str(tmp_path), max_total=20)
    await store.save("step_1", b"x" * 10)  # 累计 10
    await store.save("step_1", b"x" * 5)   # 累计 15
    with pytest.raises(ArtifactQuotaError, match="超过 run 上限"):
        await store.save("step_1", b"x" * 10)  # 累计 25 > 20


async def test_save_no_quota_when_none(tmp_path):
    """max_size / max_total 为 None 时不限制。"""
    store = ArtifactStore("run_12", base_dir=str(tmp_path), max_size=None, max_total=None)
    ref = await store.save("step_1", b"x" * 1024 * 1024)
    assert ref.size == 1024 * 1024


# ---------------------------------------------------------------------------
# 事件发布
# ---------------------------------------------------------------------------
async def test_save_publishes_artifact_produced_event(tmp_path):
    """save 后 EventBus 收到 ARTIFACT_PRODUCED 事件，payload = ArtifactRef 字段。"""
    log = EventLog("run_13", base_dir=str(tmp_path))
    bus = EventBus("run_13", log=log)
    store = ArtifactStore("run_13", event_bus=bus, base_dir=str(tmp_path))

    ref = await store.save("step_1", "hello", content_type="text/markdown")

    events = list(log.read_from())
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.ARTIFACT_PRODUCED
    assert ev.step_id == "step_1"
    assert ev.run_id == "run_13"
    # payload 对齐 ArtifactRef 字段
    payload = ev.payload
    assert payload["id"] == ref.id
    assert payload["uri"] == ref.uri
    assert payload["size"] == ref.size
    assert payload["md5"] == ref.md5
    assert payload["content_type"] == "text/markdown"


async def test_save_no_event_when_no_bus(tmp_path):
    """未挂 EventBus 时 save 不发事件，仅落盘。"""
    store = ArtifactStore("run_14", base_dir=str(tmp_path))  # event_bus=None
    ref = await store.save("step_1", "hello")
    # 文件已落盘
    assert os.path.isfile(ref.uri)
    # 没有 events.jsonl
    log_path = os.path.join(str(tmp_path), "run_14", "events.jsonl")
    assert not os.path.exists(log_path)


async def test_save_event_after_rename(tmp_path):
    """写序保证：publish 事件时文件必然已存在（rename 先于 publish）。

    通过自定义订阅者验证：收到 ARTIFACT_PRODUCED 事件时立即检查文件存在。
    """
    log = EventLog("run_15", base_dir=str(tmp_path))
    bus = EventBus("run_15", log=log)
    store = ArtifactStore("run_15", event_bus=bus, base_dir=str(tmp_path))
    sub = await bus.subscribe()

    await store.save("step_1", "content")

    ev = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev is not None
    assert ev.type == EventType.ARTIFACT_PRODUCED
    # 收到事件时文件必然已存在
    uri = ev.payload["uri"]
    assert os.path.isfile(uri), "收到事件时文件应已存在（rename 先于 publish）"
    sub.cancel()


# ---------------------------------------------------------------------------
# read / list_artifacts
# ---------------------------------------------------------------------------
async def test_read_returns_content(tmp_path):
    """read 按 ref.uri 读取产物内容。"""
    store = ArtifactStore("run_16", base_dir=str(tmp_path))
    content = b"read me"
    ref = await store.save("step_1", content)
    assert store.read(ref) == content


async def test_read_str_content_decodes(tmp_path):
    """str 内容落盘后 read 返回 bytes（utf-8 编码的原文）。"""
    store = ArtifactStore("run_17", base_dir=str(tmp_path))
    text = "你好"
    ref = await store.save("step_1", text)
    assert store.read(ref) == text.encode("utf-8")


async def test_list_artifacts_empty(tmp_path):
    """无产物时 list_artifacts 返回空列表。"""
    store = ArtifactStore("run_18", base_dir=str(tmp_path))
    assert store.list_artifacts() == []


async def test_list_artifacts_sorted_by_ts(tmp_path):
    """list_artifacts 按 ts 升序排列。"""
    store = ArtifactStore("run_19", base_dir=str(tmp_path))
    ref1 = await store.save("step_1", "a", artifact_id="id1")
    # 微小延迟确保 ts 不同
    import time as _time
    _time.sleep(0.001)
    ref2 = await store.save("step_1", "b", artifact_id="id2")
    _time.sleep(0.001)
    ref3 = await store.save("step_1", "c", artifact_id="id3")

    artifacts = store.list_artifacts()
    assert len(artifacts) == 3
    assert artifacts[0].id == "id1"
    assert artifacts[1].id == "id2"
    assert artifacts[2].id == "id3"


async def test_multiple_artifacts_accumulate(tmp_path):
    """多次 save 累计多个 artifact，配额计数与缓存同步更新。"""
    store = ArtifactStore("run_20", base_dir=str(tmp_path), max_total=1000)
    refs = []
    for i in range(5):
        ref = await store.save(
            f"step_{i}", f"content_{i}", artifact_id=f"id_{i}",
            content_type="text/plain",
        )
        refs.append(ref)

    assert len(store.list_artifacts()) == 5
    # 累计字节 = 5 * len("content_N") = 5 * 9 = 45
    assert store._total_bytes == sum(len(f"content_{i}") for i in range(5))


# ---------------------------------------------------------------------------
# 路径布局
# ---------------------------------------------------------------------------
async def test_artifact_path_layout(tmp_path):
    """产物路径符合 {base_dir}/{run_id}/artifacts/{step_id}/{artifact_id} 布局。"""
    store = ArtifactStore("run_layout", base_dir=str(tmp_path))
    ref = await store.save("step_a", "content", artifact_id="art_xyz")

    # 路径分解
    expected_dir = os.path.join(str(tmp_path), "run_layout", "artifacts", "step_a")
    expected_path = os.path.join(expected_dir, "art_xyz")
    # uri 用正斜杠
    assert ref.uri.replace("/", os.sep).endswith(expected_path) or ref.uri.endswith(
        f"run_layout/artifacts/step_a/art_xyz"
    )
    assert os.path.isfile(expected_path)


async def test_artifact_per_step_isolation(tmp_path):
    """不同 step_id 的产物隔离到不同目录。"""
    store = ArtifactStore("run_iso", base_dir=str(tmp_path))
    ref1 = await store.save("step_a", "a", artifact_id="art_1")
    ref2 = await store.save("step_b", "b", artifact_id="art_2")

    assert "step_a" in ref1.uri
    assert "step_b" in ref2.uri
    assert os.path.isfile(ref1.uri)
    assert os.path.isfile(ref2.uri)


# ---------------------------------------------------------------------------
# 二进制内容
# ---------------------------------------------------------------------------
async def test_save_binary_content(tmp_path):
    """save 支持二进制内容（bytes）。"""
    store = ArtifactStore("run_bin", base_dir=str(tmp_path))
    binary = bytes(range(256))
    ref = await store.save("step_1", binary, content_type="application/octet-stream")

    assert ref.size == 256
    assert store.read(ref) == binary
    # 二进制无法 utf-8 解码，summary 退化为 <binary N bytes>
    assert ref.summary == "<binary 256 bytes>"


# ---------------------------------------------------------------------------
# 崩溃窗口：tmp 残留（模拟 step 1-2 之间崩溃）
# ---------------------------------------------------------------------------
async def test_tmp_residual_left_when_simulated_crash(tmp_path):
    """模拟崩溃窗口 1-2：手动构造 .tmp 残留（GCSweeper 测试覆盖清理）。"""
    store = ArtifactStore("run_crash", base_dir=str(tmp_path))
    # 手动创建一个 .tmp 文件模拟崩溃残留
    artifact_dir = os.path.join(str(tmp_path), "run_crash", "artifacts", "step_1")
    os.makedirs(artifact_dir, exist_ok=True)
    tmp_file = os.path.join(artifact_dir, "crashed.tmp")
    with open(tmp_file, "wb") as f:
        f.write(b"incomplete")

    # 正常 save 另一个 artifact 不受影响
    ref = await store.save("step_1", "good", artifact_id="good_id")
    assert os.path.isfile(ref.uri)
    # tmp 残留仍在（由 GCSweeper 清理）
    assert os.path.exists(tmp_file)
