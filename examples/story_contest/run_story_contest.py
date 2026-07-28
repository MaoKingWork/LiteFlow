"""LiteFlow 最佳实践示例 —— 故事创作大赛工作流运行器。

演示内容:
    - 从 YAML 加载工作流
    - 传入动态输入(主角名 + 故事类型)
    - 并行创作 + 并行评审 + 评分汇总 + PDF 导出
    - 流式输出(实时看到创作过程)
    - 运行结果展示

使用方法:
    # 1. 设置 MIMO API Key 环境变量
    #    Windows PowerShell:
    $env:MIMO_API_KEY = "sk-your-key-here"
    #    Linux / macOS:
    export MIMO_API_KEY="sk-your-key-here"

    # 2. 安装依赖(含 PDF 转换)
    uv pip install 'liteflow[doc-convert]'

    # 3. 运行
    uv run python examples/story_contest/run_story_contest.py

    # 自定义输入:
    uv run python examples/story_contest/run_story_contest.py --name "林远" --type "科幻"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 确保 src 在 Python 路径中(开发模式)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


async def main(protagonist: str, story_type: str) -> None:
    """加载并执行故事创作大赛工作流。"""
    from agentkit.yaml.loader import load_workflow

    # ─── 加载 YAML 工作流 ─────────────────────────────────────────────────
    yaml_path = Path(__file__).parent / "story_contest.yaml"
    print(f"\n{'='*60}")
    print(f"  LiteFlow 故事创作大赛")
    print(f"  主角: {protagonist}")
    print(f"  类型: {story_type}")
    print(f"{'='*60}\n")

    workflow = load_workflow(str(yaml_path))

    # ─── 执行工作流 ─────────────────────────────────────────────────────────
    result = await workflow.run(
        inputs={
            "protagonist": protagonist,
            "story_type": story_type,
        }
    )

    # ─── 输出结果 ───────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  工作流执行完成!")
    print(f"  状态: {result.status}")
    print(f"  完成步骤: {result.completed_steps}")
    print(f"{'='*60}\n")

    if result.status == "completed":
        ctx = result.context

        # 展示 AI 评分结果
        final_results = ctx.get("final_results")
        if final_results and isinstance(final_results, dict):
            print("📊 评分总览:")
            print("-" * 50)
            for item in final_results.get("results", []):
                style = item.get("style_cn", item.get("style", "?"))
                score = item.get("final_score", 0)
                print(f"  {style}: {score} 分")
            print("-" * 50)
            winner_cn = final_results.get("winner_cn", "?")
            winner_score = final_results.get("winner_final_score", 0)
            print(f"\n🏆 冠军: {winner_cn} ({winner_score} 分)")
            print(f"   体系: {final_results.get('scoring_system', '')})")

        # 展示 PDF 输出路径
        pdf_result = ctx.get("pdf_result")
        if pdf_result and isinstance(pdf_result, dict):
            files = pdf_result.get("files", [])
            for f in files:
                print(f"\n📄 已生成 {f.get('format', '').upper()}: {f.get('path', '')}")
                print(f"   大小: {f.get('size', 0) / 1024:.1f} KB")
    else:
        print(f"❌ 工作流执行失败: {result.error}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LiteFlow 故事创作大赛")
    parser.add_argument(
        "--name", default="叶辰",
        help="主角名字 (默认: 叶辰)",
    )
    parser.add_argument(
        "--type", default="奇幻",
        help="故事类型 (默认: 奇幻)",
    )
    args = parser.parse_args()

    asyncio.run(main(args.name, args.type))
