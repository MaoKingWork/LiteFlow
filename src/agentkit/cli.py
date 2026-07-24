"""cli —— AgentKit 命令行入口。

提供五个子命令:
    - ``run``:       运行工作流 YAML
    - ``validate``:  静态校验工作流 YAML
    - ``dry-run``:   生成执行计划(不实际运行)
    - ``mcp``:       MCP Server 健康检查
    - ``serve``:     启动可视化服务(FastAPI + SSE)

使用 argparse(标准库),无额外依赖。

用法示例::

    agentkit run workflow.yaml --input date=2024-01-01
    agentkit validate workflow.yaml
    agentkit dry-run workflow.yaml
    agentkit mcp health-check
    agentkit serve --dir ./workflows --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

# 延迟导入 agentkit 模块,避免 --help 时触发全部依赖
__all__ = ["main"]


def _parse_inputs(input_list: list[str] | None) -> dict[str, Any]:
    """解析 ``--input KEY=VALUE`` 参数为 dict。

    VALUE 尝试 JSON 解析(支持 int / float / bool / list / dict),
    失败时作为原始字符串。
    """
    if not input_list:
        return {}
    result: dict[str, Any] = {}
    for item in input_list:
        if "=" not in item:
            logging.warning("忽略无法解析的 --input 参数: %s", item)
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        # 尝试 JSON 解析 value
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass  # 保留原始字符串
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def _cmd_validate(args: argparse.Namespace) -> int:
    """校验工作流 YAML。"""
    try:
        import yaml
    except ImportError:
        print("错误: 需要 PyYAML。请运行 pip install pyyaml", file=sys.stderr)
        return 1

    from agentkit.yaml.validator import validate_workflow

    with open(args.yaml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        print(f"错误: {args.yaml_path} 顶层应为 dict", file=sys.stderr)
        return 1

    report = validate_workflow(config)
    print(report)

    return 0 if report.is_valid else 1


def _cmd_dry_run(args: argparse.Namespace) -> int:
    """生成执行计划。"""
    from agentkit.yaml.loader import load_workflow

    wf = load_workflow(args.yaml_path)
    inputs = _parse_inputs(args.input)
    plan = wf.dry_run(inputs)

    print(f"工作流: {plan['workflow_name']}")
    print(f"输入变量: {', '.join(plan['inputs']) or '(无)'}")
    print(f"Step 总数: {plan['total_steps']}")
    print("-" * 60)
    for i, step in enumerate(plan["steps"]):
        print(
            f"  {i + 1}. [{step['type']}] {step['id']}"
            + (f" → output: {step['output']}" if step["output"] else "")
        )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """运行工作流。"""
    import logging

    from agentkit.core.trace_summary import TraceSummary
    from agentkit.yaml.loader import load_workflow

    # --verbose: 开启 DEBUG 级别日志(httpx 请求 / MCP 细节 / 内部 debug)
    # 默认(default_hooks_enabled=True)已装配 LoggingHooks 输出 INFO 级摘要,
    # --verbose 在此基础上叠加标准库 logging DEBUG,便于排查网络/协议问题。
    if getattr(args, "verbose", False):
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    # 可观测性 hooks(LoggingHooks + TokenAccountingHooks)由 Workflow 的
    # auto_hooks 自动装配,无需 --verbose 即可输出日志与 token 计量。
    wf = load_workflow(args.yaml_path)
    inputs = _parse_inputs(args.input)

    async def _run() -> int:
        if args.resume:
            result = await wf.resume(args.resume)
        else:
            result = await wf.run(inputs=inputs)

        print(f"\n运行 ID: {result.run_id}")
        print(f"状态: {result.status}")
        print(f"已完成 Step: {', '.join(result.completed_steps)}")

        # 可观测性汇总:step 级耗时 / token 用量 / 失败原因链
        print("\n" + TraceSummary.from_context(result.context).to_text())

        if result.status == "failed":
            print(f"错误: {result.error}", file=sys.stderr)
            print(f"\n恢复命令: agentkit run {args.yaml_path} --resume {result.run_id}")
            return 1

        # 打印上下文输出
        print("\n输出:")
        for key in sorted(result.context.keys()):
            value = result.context.get(key)
            value_str = json.dumps(value, ensure_ascii=False, default=str)
            if len(value_str) > 200:
                value_str = value_str[:200] + "..."
            print(f"  {key}: {value_str}")
        return 0

    return asyncio.run(_run())


def _cmd_mcp(args: argparse.Namespace) -> int:
    """MCP Server 健康检查。"""
    from agentkit.mcp.manager import MCPManager, MCPServerConfig

    if args.subcmd == "health-check":
        # 从 YAML 中读取 mcp_servers 配置
        if args.yaml_path:
            try:
                import yaml
            except ImportError:
                print("错误: 需要 PyYAML", file=sys.stderr)
                return 1
            with open(args.yaml_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            mcp_configs = [
                MCPServerConfig.from_dict(c)
                for c in config.get("mcp_servers", [])
            ]
        else:
            print("请通过 --yaml-path 指定工作流 YAML 文件")
            return 1

        if not mcp_configs:
            print("未配置 MCP Server")
            return 0

        manager = MCPManager(configs=mcp_configs)

        async def _check() -> int:
            results = await manager.connect_all()
            try:
                for r in results:
                    status = "已连接" if r.connected else "连接失败"
                    print(f"  {r.name}: {status}")
                    if r.error:
                        print(f"    错误: {r.error}")
                    if r.tools:
                        print(f"    工具({len(r.tools)}): {', '.join(r.tools[:5])}")
                        if len(r.tools) > 5:
                            print(f"    ... 共 {len(r.tools)} 个")
                    if r.resources:
                        print(f"    资源({len(r.resources)}): {', '.join(r.resources[:3])}")
            finally:
                await manager.close_all()
            return 0

        return asyncio.run(_check())

    print(f"未知 MCP 子命令: {args.subcmd}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# serve 子命令
# ---------------------------------------------------------------------------
def _cmd_serve(args: argparse.Namespace) -> int:
    """启动可视化服务(FastAPI + uvicorn)。

    流程:
        1. 把 CLI 参数覆盖写入 config(``server_host`` / ``server_port`` /
           ``server_token`` / ``server_cors_origins``)
        2. 懒加载 :func:`agentkit.server.app.create_app` 与 ``uvicorn``
        3. 构造 app + 从 config 读取最终 settings,启动 uvicorn

    Args:
        args: argparse Namespace,含 ``dir`` / ``host`` / ``port`` / ``token`` /
              ``cors_origins``。

    Returns:
        int: 退出码。``1`` 表示依赖缺失或启动失败;``0`` 表示正常退出。
    """
    from agentkit.config import set_default

    # 覆盖 config(仅覆盖显式指定的参数,None 保留 config 默认)
    if args.host is not None:
        set_default("server_host", args.host)
    if args.port is not None:
        set_default("server_port", args.port)
    if args.token is not None:
        set_default("server_token", args.token)
    if args.cors_origins:
        set_default("server_cors_origins", list(args.cors_origins))

    # 懒加载 server.app
    try:
        from agentkit.server.app import create_app
    except ImportError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    # 懒加载 uvicorn
    try:
        import uvicorn
    except ImportError:
        print(
            "错误: 需要 uvicorn: pip install agentkit[server]",
            file=sys.stderr,
        )
        return 1

    from agentkit.server.settings import ServerSettings

    app = create_app(args.dir)
    settings = ServerSettings.from_config()
    # uvicorn.run 阻塞直到进程退出;host/port 以 config 最终值为准
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。

    Args:
        argv: 命令行参数列表(不含程序名);``None`` 时用 ``sys.argv[1:]``。

    Returns:
        int: 进程退出码,0 表示成功。
    """
    parser = argparse.ArgumentParser(
        prog="agentkit",
        description="AgentKit CLI — 轻量化智能体框架命令行工具",
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # run
    p_run = subparsers.add_parser("run", help="运行工作流")
    p_run.add_argument("yaml_path", help="工作流 YAML 文件路径")
    p_run.add_argument(
        "--input", "-i", action="append", default=[],
        help="输入变量 KEY=VALUE(可多次指定)",
    )
    p_run.add_argument("--resume", default=None, help="从检查点恢复(run_id)")
    p_run.add_argument("--verbose", "-v", action="store_true", help="详细日志输出")

    # validate
    p_val = subparsers.add_parser("validate", help="校验工作流 YAML")
    p_val.add_argument("yaml_path", help="工作流 YAML 文件路径")

    # dry-run
    p_dry = subparsers.add_parser("dry-run", help="生成执行计划(不实际运行)")
    p_dry.add_argument("yaml_path", help="工作流 YAML 文件路径")
    p_dry.add_argument(
        "--input", "-i", action="append", default=[],
        help="输入变量 KEY=VALUE(可多次指定)",
    )

    # mcp
    p_mcp = subparsers.add_parser("mcp", help="MCP Server 管理")
    p_mcp.add_argument("subcmd", choices=["health-check"], help="MCP 子命令")
    p_mcp.add_argument("--yaml-path", default=None, help="工作流 YAML 文件路径")

    # serve
    p_serve = subparsers.add_parser("serve", help="启动可视化服务")
    p_serve.add_argument(
        "--dir", default=".", help="工作流 YAML 目录(默认当前目录)"
    )
    p_serve.add_argument(
        "--host", default=None, help="绑定地址(默认 127.0.0.1)"
    )
    p_serve.add_argument(
        "--port", type=int, default=None, help="端口(默认 8000)"
    )
    p_serve.add_argument(
        "--token", default=None, help="鉴权 bearer token(为空时仅允许本地访问)"
    )
    p_serve.add_argument(
        "--cors-origins",
        action="append",
        default=[],
        help="CORS 允许的 origin(可多次指定;不指定则关闭 CORS)",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "run":
        return _cmd_run(args)
    elif args.command == "validate":
        return _cmd_validate(args)
    elif args.command == "dry-run":
        return _cmd_dry_run(args)
    elif args.command == "mcp":
        return _cmd_mcp(args)
    elif args.command == "serve":
        return _cmd_serve(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
