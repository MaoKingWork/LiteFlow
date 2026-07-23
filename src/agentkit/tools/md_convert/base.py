"""md_convert.base —— Markdown 转换基础设施。

定义 Markdown 块级元素的中间表示 (IR)、HTML→Block 解析器、以及转换器
统一接口 ``Converter`` 与懒加载工厂 ``get_converter``。

设计要点:
    - **IR 驱动**: MD 先经 markdown2 转为 HTML, 再由 ``HtmlBlockParser``
      解析为 ``Block`` 列表。PDF / DOCX 转换器各自由 Block 列表构建目标
      文档, 互不耦合。新增输出格式只需实现 ``Converter`` 并在
      ``get_converter`` 中注册。
    - **零硬依赖**: 本模块仅依赖标准库 (``html.parser`` / ``dataclasses``),
      不导入 markdown2 / reportlab / python-docx, 确保未安装可选依赖时
      ``agentkit.tools`` 包仍可正常导入。
    - **内联格式保留**: ``Block.text`` / ``Block.items`` 保存内联 HTML
      (如 ``<strong>`` / ``<em>`` / ``<a>``), reportlab 可直接解析,
      python-docx 通过 ``InlineExtractor`` 解析为 runs。

公开 API:
    - Block:              块级元素 IR
    - HtmlBlockParser:    HTML→Block 解析器
    - parse_html_to_blocks: 便捷函数
    - Converter:          转换器抽象基类
    - get_converter:      按格式名懒加载转换器实例
    - AVAILABLE_FORMATS:  支持的格式元组
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from pathlib import Path


# ---------------------------------------------------------------------------
# Block —— 块级元素中间表示
# ---------------------------------------------------------------------------
@dataclass
class Block:
    """Markdown 块级元素的统一中间表示。

    用 ``type`` 字段区分块类型, 其余字段按需填充。内联格式保留为 HTML
    字符串 (reportlab Paragraph 原生支持, python-docx 经 InlineExtractor
    解析)。

    Attributes:
        type:     块类型: heading | paragraph | list | code | quote | table | hr
        level:    heading 的标题级别 (1-6)。
        text:     heading / paragraph / quote / list-item 的内联 HTML;
                  code 的原始代码文本。
        ordered:  list 是否有序。
        items:    list 各项的内联 HTML。
        language: code 的语言标识 (如 "python")。
        headers:  table 表头各列内联 HTML。
        rows:     table 数据行, 每行各列内联 HTML。
    """

    type: str
    level: int = 0
    text: str = ""
    ordered: bool = False
    items: list[str] = field(default_factory=list)
    language: str = ""
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HtmlBlockParser —— HTML → Block 列表
# ---------------------------------------------------------------------------
# 内联标签集合: 这些标签在块级解析时被原样重建到文本缓冲区, 不触发块级逻辑。
_INLINE_TAGS = frozenset({
    "strong", "b", "em", "i", "code", "a", "del", "strike",
    "sub", "sup", "u", "mark", "span", "font",
})


class HtmlBlockParser(HTMLParser):
    """将 markdown2 生成的 HTML 解析为 ``Block`` 列表。

    使用 ``convert_charrefs=True`` (默认), 字符引用自动解码。对于非 code
    块的文本内容, 重新转义 ``&`` / ``<`` / ``>``, 以保证重建后的内联 HTML
    对 reportlab 是合法的 XML 标记; code 块则保留原始解码文本。

    线程安全: 单实例非线程安全 (与 HTMLParser 一致), 每次解析创建新实例。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._buf: list[str] = []
        # 上下文栈: 跟踪当前所处的块级容器
        self._ctx: list[str] = []
        self._heading_level = 0
        self._list_ordered = False
        self._list_items: list[str] = []
        self._code_lang = ""
        self._table_headers: list[str] = []
        self._table_rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._is_header_row = False

    # -- 内部辅助 ----------------------------------------------------------
    def _reconstruct_tag(self, tag: str,
                         attrs: list[tuple[str, str | None]] | None = None,
                         *, closing: bool = False, self_closing: bool = False) -> str:
        """重建内联标签的 HTML 字符串。"""
        if closing:
            return f"</{tag}>"
        attr_str = "".join(
            f' {k}="{v}"' for k, v in (attrs or []) if v is not None
        )
        if self_closing or tag == "br":
            return f"<{tag}{attr_str}/>"
        return f"<{tag}{attr_str}>"

    def _text(self) -> str:
        """合并缓冲区并 strip 首尾空白。"""
        return "".join(self._buf).strip()

    def _append_data(self, data: str) -> None:
        """将文本追加到缓冲区, 非 code 块中转义特殊字符。"""
        if "code" in self._ctx:
            self._buf.append(data)
        else:
            self._buf.append(escape(data, quote=False))

    # -- HTMLParser 回调 ---------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _INLINE_TAGS:
            self._buf.append(self._reconstruct_tag(tag, attrs))
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._ctx.append("heading")
            self._heading_level = int(tag[1])
            self._buf = []
        elif tag == "p":
            if "quote" in self._ctx:
                # blockquote 内的 <p>: 保留标签, 合并到引用文本
                self._buf.append("<p>")
            else:
                self._ctx.append("paragraph")
                self._buf = []
        elif tag in ("ul", "ol"):
            self._ctx.append("list")
            self._list_ordered = tag == "ol"
            self._list_items = []
        elif tag == "li":
            self._ctx.append("list-item")
            self._buf = []
        elif tag == "pre":
            self._ctx.append("code")
            self._buf = []
        elif tag == "code" and "code" in self._ctx:
            for k, v in attrs:
                if k == "class" and v:
                    for cls in v.split():
                        if cls.startswith("language-"):
                            self._code_lang = cls.removeprefix("language-")
                            break
                    break
        elif tag == "blockquote":
            self._ctx.append("quote")
            self._buf = []
        elif tag == "hr":
            self.blocks.append(Block(type="hr"))
        elif tag == "table":
            self._ctx.append("table")
            self._table_headers = []
            self._table_rows = []
            self._current_row = []
            self._is_header_row = False
        elif tag == "thead":
            self._is_header_row = True
        elif tag == "tbody":
            self._is_header_row = False
        elif tag == "tr":
            self._current_row = []
        elif tag in ("th", "td"):
            self._ctx.append("table-cell")
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _INLINE_TAGS:
            self._buf.append(self._reconstruct_tag(tag, closing=True))
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            if self._ctx and self._ctx[-1] == "heading":
                self.blocks.append(Block(
                    type="heading", level=self._heading_level, text=self._text(),
                ))
                self._ctx.pop()
        elif tag == "p":
            if "quote" in self._ctx:
                self._buf.append("</p>")
            elif self._ctx and self._ctx[-1] == "paragraph":
                text = self._text()
                if text:
                    self.blocks.append(Block(type="paragraph", text=text))
                self._ctx.pop()
        elif tag in ("ul", "ol"):
            if self._ctx and self._ctx[-1] == "list":
                self.blocks.append(Block(
                    type="list", ordered=self._list_ordered,
                    items=self._list_items[:],
                ))
                self._list_items = []
                self._ctx.pop()
        elif tag == "li":
            if self._ctx and self._ctx[-1] == "list-item":
                self._list_items.append(self._text())
                self._ctx.pop()
        elif tag == "pre":
            if self._ctx and self._ctx[-1] == "code":
                self.blocks.append(Block(
                    type="code", text="".join(self._buf).strip("\n"),
                    language=self._code_lang,
                ))
                self._code_lang = ""
                self._ctx.pop()
        elif tag == "blockquote":
            if self._ctx and self._ctx[-1] == "quote":
                text = self._text()
                if text:
                    self.blocks.append(Block(type="quote", text=text))
                self._ctx.pop()
        elif tag == "table":
            if self._ctx and self._ctx[-1] == "table":
                self.blocks.append(Block(
                    type="table", headers=self._table_headers,
                    rows=self._table_rows,
                ))
                self._ctx.pop()
        elif tag == "thead":
            self._is_header_row = False
        elif tag == "tr":
            if self._current_row:
                if self._is_header_row:
                    self._table_headers = self._current_row[:]
                else:
                    self._table_rows.append(self._current_row[:])
            self._current_row = []
        elif tag in ("th", "td"):
            if self._ctx and self._ctx[-1] == "table-cell":
                self._current_row.append(self._text())
                self._ctx.pop()

    def handle_startendtag(self, tag: str,
                           attrs: list[tuple[str, str | None]]) -> None:
        if tag == "hr":
            self.blocks.append(Block(type="hr"))
        elif tag == "br":
            self._buf.append("<br/>")
        elif tag in _INLINE_TAGS:
            self._buf.append(self._reconstruct_tag(tag, attrs, self_closing=True))

    def handle_data(self, data: str) -> None:
        self._append_data(data)

    def error(self, message: str) -> None:  # pragma: no cover
        """HTMLParser 兼容回调 (Python 3.5+ 不再调用)。"""
        pass


