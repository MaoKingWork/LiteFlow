"""tools.http —— HTTP 请求工具。

提供 ``HTTPTool``,通过 ``http.request`` 注册到全局 ToolRegistry。

设计要点:
    - 基于 ``httpx.AsyncClient``,支持 GET / POST / PUT / DELETE 等方法。
    - 错误处理边界清晰:HTTP 错误(4xx / 5xx)不抛异常,返回 ``ok=False``
      交由上层决策;网络错误(连接失败 / 超时)抛 httpx 异常,交由 ToolStep
      的 retry 机制处理。
    - 响应体按 Content-Type 智能解析:JSON 自动 parse,否则返回 text。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from agentkit.tools.base import Tool, tool

if TYPE_CHECKING:
    from agentkit.core.context import Context


class HTTPRequestParams(BaseModel):
    """HTTP 请求参数。"""

    url: str = Field(..., description="请求 URL")
    method: str = Field("GET", description="HTTP 方法")
    headers: dict[str, str] | None = Field(None, description="请求头")
    params: dict[str, Any] | None = Field(None, description="查询参数")
    json_body: Any | None = Field(None, description="JSON 请求体")
    timeout: float | None = Field(None, description="超时秒数")


@tool("http.request", role="source")
class HTTPTool(Tool):
    """HTTP 请求工具,基于 ``httpx.AsyncClient``。

    支持 GET / POST / PUT / DELETE 等方法。返回结构::

        {
            "status_code": int,
            "headers": dict,
            "body": str | dict | list,  # Content-Type 含 json 时已解析
            "ok": bool,                # status_code < 400
        }

    错误处理边界:

        - HTTP 错误(4xx / 5xx)不抛异常,返回 ``ok=False``,交由上层决策
        - 网络错误(连接失败 / 超时)抛 ``httpx`` 异常,交由 ToolStep retry 处理
    """

    description = "发起 HTTP 请求"

    @property
    def param_model(self) -> type[BaseModel]:
        return HTTPRequestParams

    @staticmethod
    def _parse_body(response: httpx.Response) -> Any:
        """根据响应 Content-Type 解析响应体。

        若 Content-Type 含 ``json``,则 ``json.loads`` 解析为 dict / list;
        否则返回 ``response.text``。JSON 解析失败时降级返回文本。
        """
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                return response.json()
            except Exception:
                # JSON 解析失败,降级返回文本,保证不抛
                return response.text
        return response.text

    async def call(self, params: dict, ctx: "Context") -> dict:
        """发起 HTTP 请求。

        Args:
            params: ``HTTPRequestParams`` 对应的 dict,``url`` 必填。
            ctx:    会话上下文(只读,本工具未使用)。

        Returns:
            dict: ``{"status_code", "headers", "body", "ok"}``。

        Raises:
            httpx.HTTPError: 网络层错误(连接失败 / 超时等),交由 retry。
        """
        url: str = params["url"]
        method: str = str(params.get("method") or "GET").upper()
        headers = params.get("headers")
        query_params = params.get("params")
        json_body = params.get("json_body")
        timeout = params.get("timeout") or 30.0

        # 网络层错误(ConnectError / TimeoutException 等)向上抛出交由 retry;
        # 4xx/5xx 由 httpx 正常返回 response,不会抛(未开启 raise_for_status)。
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                params=query_params,
                json=json_body,
            )

        body = self._parse_body(response)
        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": body,
            "ok": response.status_code < 400,
        }
