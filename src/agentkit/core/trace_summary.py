"""trace_summary —— StepTrace 汇总与可视化。

把工作流执行产生的 :class:`~agentkit.steps.base.StepTrace` 列表渲染为
结构化、可读的汇总信息,覆盖三类可观测性诉求:

1. **Step 级耗时表**:每个 Step 的 status / duration_ms / token 用量。
2. **Token 汇总**:整条工作流的 token 总量(从 trace 累计)。
3. **失败原因链**:所有失败 Step 的 id 与 error,便于根因定位。

设计原则:
    - 纯函数式渲染,不依赖运行期状态;输入 traces 列表即可。
    - 同时提供文本(``to_text``,Markdown 表格)与结构化(``to_dict``)两种输出,
      满足"人读"与"程序消费"两种场景。
    - ``duration_ms`` 为 ``None`` 时显示 ``N/A``,明确区分"未计时"与"0ms"。

公开 API:
    - TraceSummary: StepTrace 汇总渲染器
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentkit.core.context import Context
    from agentkit.steps.base import StepTrace

__all__ = ["TraceSummary"]


def _fmt_duration(ms: float | None) -> str:
    """格式化耗时:None → ``N/A``;否则保留一位小数并附 ``ms``。"""
    if ms is None:
        return "N/A"
    return f"{ms:.1f}ms"


class TraceSummary:
    """StepTrace 列表的汇总渲染器。

    聚合一组 :class:`~agentkit.steps.base.StepTrace`,提供耗时表、token 汇总
    与失败原因链的可读输出。可从 :class:`~agentkit.core.context.Context`
    直接构建,也可从裸 traces 列表构建。

    Args:
        traces: StepTrace 列表(通常来自 ``ctx.get_traces()``)。

    用法::

        summary = TraceSummary.from_context(ctx)
        print(summary.to_text())   # Markdown 表格
        data = summary.to_dict()    # 结构化
    """

    def __init__(self, traces: "list[StepTrace]") -> None:
        self._traces: "list[StepTrace]" = list(traces)

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------
    @classmethod
    def from_context(cls, ctx: "Context") -> "TraceSummary":
        """从 Context 构建汇总(取其全部 traces)。"""
        return cls(ctx.get_traces())

    @classmethod
    def from_traces(cls, traces: "list[StepTrace]") -> "TraceSummary":
        """从裸 traces 列表构建汇总。"""
        return cls(traces)

    # ------------------------------------------------------------------
    # 结构化输出
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """返回结构化汇总。

        Returns:
            dict: 包含以下键:
                - ``steps``: 每个 Step 的 ``{id, status, duration_ms, tokens, error}``
                - ``totals``: ``{step_count, success, failure, total_duration_ms, total_tokens}``
                - ``failures``: 失败 Step 的 ``{id, error}`` 列表
        """
        steps: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        total_duration: float = 0.0
        total_tokens: int = 0
        success_count = 0
        failure_count = 0

        for t in self._traces:
            duration = t.duration_ms
            tokens = t.token_usage or 0
            if duration is not None:
                total_duration += duration
            total_tokens += tokens
            if t.status == "success":
                success_count += 1
            else:
                failure_count += 1
                failures.append({"id": t.step_id, "error": t.error or ""})
            steps.append(
                {
                    "id": t.step_id,
                    "status": t.status,
                    "duration_ms": duration,
                    "tokens": tokens,
                    "error": t.error,
                }
            )

        return {
            "steps": steps,
            "totals": {
                "step_count": len(self._traces),
                "success": success_count,
                "failure": failure_count,
                "total_duration_ms": total_duration,
                "total_tokens": total_tokens,
            },
            "failures": failures,
        }

    # ------------------------------------------------------------------
    # 文本输出(Markdown 表格)
    # ------------------------------------------------------------------
    def to_text(self) -> str:
        """渲染为 Markdown 文本汇总。

        包含:Step 耗时表、Token 与耗时总计、失败原因链(若有)。
        """
        data = self.to_dict()
        totals = data["totals"]
        lines: list[str] = []

        # Step 耗时表
        lines.append("## Step 执行汇总")
        lines.append("")
        lines.append("| Step | 状态 | 耗时 | Tokens |")
        lines.append("|------|------|------|--------|")
        for s in data["steps"]:
            lines.append(
                f"| {s['id']} | {s['status']} | "
                f"{_fmt_duration(s['duration_ms'])} | {s['tokens']} |"
            )
        lines.append("")

        # 总计
        lines.append(
            f"- Step 数:{totals['step_count']}"
            f"(成功 {totals['success']} / 失败 {totals['failure']})"
        )
        lines.append(f"- 总耗时:{_fmt_duration(totals['total_duration_ms'])}")
        lines.append(f"- 总 Tokens:{totals['total_tokens']}")
        lines.append("")

        # 失败原因链
        if data["failures"]:
            lines.append("## 失败原因链")
            lines.append("")
            for f in data["failures"]:
                lines.append(f"- **{f['id']}**:{f['error']}")
            lines.append("")

        return "\n".join(lines)

    # 便捷:直接 print
    def print(self) -> None:
        """打印文本汇总到 stdout。"""
        print(self.to_text())