def parse_html_to_blocks(html: str) -> list[Block]:
    """将 markdown2 生成的 HTML 片段解析为 ``Block`` 列表。

    Args:
        html: markdown2 输出的 HTML 片段 (不含 ``<html>`` / ``<body>`` 包裹)。

    Returns:
        list[Block]: 块级元素列表。
    """
    parser = HtmlBlockParser()
    parser.feed(html)
    parser.close()
    return parser.blocks


# ---------------------------------------------------------------------------
# Converter —— 转换器抽象基类
# ---------------------------------------------------------------------------
class Converter(ABC):
    """将渲染后的 HTML 转换为目标格式并写入文件。

    转换器接收 HTML (而非 Markdown), 由调用方 (``MDConvertTool``) 统一完成
    MD→HTML 步骤。这样 markdown2 只调用一次, 且转换器职责单一。

    实现者需:
        1. 声明 ``format`` 属性 (如 ``"pdf"`` / ``"docx"``)
        2. 实现 ``convert`` 方法, 将 HTML 写为目标格式到 ``output_path``
    """

    @property
    @abstractmethod
    def format(self) -> str:
        """目标格式名 (如 ``"html"`` / ``"pdf"`` / ``"docx"``)。"""

    @abstractmethod
    def convert(self, html: str, output_path: Path) -> None:
        """将 HTML 转换为目标格式并写入 ``output_path``。

        Args:
            html:        markdown2 渲染后的 HTML 片段。
            output_path: 目标文件路径 (已确保父目录存在)。
        """


