"""server.app —— FastAPI 应用工厂 + 生命周期管理（F1）。

本模块实现 :func:`create_app`，把前置阶段的全部能力组装成一个可运行的
FastAPI 应用：

    - lifespan：启动时 Reconciler 对账 + 启动 GCSweeper 定时循环；
      关闭时取消 GC 任务 + RunManager.shutdown()。
    - 安全中间件（C3）：CORS（可选）。
    - 路由：workflows（E1）+ runs（E2）+ artifacts（E4）+ SSE（E3）。
    - 依赖注入：RunManager / CheckpointStore 通过 ``app.state`` 共享。

设计要点（对齐 ``docs/p1-implementation-plan.md`` §F1）：
    - **懒加载**：模块顶层不 import fastapi，仅在 :func:`create_app` 内导入。
      这样未安装 ``agentkit[server]`` extra 时仍可 ``import agentkit.server.app``
      而不报错；调用 :func:`create_app` 才抛 ImportError 带安装提示。
    - **不侵入 core/runtime**：经公开 API 接入，``core/`` 零改动。
    - **lifespan 幂等**：Reconciler 失败不阻断启动（记 warning，继续运行）。
    - **/health 端点不鉴权**：便于容器编排探活。

注意：本模块 **不用** ``from __future__ import annotations``
（Pydantic + FastAPI 局部类解析陷阱，见 ``adapters/api_router.py`` 注释）。

公开 API：
    - create_app: FastAPI 应用工厂
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from agentkit.core.checkpoint import CheckpointStore, LocalCheckpointStore
from agentkit.runtime.reconciler import Reconciler
from agentkit.runtime.run_manager import RunManager
from agentkit.server.routes.artifacts import create_artifact_routes, gc_sweeper_loop
from agentkit.server.routes.runs import create_run_routes
from agentkit.server.routes.workflows import create_workflow_routes
from agentkit.server.settings import ServerSettings

logger = logging.getLogger(__name__)

__all__ = ["create_app"]


@asynccontextmanager
async def _lifespan(app):
    """应用生命周期上下文。

    启动时：
        1. :class:`Reconciler.reconcile()` —— 对账僵尸 run + GC 清理
        2. 启动 :func:`gc_sweeper_loop` 定时循环（后台 asyncio.Task）

    关闭时：
        1. 取消 GCSweeper 循环
        2. :meth:`RunManager.shutdown()` —— 关闭所有活跃 Task + EventBus

    Reconciler 失败不阻断启动（记 warning），保证 server 可用。
    """
    settings: ServerSettings = app.state.settings
    run_manager: RunManager = app.state.run_manager
    base_dir: str = run_manager._base_dir

    # ------------------------------------------------------------------
    # 启动：Reconciler + GC 循环
    # ------------------------------------------------------------------
    try:
        reconciler = Reconciler(
            run_manager._checkpoint_store, base_dir=base_dir
        )
        result = await reconciler.reconcile()
        logger.info(
            "启动对账完成：interrupted=%d, gc=%s, corrupt_logs=%d",
            result.interrupted_count,
            result.gc_stats,
            result.event_log_corrupt,
        )
    except Exception:
        # 对账失败不阻断启动（可能 base_dir 不存在或权限问题）
        logger.warning("启动对账失败，跳过", exc_info=True)

    gc_task = asyncio.create_task(
        gc_sweeper_loop(
            base_dir,
            interval=settings.gc_interval_seconds,
            grace=settings.gc_orphan_grace_seconds,
        ),
        name="gc_sweeper",
    )

    try:
        yield
    finally:
        # ------------------------------------------------------------------
        # 关闭：取消 GC + shutdown RunManager
        # ------------------------------------------------------------------
        gc_task.cancel()
        try:
            await gc_task
        except asyncio.CancelledError:
            pass

        try:
            await run_manager.shutdown()
        except Exception:
            logger.warning("RunManager shutdown 失败", exc_info=True)


def create_app(
    workflow_dir: str = ".",
    *,
    settings: ServerSettings | None = None,
    checkpoint_store: CheckpointStore | None = None,
):
    """创建 FastAPI 应用。

    懒加载 fastapi。组装 lifespan + 安全中间件 + 全部路由。

    Args:
        workflow_dir:    工作流 YAML 目录。
        settings:        Server 配置；``None`` 时 :meth:`ServerSettings.from_config`。
        checkpoint_store: 检查点存储；``None`` 时 :class:`LocalCheckpointStore`。

    Returns:
        FastAPI: 已组装好的应用实例。

    Raises:
        ImportError: 未安装 fastapi 时。
    """
    try:
        from fastapi import FastAPI
    except ImportError as e:
        raise ImportError(
            "Server 需要 fastapi: pip install agentkit[server]"
        ) from e

    # 懒导入 security（内部也懒加载 starlette）
    from agentkit.server.security import create_security_middleware

    settings = settings or ServerSettings.from_config()
    base_dir = os.path.join("output", "runs")
    checkpoint_store = checkpoint_store or LocalCheckpointStore()
    run_manager = RunManager(checkpoint_store=checkpoint_store, base_dir=base_dir)

    app = FastAPI(
        title="AgentKit Server",
        description="工作流可视化服务：CRUD + run 控制 + SSE + 产物下载",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # 共享状态
    app.state.settings = settings
    app.state.run_manager = run_manager
    app.state.workflow_dir = workflow_dir
    app.state.base_dir = base_dir

    # ------------------------------------------------------------------
    # 中间件
    # ------------------------------------------------------------------
    for mw_cls, mw_kwargs in create_security_middleware(settings):
        app.add_middleware(mw_cls, **mw_kwargs)

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    app.include_router(create_workflow_routes(workflow_dir))
    app.include_router(create_run_routes(run_manager, workflow_dir))
    app.include_router(create_artifact_routes(base_dir))

    # SSE 路由（单独注册，需 run_manager 注入）
    _register_sse_route(app, run_manager)

    # ------------------------------------------------------------------
    # 健康检查（不鉴权，便于容器探活）
    # ------------------------------------------------------------------
    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # 静态前端（可视化控制台，挂载于 /；注册在最后，不影响 API 路由匹配）
    # ------------------------------------------------------------------
    _mount_static_frontend(app)

    return app


def _mount_static_frontend(app) -> None:
    """把 ``server/static/`` 下的可视化前端挂载到 ``/``。

    懒加载 starlette.staticfiles；目录不存在时跳过（如裁剪安装），
    不影响 API 可用性。``html=True`` 使 ``/`` 直接返回 ``index.html``。

    另将 ``agentkit/assets/fonts/`` 挂载到 ``/fonts``，供前端
    ``@font-face`` 加载内嵌字体（OPPO Sans）。``/fonts`` 必须在 ``/``
    之前注册，否则会被 ``/`` catch-all 吞掉。
    """
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    if not os.path.isdir(static_dir):
        logger.warning("静态前端目录不存在，跳过挂载: %s", static_dir)
        return
    try:
        from starlette.staticfiles import StaticFiles
    except ImportError:
        logger.warning("starlette.staticfiles 不可用，跳过静态前端挂载")
        return

    # 字体目录: agentkit/assets/fonts/ (相对于 server/)
    fonts_dir = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "assets", "fonts",
    ))
    if os.path.isdir(fonts_dir):
        app.mount("/fonts", StaticFiles(directory=fonts_dir), name="fonts")
    else:
        logger.warning("字体目录不存在，跳过 /fonts 挂载: %s", fonts_dir)

    app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")


def _register_sse_route(app, run_manager) -> None:
    """注册 SSE 事件流端点 ``GET /api/runs/{run_id}/events``。

    支持 ``Last-Event-ID`` 头续传。
    """
    from fastapi import HTTPException, Request

    from agentkit.server.sse import create_sse_response

    @app.get("/api/runs/{run_id}/events", tags=["sse"])
    async def stream_run_events(run_id: str, request: Request):
        # Last-Event-ID 头（sse-starlette 约定）
        last_event_id_str = request.headers.get("last-event-id", "0")
        try:
            last_event_id = int(last_event_id_str)
        except ValueError:
            last_event_id = 0

        # 校验 run 是否存在（checkpoint 存在或内存活跃）
        handle = run_manager.get(run_id)
        if handle is None:
            # 不在内存，查 checkpoint
            cp = await run_manager._checkpoint_store.load(run_id)
            if cp is None:
                raise HTTPException(
                    status_code=404, detail=f"run {run_id!r} 不存在"
                )

        return create_sse_response(run_id, run_manager, last_event_id)
