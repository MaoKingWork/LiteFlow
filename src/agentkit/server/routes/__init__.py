"""server.routes —— FastAPI 路由层。

各子模块懒加载 fastapi,未安装 ``agentkit[server]`` extra 时
``import agentkit.server.routes`` 仍可成功,仅在调用工厂函数时报错。

子模块:
    - workflows:  工作流 CRUD + 校验 + 内省
    - runs:       run CRUD + cancel + resume
    - artifacts:  产物清单 + 下载 + GCSweeper 调度
"""
