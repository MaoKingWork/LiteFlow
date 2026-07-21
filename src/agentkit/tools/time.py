"""tools.time —— 时间获取工具。

提供 ``TimeNowTool``,通过 ``time.now`` 注册到全局 ToolRegistry。

设计要点:
    - 零依赖:仅用标准库 ``datetime`` 与 ``zoneinfo``(Python 3.9+ 内置)。
    - 解决 LLM 时间幻觉:YAML 工作流无需外部传入 ``date``,首步取当前时间
      作为下游所有 Step 的基准。
    - 支持时区 / 格式 / 时间偏移:覆盖"今天 / 昨天 / 上周一 / 月初"等常见场景。
    - 时区解析优先级:参数 ``timezone`` > 环境变量 ``TIME_TIMEZONE`` > UTC。
    - 格式化遵循 ``strftime`` 语法;默认 ISO 8601(``YYYY-MM-DDTHH:MM:SS+TZ``)。

平台时区数据:
    - Linux / macOS:系统 ``/usr/share/zoneinfo`` 提供 IANA 时区库,开箱即用。
    - Windows:系统无 IANA 时区库,需安装 ``tzdata`` 包(``pip install tzdata``)。
    - UTC 兜底:任何平台上 ``"UTC"`` 都用 ``datetime.timezone.utc`` 实现,
      保证默认行为零依赖可用;其他 IANA 时区在 Windows 上需要 ``tzdata``。

返回结构::

    {
        "datetime":   "2024-01-01T12:00:00+08:00",  # ISO 8601
        "timestamp":  1704067200.0,                 # Unix 时间戳
        "formatted":  "2024-01-01",                  # 按 format 参数格式化
        "timezone":   "Asia/Shanghai",               # 实际生效时区
        "offset_days":   0,                          # 偏移天数(回显)
        "offset_hours":  0,                          # 偏移小时(回显)
    }
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from agentkit.tools.base import Tool, tool

if TYPE_CHECKING:
    from agentkit.core.context import Context


# UTC 别名:任何平台始终可用,作为系统无 tzdata 时的兜底
_UTC_ALIASES = {"utc", "z", "gmt", "ulc"}


class TimeNowParams(BaseModel):
    """时间获取参数。"""

    timezone: str | None = Field(
        None,
        description="时区名(IANA 标识,如 'Asia/Shanghai' / 'UTC');"
        "默认读 TIME_TIMEZONE 环境变量,未设置则用 UTC",
    )
    format: str | None = Field(
        None,
        description="strftime 格式串(如 '%Y-%m-%d' / '%Y%m%d');"
        "默认 ISO 8601(返回 datetime 字段)",
    )
    offset_days: int = Field(0, description="天数偏移(可负,如 -1 表示昨天)")
    offset_hours: int = Field(0, description="小时偏移(可负)")
    offset_minutes: int = Field(0, description="分钟偏移(可负)")


@tool("time.now", role="action")
class TimeNowTool(Tool):
    """时间获取工具。

    在调用时计算当前时间(可加偏移),按指定时区与格式返回。常用场景:

        - 工作流首步取"今天"作为后续 SQL / 报告的日期基准
        - 取"昨天" / "上周一" / "月初"等相对日期
        - 跨时区对齐(默认 UTC,中国场景建议设 ``TIME_TIMEZONE=Asia/Shanghai``)

    时区解析优先级:

        1. 参数 ``timezone``(最高)
        2. 环境变量 ``TIME_TIMEZONE``
        3. UTC(兜底)

    时区无效时抛 ``ValueError``,交由 ToolStep retry 处理。
    """

    description = "获取当前时间(支持时区与偏移),返回 datetime / timestamp / formatted"

    @property
    def param_model(self) -> type[BaseModel]:
        return TimeNowParams

    @staticmethod
    def _resolve_tz(timezone: str | None) -> tuple[str, Any]:
        """解析生效时区,返回 (时区名, tzinfo 对象)。

        优先级:显式参数 > ``TIME_TIMEZONE`` 环境变量 > ``"UTC"``。

        实现细节:

            - ``"UTC"`` 及常见别名(``"Z"`` / ``"GMT"`` 等)始终用
              ``datetime.timezone.utc``,**不依赖** ``tzdata`` 包,
              保证任何平台默认行为可用。
            - 其他 IANA 时区用 ``ZoneInfo`` 解析;在 Windows 上若无
              ``tzdata`` 包,抛带安装提示的 ``ValueError``。

        Args:
            timezone: 调用方显式传入的时区名,``None`` 表示走环境变量。

        Returns:
            tuple: ``(tz_name, tzinfo)``。``tzinfo`` 为 ``tzinfo`` 子类实例。

        Raises:
            ValueError: 时区名无法解析(附安装提示)。
        """
        tz_name = timezone or os.environ.get("TIME_TIMEZONE") or "UTC"

        # UTC 短路:不依赖 tzdata,任何平台零依赖可用
        if tz_name.lower() in _UTC_ALIASES:
            return tz_name, dt_timezone.utc

        try:
            return tz_name, ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as e:
            raise ValueError(
                f"无法解析时区 {tz_name!r}。可能原因:(1) 时区名拼写错误,"
                f"应为 IANA 标识如 'Asia/Shanghai' / 'America/Los_Angeles';"
                f"(2) Windows 系统缺 IANA 时区库,请执行"
                f" `pip install tzdata` 后重试。"
            ) from e

    async def call(self, params: dict, ctx: "Context") -> dict:
        """获取当前时间。

        Args:
            params: ``TimeNowParams`` 对应的 dict。
            ctx:    会话上下文(只读,本工具未使用)。

        Returns:
            dict: ``{"datetime", "timestamp", "formatted", "timezone",
                   "offset_days", "offset_hours", "offset_minutes"}``。

        Raises:
            ValueError: 时区名无法解析。
        """
        tz_name, tz = self._resolve_tz(params.get("timezone"))

        # 以指定时区"现在"为基准,再叠加偏移
        now = datetime.now(tz=tz)
        offset = timedelta(
            days=params.get("offset_days", 0),
            hours=params.get("offset_hours", 0),
            minutes=params.get("offset_minutes", 0),
        )
        result = now + offset

        fmt = params.get("format")
        formatted = result.strftime(fmt) if fmt else result.isoformat()

        return {
            "datetime": result.isoformat(),
            "timestamp": result.timestamp(),
            "formatted": formatted,
            "timezone": tz_name,
            "offset_days": params.get("offset_days", 0),
            "offset_hours": params.get("offset_hours", 0),
            "offset_minutes": params.get("offset_minutes", 0),
        }
