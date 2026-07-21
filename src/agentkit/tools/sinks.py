"""tools.sinks —— 数据沉淀工具(WeCom / Webhook)。

提供两个 Sink 角色工具:

    - ``sink.wecom``:  企业微信群机器人消息发送(WeComSink)
    - ``sink.webhook``:通用 webhook 发送(WebhookSink)

设计要点:
    - 均基于 ``httpx.AsyncClient``,与 ``http`` 工具一致。
    - HTTP 错误(4xx / 5xx)不抛异常,返回 ``ok=False`` 交由上层决策;
      网络错误抛 httpx 异常,交由 ToolStep 的 retry 机制处理。
    - WeComSink 的 webhook 可由参数传入或读 ``WECOM_WEBHOOK`` 环境变量,
      缺失抛 ``ValueError``。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from agentkit.tools.base import Tool, tool

if TYPE_CHECKING:
    from agentkit.core.context import Context


# ---------------------------------------------------------------------------
# 企业微信群机器人
# ---------------------------------------------------------------------------
class WeComParams(BaseModel):
    """企微机器人消息参数。"""

    webhook: str | None = Field(
        None, description="企微机器人 webhook URL(默认读 WECOM_WEBHOOK 环境变量)"
    )
    content: str = Field(..., description="消息内容(text)")
    msgtype: str = Field("text", description="消息类型(text|markdown)")


@tool("sink.wecom", role="sink")
class WeComSink(Tool):
    """企业微信群机器人消息发送。

    POST 到 webhook,body 形如::

        - text:     {"msgtype": "text",     "text":     {"content": ...}}
        - markdown: {"msgtype": "markdown", "markdown": {"content": ...}}

    返回 ``{"ok": bool, "response": <企微返回 json>}``。
    ``ok`` 由企微返回的 ``errcode`` 字段决定(``errcode == 0`` 即成功)。

    webhook 缺失抛 ``ValueError``。
    """

    description = "发送企业微信机器人消息"

    @property
    def param_model(self) -> type[BaseModel]:
        return WeComParams

    async def call(self, params: dict, ctx: "Context") -> dict:
        """发送企微消息。

        Args:
            params: ``WeComParams`` 对应的 dict,``content`` 必填。
            ctx:    会话上下文(只读,本工具未使用)。

        Returns:
            dict: ``{"ok": bool, "response": <企微返回>}``。

        Raises:
            ValueError: webhook 既未传入也未配置 ``WECOM_WEBHOOK``。
            httpx.HTTPError: 网络层错误,交由 retry。
        """
        webhook = params.get("webhook") or os.environ.get("WECOM_WEBHOOK")
        if not webhook:
            raise ValueError(
                "缺少 WECOM_WEBHOOK:请传入 webhook 参数或设置 WECOM_WEBHOOK 环境变量"
            )

        content = params["content"]
        msgtype = params.get("msgtype") or "text"
        # 仅允许 text / markdown,其他统一按 text 处理
        if msgtype not in ("text", "markdown"):
            msgtype = "text"
        body = {"msgtype": msgtype, msgtype: {"content": content}}

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook, json=body)

        # 解析企微返回(errcode / errmsg)
        try:
            resp_json: Any = response.json()
        except Exception:
            resp_json = {"raw": response.text}

        ok = isinstance(resp_json, dict) and resp_json.get("errcode", 0) == 0
        return {"ok": ok, "response": resp_json}


# ---------------------------------------------------------------------------
# 通用 webhook
# ---------------------------------------------------------------------------
class WebhookParams(BaseModel):
    """通用 webhook 参数。"""

    url: str = Field(..., description="webhook URL")
    content: Any = Field(..., description="请求体")
    method: str = Field("POST", description="HTTP 方法")
    headers: dict[str, str] | None = Field(None, description="请求头")


@tool("sink.webhook", role="sink")
class WebhookSink(Tool):
    """通用 webhook 发送。

    以 ``content`` 为 JSON body,POST 到 ``url``(method 可配置)。
    返回 ``{"ok": bool, "status_code": int}``。

    错误处理:

        - HTTP 错误(4xx / 5xx)不抛异常,返回 ``ok=False``
        - 网络错误抛 ``httpx`` 异常,交由 retry 处理
    """

    description = "发送通用 webhook"

    @property
    def param_model(self) -> type[BaseModel]:
        return WebhookParams

    async def call(self, params: dict, ctx: "Context") -> dict:
        """发送 webhook。

        Args:
            params: ``WebhookParams`` 对应的 dict,``url`` / ``content`` 必填。
            ctx:    会话上下文(只读,本工具未使用)。

        Returns:
            dict: ``{"ok": bool, "status_code": int}``。

        Raises:
            httpx.HTTPError: 网络层错误,交由 retry。
        """
        url: str = params["url"]
        content = params.get("content")
        method = str(params.get("method") or "POST").upper()
        headers = params.get("headers")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method, url, json=content, headers=headers
            )

        return {
            "ok": response.status_code < 400,
            "status_code": response.status_code,
        }