# ---------------------------------------------------------------------------
# 懒加载工厂
# ---------------------------------------------------------------------------
#: 支持的格式名 (顺序即 ``--help`` 中的展示顺序)
AVAILABLE_FORMATS: tuple[str, ...] = ("html", "pdf", "docx")

#: 各格式缺失依赖时的安装提示
_INSTALL_HINTS: dict[str, str] = {
    "pdf": "PDF 转换需要 reportlab。安装: uv pip install reportlab",
    "docx": "DOCX 转换需要 python-docx。安装: uv pip install python-docx",
}


def get_converter(fmt: str) -> Converter:
    """按格式名懒加载并返回转换器实例。

    使用延迟导入确保可选依赖 (reportlab / python-docx) 仅在对应格式被
    请求时才加载。未安装时抛 ``ImportError`` 并附安装提示。

    Args:
        fmt: 格式名, 取值见 ``AVAILABLE_FORMATS``。

    Returns:
        Converter: 对应格式的转换器实例。

    Raises:
        ValueError: 不支持的格式名。
        ImportError: 该格式所需的可选依赖未安装。
    """
    if fmt == "html":
        from agentkit.tools.md_convert.html_converter import HtmlConverter
        return HtmlConverter()

    if fmt == "pdf":
        try:
            from agentkit.tools.md_convert.pdf_converter import PdfConverter
        except ImportError as exc:
            raise ImportError(_INSTALL_HINTS["pdf"]) from exc
        return PdfConverter()

    if fmt == "docx":
        try:
            from agentkit.tools.md_convert.docx_converter import DocxConverter
        except ImportError as exc:
            raise ImportError(_INSTALL_HINTS["docx"]) from exc
        return DocxConverter()

    raise ValueError(
        f"不支持的格式 {fmt!r}。支持的格式: {', '.join(AVAILABLE_FORMATS)}"
    )
