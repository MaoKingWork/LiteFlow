"""报告生成引擎 × AgentKit 深度适配 —— 声明式用法演示入口。

演示完整链路:
    ReportEngine(LocalStorage) → ReportEngineTool(ArtifactStore) →
    YAML 声明式工作流 → 产物落盘 + 事件分发

运行方式:
    python examples/run_report_demo.py

无 LLM API Key 也能运行(纯 ToolStep 驱动)。LLM Function Call 路径见
本文件末尾 ``demo_llm_function_call`` 函数(需配置 API Key)。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 确保源码包可导入(src/ 布局)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from agentkit.core.hooks import CompositeHooks, LoggingHooks
from agentkit.runtime.artifact import ArtifactStore
from agentkit.runtime.event import EventBus, EventLog
from agentkit.runtime.event_hooks import EventBusHooks
from agentkit.yaml.loader import load_workflow
from report_engine_sdk import ReportEngine
from report_engine_sdk.adapters.agentkit import create_agentkit_tool
from report_engine_sdk.storage.local import LocalStorage

# SDK 自带 config/packs 路径(含 ops_report / work_report / teacher_eval 等示例)
SDK_CONFIG_DIR = str(_SRC_DIR / "report_engine_sdk" / "config")
YAML_PATH = str(Path(__file__).resolve().parent / "report_demo.yaml")
OUTPUT_DIR = str(_PROJECT_ROOT / "output" / "report_demo")


async def run_declarative_workflow() -> None:
    """声明式路径:加载 YAML → 注册工具 → 运行工作流。

    展示深度适配的核心能力:
        - ReportEngineTool 经 ``execution="thread"`` 卸载到子线程
        - ArtifactStore 自动把 preview 落盘 + 发 ARTIFACT_PRODUCED 事件
        - EventBusHooks 把工作流生命周期翻译为事件流
        - {{var}} 模板解析保留原始类型(数字 / 列表)
    """
    run_id = "report_demo_run"

    # 1. 构建 ReportEngine:LocalStorage 使渲染结果落盘为 file:// URI
    engine = ReportEngine(SDK_CONFIG_DIR, LocalStorage(OUTPUT_DIR))

    # 2. 构建运行时可视化层:EventLog(持久化) + EventBus(分发) + ArtifactStore(产物)
    log = EventLog(run_id, base_dir=OUTPUT_DIR)
    bus = EventBus(run_id, log=log)
    store = ArtifactStore(run_id, event_bus=bus, base_dir=OUTPUT_DIR)

    # 3. 注册 ReportEngineTool 到全局 ToolRegistry
    #    artifact_store 注入后,工具成功渲染时自动落盘 + 发事件
    create_agentkit_tool(engine, artifact_store=store)

    # 4. 订阅事件流,运行时打印 ARTIFACT_PRODUCED 事件
    sub = await bus.subscribe()

    # 5. 加载 YAML 工作流,挂载 EventBusHooks 把生命周期翻译为事件
    hooks = CompositeHooks([
        LoggingHooks(),
        EventBusHooks(bus, run_id),
    ])
    wf = load_workflow(YAML_PATH, hooks=hooks)

    # 6. 运行工作流
    inputs = {
        "service_name": "api-gateway",
        "uptime_pct": 99.95,
        "error_count": 3,
        "latency_ms": 45.2,
        "date_str": "2026-07-23",
    }
    print("=" * 60)
    print("报告生成引擎 × AgentKit 深度适配演示(声明式 YAML)")
    print("=" * 60)
    print(f"工作流: {wf.name}")
    print(f"输入: {json.dumps(inputs, ensure_ascii=False)}")
    print("-" * 60)

    result = await wf.run(inputs=inputs)

    # 7. 打印运行结果
    print(f"\n运行 ID: {result.run_id}")
    print(f"状态: {result.status}")
    print(f"已完成 Step: {', '.join(result.completed_steps)}")

    for key in ("health_report", "daily_briefing"):
        value = result.context.get(key)
        if not value:
            continue
        print(f"\n--- {key} ---")
        if "error" in value:
            print(f"  错误: {value['error']}")
            continue
        print(f"  file_uri: {value.get('file_uri')}")
        preview = value.get("preview", "")
        print(f"  preview:\n{_indent(preview, '    ')}")
        if "artifact" in value:
            art = value["artifact"]
            print(f"  artifact:")
            print(f"    step_id: {art['step_id']}")
            print(f"    uri: {art['uri']}")
            print(f"    size: {art['size']} bytes")
            print(f"    md5: {art['md5']}")

    # 8. 打印收到的事件
    print("\n--- 事件流 ---")
    events_received = 0
    while not sub._queue.empty() if hasattr(sub, "_queue") else True:
        try:
            event = await asyncio.wait_for(sub.get(), timeout=0.5)
        except asyncio.TimeoutError:
            break
        if event is None:
            break
        events_received += 1
        print(f"  [{event.type}] {event.payload.get('step_id', event.payload.get('workflow_name', ''))}")
    print(f"共收到 {events_received} 个事件")

    await bus.close()
    print("\n演示完成。产物目录:", OUTPUT_DIR)


def _indent(text: str, prefix: str) -> str:
    """按行缩进文本。"""
    return "\n".join(prefix + line for line in text.splitlines())


async def demo_llm_function_call() -> None:
    """LLM Function Call 路径演示(高上限)。

    LLM 自主决定调用 report.generate 工具生成报告,展示:
        - ReportEngineTool 的 param_model 自动生成 JSON Schema
        - LLMStep Function Call 循环
        - thread 卸载在 Function Call 路径同样生效

    需要 LLM API Key。未配置时跳过。
    """
    import os

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("\n[LLM Function Call 演示] 跳过:未设置 OPENAI_API_KEY / DEEPSEEK_API_KEY")
        return

    from agentkit.core.agent import AgentConfig
    from agentkit.llm.provider import get_provider
    from agentkit.steps.llm_step import LLMStep

    engine = ReportEngine(SDK_CONFIG_DIR, LocalStorage(OUTPUT_DIR))
    create_agentkit_tool(engine)

    provider = get_provider("deepseek" if os.environ.get("DEEPSEEK_API_KEY") else "openai")
    agent = AgentConfig(
        name="report_assistant",
        model=provider.model,
        provider=provider.name,
        system="你是运维报告助手。根据用户描述调用 report.generate 生成健康检查报告。",
        tools=["report.generate"],
        max_tool_iterations=3,
    )
    step = LLMStep(
        id="llm_report",
        agent=agent,
        prompt="服务 api-gateway 今日运行率 99.9%,错误数 2,延迟 30ms,日期 2026-07-23。请生成健康检查报告。",
        output="reply",
    )

    print("\n" + "=" * 60)
    print("LLM Function Call 路径演示")
    print("=" * 60)

    from agentkit.core.context import Context
    ctx = Context()
    trace = await step.execute(ctx)
    print(f"状态: {trace.status}")
    print(f"回复: {ctx.get('reply')}")
    print(f"工具调用: {trace.tool_calls}")


async def main() -> None:
    await run_declarative_workflow()
    await demo_llm_function_call()


if __name__ == "__main__":
    asyncio.run(main())
