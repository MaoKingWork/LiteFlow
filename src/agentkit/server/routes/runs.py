"""server.routes.runs —— run CRUD + cancel + resume（E2）。

端点:
    POST /api/workflows/{name}/runs       启动 run
    GET  /api/runs?workflow=              run 列表
    GET  /api/runs/{run_id}               状态 + traces + 产物清单
    POST /api/runs/{run_id}/cancel?mode=  中断
    POST /api/runs/{run_id}/resume        恢复

设计原则:
    - 懒加载 fastapi
    - 从 run_manager 获取 base_dir / checkpoint_store
    - traces 从 EventLog STEP_FINISHED 事件提取
    - artifacts 复用 artifacts.list_artifacts_from_log

注意:本模块 **不用** ``from __future__ import annotations``
（Pydantic + FastAPI 局部类解析陷阱）。
"""

import json
import os
from dataclasses import asdict

from agentkit.runtime.event import EventLog, EventType
from agentkit.server.routes.artifacts import list_artifacts_from_log

__all__ = ["create_run_routes"]


def create_run_routes(run_manager, workflow_dir: str, prefix: str = "/api"):
    """创建 run 路由。

    Args:
        run_manager:   RunManager 实例。
        workflow_dir:  工作流 YAML 文件目录。
        prefix:        URL 前缀,默认 ``/api``。
    """
    try:
        from fastapi import APIRouter, HTTPException, Request
    except ImportError as e:
        raise ImportError(
            "Server 需要 fastapi: pip install agentkit[server]"
        ) from e

    router = APIRouter(prefix=prefix, tags=["runs"])
    base_dir = getattr(run_manager, "_base_dir", "output/runs")
    checkpoint_store = run_manager._checkpoint_store

    # ------------------------------------------------------------------
    # POST /workflows/{name}/runs —— 启动 run
    # ------------------------------------------------------------------
    @router.post("/workflows/{name}/runs")
    async def start_run(name: str, request: Request):
        """启动指定工作流的 run。"""
        import yaml
        from agentkit.yaml.loader import load_workflow_from_dict

        filepath = os.path.join(workflow_dir, f"{name}.yaml")
        if not os.path.exists(filepath):
            raise HTTPException(
                status_code=404,
                detail=f"工作流 {name!r} 不存在",
            )

        with open(filepath, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if not isinstance(config, dict):
            raise HTTPException(
                status_code=400,
                detail="工作流文件解析失败",
            )

        try:
            workflow = load_workflow_from_dict(config, base_dir=workflow_dir)
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"工作流编译失败: {e}",
            )

        # 解析请求体
        body = await request.body()
        inputs = None
        run_id = None
        if body:
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    inputs = data.get("inputs")
                    run_id = data.get("run_id")
            except (json.JSONDecodeError, ValueError):
                pass

        try:
            result_run_id = await run_manager.start(
                workflow, inputs=inputs, run_id=run_id
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"启动 run 失败: {e}",
            )

        return {"run_id": result_run_id}

    # ------------------------------------------------------------------
    # GET /runs —— run 列表
    # ------------------------------------------------------------------
    @router.get("/runs")
    async def list_runs(workflow: str = None):
        """返回 run 列表,可选按 workflow 名称过滤。"""
        summaries = await run_manager.list_runs(workflow)
        return {"runs": [asdict(s) for s in summaries]}

    # ------------------------------------------------------------------
    # GET /runs/{run_id} —— run 详情
    # ------------------------------------------------------------------
    @router.get("/runs/{run_id}")
    async def get_run_detail(run_id: str):
        """返回 run 状态 + traces + 产物清单。"""
        checkpoint = await checkpoint_store.load(run_id)
        if checkpoint is None:
            raise HTTPException(
                status_code=404,
                detail=f"run {run_id!r} 不存在",
            )

        # 从事件日志提取 traces（STEP_FINISHED 事件）
        log = EventLog(run_id, base_dir=base_dir)
        traces = []
        for event in log.read_from():
            if event.type == EventType.STEP_FINISHED:
                traces.append({
                    "step_id": event.step_id,
                    "payload": event.payload,
                })

        # 产物清单
        artifacts = list_artifacts_from_log(run_id, base_dir)

        return {
            "run_id": run_id,
            "workflow_name": checkpoint.workflow_name,
            "status": checkpoint.status,
            "completed_steps": checkpoint.completed_steps,
            "started_at": checkpoint.started_at,
            "updated_at": checkpoint.updated_at,
            "error": checkpoint.error,
            "traces": traces,
            "artifacts": artifacts,
        }

    # ------------------------------------------------------------------
    # POST /runs/{run_id}/cancel —— 中断
    # ------------------------------------------------------------------
    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, mode: str = "graceful"):
        """中断指定 run。mode: graceful（默认）/ immediate。"""
        try:
            await run_manager.cancel(run_id, mode=mode)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"run {run_id!r} 不存在或已结束",
            )
        return {"run_id": run_id, "cancelled": True, "mode": mode}

    # ------------------------------------------------------------------
    # POST /runs/{run_id}/resume —— 恢复
    # ------------------------------------------------------------------
    @router.post("/runs/{run_id}/resume")
    async def resume_run(run_id: str):
        """恢复 interrupted / failed 的 run。"""
        try:
            new_run_id = await run_manager.resume(run_id)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"run {run_id!r} 不存在",
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=str(e),
            )
        return {"run_id": new_run_id, "resumed_from": run_id}

    return router
