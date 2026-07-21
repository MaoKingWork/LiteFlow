"""cli —— AgentKit 命令行入口。

提供四个子命令:
    - ``run``:       运行工作流 YAML
    - ``validate``:  静态校验工作流 YAML
    - ``dry-run``:   生成执行计划(不实际运行)
    - ``mcp``:       MCP Server 健康检查

使用 argparse(标准库),无额外依赖。

用法示例::

    agentkit run workflow.yaml --input date=2024-01-01
    agentkit validate workflow.yaml
    agentkit dry-run workflow.yaml
    agentkit mcp health-check
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
    from agentkit.core.trace_summary import TraceSummary
    from agentkit.yaml.loader import load_workflow

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
        for key in sorted(result.context._data.keys()):
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

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
