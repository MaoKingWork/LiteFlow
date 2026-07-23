"""md_convert.tool —— Markdown 格式转换工具。

提供 ``MDConvertTool``, 通过 ``md.convert`` 注册到全局 ToolRegistry。
将标准 Markdown 内容 (字符串或文件路径) 转换为 HTML / PDF / DOCX 格式。

设计要点:
    - **输入灵活**: ``content`` (MD 字符串) 或 ``path`` (文件路径) 二选一;
      ``path`` 优先, 二者均空抛 ``ValueError``。
    - **多格式输出**: ``formats`` 接受 ``html`` / ``pdf`` / ``docx`` 的任意
      子集, 一次调用可同时生成多种格式。
    - **懒加载依赖**: markdown2 在 ``call`` 内延迟导入; reportlab /
      python-docx 由 ``get_converter`` 按需加载。未安装时抛 ``ImportError``
      并附安装提示。
    - **role="sink"**: 转换结果是输出终端操作。

返回结构::

    {
        "input":  "/abs/input.md" | "inline",
        "files": [
            {"format": "html", "path": "/abs/output.html", "size": 1234},
            {"format": "pdf",  "path": "/abs/output.pdf",  "size": 5678},
        ],
    }
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from agentkit.tools.base import Tool, tool
from agentkit.tools.md_convert.base import AVAILABLE_FORMATS, get_converter

if TYPE_CHECKING:
    from agentkit.core.context import Context


#: markdown2 启用的 extras (覆盖 GFM 常用语法: 围栏代码块 / 表格 / 删除线)
_MARKDOWN_EXTRAS = [
    "fenced-code-blocks",
    "tables",
    "strike",
    "cuddled-lists",
    "header-ids",
]


class MDConvertParams(BaseModel):
    """Markdown 转换参数。"""

    content: str | None = Field(
        None,
        description="Markdown 内容字符串 (与 path 二选一, path 优先)",
    )
    path: str | None = Field(
        None,
        description="Markdown 文件路径 (与 content 二选一, path 优先)",
    )
    formats: list[str] = Field(
        default_factory=lambda: ["html"],
        description=f"目标格式列表, 可选: {', '.join(AVAILABLE_FORMATS)}",
    )
    output_dir: str = Field(
        ".",
        description="输出目录 (默认当前目录, 不存在时自动创建)",
    )
    output_name: str | None = Field(
        None,
        description="输出文件名 (不含扩展名, 默认从 path 推导或 'markdown_output')",
    )


@tool("md.convert", role="sink")
class MDConvertTool(Tool):
    """Markdown 格式转换工具。

    将 Markdown 转换为 HTML / PDF / DOCX。支持直接传入 MD 字符串或指定
    MD 文件路径, 一次调用可生成多种格式。

    依赖:
        - ``markdown2`` (必需, MD→HTML 解析)
        - ``reportlab`` (PDF 格式时必需)
        - ``python-docx`` (DOCX 格式时必需)

    安装全部依赖::

        uv pip install 'liteflow[doc-convert]'
    """

    description = (
        "将 Markdown 内容转换为 HTML / PDF / DOCX 格式文件。"
        "输入可为 MD 字符串或文件路径, 支持同时输出多种格式。"
    )

    @property
    def param_model(self) -> type[BaseModel]:
        return MDConvertParams

    async def call(self, params: dict, ctx: "Context") -> dict[str, Any]:
        """执行 Markdown 格式转换。

        Args:
            params: ``MDConvertParams`` 对应的 dict。
            ctx:    会话上下文 (只读, 本工具未使用)。

        Returns:
            dict: ``{"input": str, "files": [{"format", "path", "size"}]}``。

        Raises:
            ValueError: ``content`` 与 ``path`` 均为空, 或 ``formats`` 含不支持的格式。
            ImportError: 所需可选依赖未安装。
            FileNotFoundError: ``path`` 指定的文件不存在。
        """
        # 1. 解析输入: path 优先, 否则用 content
        path_str = params.get("path")
        content = params.get("content")
        if path_str:
            input_path = Path(path_str)
            if not input_path.is_absolute():
                input_path = Path(os.environ.get("FILE_ROOT", ".")) / input_path
            markdown = input_path.read_text(encoding="utf-8")
            input_label = str(input_path)
            default_name = input_path.stem
        elif content:
            markdown = content
            input_label = "inline"
            default_name = "markdown_output"
        else:
            raise ValueError(
                "必须提供 content (Markdown 字符串) 或 path (文件路径) 之一"
            )

        # 2. 校验格式
        formats = params.get("formats") or ["html"]
        invalid = [f for f in formats if f not in AVAILABLE_FORMATS]
        if invalid:
            raise ValueError(
                f"不支持的格式: {invalid}。支持的格式: {', '.join(AVAILABLE_FORMATS)}"
            )

        # 3. MD → HTML (markdown2 延迟导入)
        try:
            import markdown2
        except ImportError as exc:
            raise ImportError(
                "Markdown 转换需要 markdown2。"
                "安装: uv pip install 'liteflow[doc-convert]'"
            ) from exc
        html = markdown2.markdown(markdown, extras=_MARKDOWN_EXTRAS)

        # 4. 解析输出路径
        output_dir = Path(params.get("output_dir") or ".")
        if not output_dir.is_absolute():
            output_dir = Path(os.environ.get("FILE_ROOT", ".")) / output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        output_name = params.get("output_name") or default_name

        # 5. 逐格式转换
        files: list[dict[str, Any]] = []
        for fmt in formats:
            converter = get_converter(fmt)
            out_path = output_dir / f"{output_name}.{fmt}"
            converter.convert(html, out_path)
            files.append({
                "format": fmt,
                "path": str(out_path),
                "size": out_path.stat().st_size,
            })

        return {
            "input": input_label,
            "files": files,
        }
