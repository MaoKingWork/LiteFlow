"""agentkit.server —— 可视化服务层（P1）。

本子包实现基于 FastAPI 的 HTTP 可视化服务，提供工作流 CRUD、run 控制、
SSE 事件流、产物下载等能力。模块顶层不 import fastapi（懒加载），仅在
``agentkit[server]`` extra 安装 fastapi/uvicorn/sse-starlette 后可用。

依赖方向（对齐 ``docs/p1-implementation-plan.md`` 附录）：
    - ``core/`` 不 import ``server/``
    - ``runtime/`` 不 import ``server/``
    - ``server/`` 模块顶层不 import fastapi（懒加载在函数内）
    - ``server/`` 模块不用 ``from __future__ import annotations``
      （Pydantic + FastAPI 局部类解析陷阱）
"""
