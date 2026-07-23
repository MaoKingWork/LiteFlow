"""tools.md_convert —— Markdown 格式转换工具子包。

导入本子包即自动注册 ``MDConvertTool`` 到全局 ``ToolRegistry``:

    - ``MDConvertTool`` (name="md.convert", role="sink")

模块结构:

    - base:            Block IR、HtmlBlockParser、Converter ABC、get_converter
    - html_converter:  HTML 转换器 (零外部依赖)
    - pdf_converter:   PDF 转换器 (reportlab, 懒加载)
    - docx_converter:  DOCX 转换器 (python-docx, 懒加载)
    - tool:            MDConvertTool 工具包装

可选依赖安装::

    uv pip install 'liteflow[doc-convert]'
"""

from agentkit.tools.md_convert.base import (
    AVAILABLE_FORMATS,
    Block,
    Converter,
    get_converter,
    parse_html_to_blocks,
)
from agentkit.tools.md_convert.tool import MDConvertParams, MDConvertTool

__all__ = [
    "MDConvertTool",
    "MDConvertParams",
    "Converter",
    "Block",
    "get_converter",
    "parse_html_to_blocks",
    "AVAILABLE_FORMATS",
]
