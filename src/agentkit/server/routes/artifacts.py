"""server.routes.artifacts —— 产物清单 + 下载 + GCSweeper 调度（E4）。

端点:
    GET /api/runs/{run_id}/artifacts          产物清单
    GET /api/artifacts/{run_id}/{artifact_id}  下载（支持 Range）

辅助函数:
    list_artifacts_from_log  从事件日志重建产物引用列表

后台任务:
    gc_sweeper_loop  GCSweeper 定时循环（在 F1 lifespan 启动）

设计原则:
    - 懒加载 fastapi / starlette
    - 产物清单从 events.jsonl 重建（server 重启后内存缓存丢失）
    - 下载支持 Range 头分块（206 Partial Content）

注意:本模块 **不用** ``from __future__ import annotations``
（Pydantic + FastAPI 局部类解析陷阱）。
"""

import asyncio
import os

from agentkit.runtime.event import EventLog, EventType

__all__ = [
    "create_artifact_routes",
    "list_artifacts_from_log",
    "gc_sweeper_loop",
]


def list_artifacts_from_log(run_id: str, base_dir: str) -> list:
    """从事件日志重建产物引用列表。

    扫描 ``events.jsonl`` 中所有 ``ARTIFACT_PRODUCED`` 事件,
    payload 即 :meth:`ArtifactRef.to_dict` 的结果。

    Args:
        run_id:  run id。
        base_dir: 事件日志根目录。

    Returns:
        list[dict]: 产物引用列表（按事件顺序）,每项含
        ``id`` / ``step_id`` / ``uri`` / ``content_type`` / ``size`` /
        ``md5`` / ``summary`` / ``ts``。
    """
    log = EventLog(run_id, base_dir=base_dir)
    refs = []
    for event in log.read_from():
        if event.type == EventType.ARTIFACT_PRODUCED:
            payload = dict(event.payload)
            payload.setdefault("run_id", run_id)
            refs.append(payload)
    return refs


def _find_artifact_uri(run_id: str, artifact_id: str, base_dir: str):
    """从事件日志查找指定产物的 uri。

    Returns:
        str: 产物文件路径;不存在返回 None。
    """
    for ref in list_artifacts_from_log(run_id, base_dir):
        if ref.get("id") == artifact_id:
            uri = ref.get("uri", "")
            if uri and os.path.exists(uri):
                return uri
    return None


def _parse_range(range_header: str, file_size: int):
    """解析 Range 头。

    支持三种形式:
        - ``bytes=0-4``     前 5 字节
        - ``bytes=5-``      从第 6 字节到末尾
        - ``bytes=-5``      末 5 字节（后缀范围）

    Returns:
        tuple: (start, end) 含端点;格式错误返回 None。
    """
    try:
        unit, ranges = range_header.split("=", 1)
        if unit.strip().lower() != "bytes":
            return None
        ranges = ranges.strip()
        # 仅支持单段范围
        if "," in ranges:
            ranges = ranges.split(",", 1)[0].strip()
        start_str, sep, end_str = ranges.partition("-")
        if not sep:
            return None
        if start_str == "":
            # 后缀范围: bytes=-N → 末 N 字节
            n = int(end_str)
            if n <= 0:
                return None
            start = max(0, file_size - n)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
            if end >= file_size:
                end = file_size - 1
        if start > end or start >= file_size or start < 0:
            return None
        return start, end
    except (ValueError, AttributeError):
        return None


def create_artifact_routes(base_dir: str, prefix: str = "/api"):
    """创建产物路由。

    Args:
        base_dir: 事件日志 / 产物存储根目录。
        prefix:   URL 前缀,默认 ``/api``。
    """
    try:
        from fastapi import APIRouter, HTTPException, Request
        from starlette.responses import FileResponse, StreamingResponse
    except ImportError as e:
        raise ImportError(
            "Server 需要 fastapi: pip install agentkit[server]"
        ) from e

    router = APIRouter(prefix=prefix, tags=["artifacts"])

    # ------------------------------------------------------------------
    # GET /runs/{run_id}/artifacts —— 产物清单
    # ------------------------------------------------------------------
    @router.get("/runs/{run_id}/artifacts")
    async def list_artifacts(run_id: str):
        """返回指定 run 的产物清单。"""
        refs = list_artifacts_from_log(run_id, base_dir)
        return {"artifacts": refs}

    # ------------------------------------------------------------------
    # GET /artifacts/{run_id}/{artifact_id} —— 下载（支持 Range）
    # ------------------------------------------------------------------
    @router.get("/artifacts/{run_id}/{artifact_id}")
    async def download_artifact(run_id: str, artifact_id: str, request: Request):
        """下载产物文件,支持 Range 头分块。"""
        uri = _find_artifact_uri(run_id, artifact_id, base_dir)
        if uri is None:
            raise HTTPException(status_code=404, detail="产物不存在")

        file_size = os.path.getsize(uri)
        range_header = request.headers.get("range", "")

        if not range_header:
            return FileResponse(
                uri,
                media_type="application/octet-stream",
                filename=artifact_id,
            )

        # Range 请求 → 206 Partial Content
        parsed = _parse_range(range_header, file_size)
        if parsed is None:
            return FileResponse(
                uri,
                media_type="application/octet-stream",
                filename=artifact_id,
                headers={"Content-Range": f"bytes */{file_size}"},
                status_code=416,
            )

        start, end = parsed
        content_length = end - start + 1

        def _iter():
            with open(uri, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            _iter(),
            media_type="application/octet-stream",
            status_code=206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(content_length),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'attachment; filename="{artifact_id}"',
            },
        )

    return router


async def gc_sweeper_loop(base_dir: str, interval: float, grace: float):
    """GCSweeper 定时循环（在 lifespan 启动）。

    每 ``interval`` 秒执行一次 :meth:`GCSweeper.sweep_once`。

    Args:
        base_dir: 存储根目录。
        interval: 扫描间隔（秒）。
        grace:    孤儿文件宽限期（秒）。
    """
    from agentkit.runtime.artifact import GCSweeper

    sweeper = GCSweeper(base_dir=base_dir, orphan_grace_seconds=grace)
    while True:
        await asyncio.sleep(interval)
        try:
            stats = sweeper.sweep_once()
            if stats.get("deleted_tmp", 0) or stats.get("deleted_orphan", 0):
                import logging
                logging.getLogger(__name__).info(
                    "GCSweeper: %s", stats
                )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("GCSweeper 扫描失败")
