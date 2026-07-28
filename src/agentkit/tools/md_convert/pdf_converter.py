"""md_convert.pdf_converter —— PDF 转换器 (reportlab)。

将 markdown2 渲染后的 HTML 经 ``parse_html_to_blocks`` 解析为 Block 列表,
再映射为 reportlab platypus Flowable, 构建为 PDF 文档。

依赖: ``reportlab`` (可选, 仅在本模块被导入时加载)。

设计要点:
    - 内联 HTML 标签映射: ``<strong>``→``<b>``, ``<em>``→``<i>``,
      ``<code>``→``<font face="...">``, ``<del>``→``<strike>``。
      reportlab ``Paragraph`` 原生支持这些标签, 无需自行解析内联格式。
    - 块级映射: heading→Paragraph(HeadingN), paragraph→Paragraph(Normal),
      list→ListFlowable, code→Preformatted, quote→缩进Paragraph,
      table→Table, hr→HRFlowable。
    - 样式从 ``getSampleStyleSheet`` 扩展, 保持与 reportlab 默认风格一致。
    - **字体内嵌**: 通过 ``pdfmetrics.registerFont(TTFont)`` 将 OPPO Sans
      TTF 注册并内嵌进生成的 PDF; 同一 TTF 同时注册为 Bold 变体并经
      ``registerFontFamily`` 关联, 使 ``<b>`` 标签可正确路由。后续新增
      独立 Bold/Italic TTF 只需更新 ``_register_fonts``。
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from agentkit.assets import DEFAULT_FONT, get_font_path
from agentkit.tools.md_convert.base import Block, Converter, parse_html_to_blocks

#: 代码块字体 (reportlab 内置 PostScript 名)。
#: reportlab 仅识别内置 PS 名 (Courier / Helvetica / Times-Roman) 与已通过
#: ``TTFont`` 注册的字体。"Courier New" 是 Windows 系统字体名, reportlab
#: 无法识别, 故 PDF 侧使用内置 "Courier"。后续若引入专用等宽 TTF
#: (如 JetBrains Mono), 在 ``_register_fonts`` 中注册后更新此常量即可。
_CODE_FONT_RL: str = "Courier"

# ---------------------------------------------------------------------------
# 字体注册 (模块级缓存, 仅首次调用时读取 TTF 文件)
# ---------------------------------------------------------------------------
_fonts_registered: bool = False


def _register_fonts() -> None:
    """向 reportlab 注册内嵌字体 (OPPO Sans)。

    使用模块级 ``_fonts_registered`` 标志避免重复读取 TTF 文件。
    reportlab ``registerFont`` 本身按名去重, 但 ``TTFont`` 构造器会读文件,
    因此缓存仍有必要。

    当前 OPPO Sans 仅有单一 Regular TTF, 同时注册为 Bold 变体并经
    ``registerFontFamily`` 关联, 使 Paragraph 中的 ``<b>`` 能路由到
    "OPPO Sans-Bold"。后续若引入独立 Bold TTF, 替换下方注册即可。
    """
    global _fonts_registered
    if _fonts_registered:
        return
    ttf_path = str(get_font_path(DEFAULT_FONT))
    pdfmetrics.registerFont(TTFont(DEFAULT_FONT, ttf_path))
    # 同一 TTF 暂时兼作 Bold 变体 (结构正确, 视觉等宽; 后续可拆分)
    bold_name = f"{DEFAULT_FONT}-Bold"
    pdfmetrics.registerFont(TTFont(bold_name, ttf_path))
    pdfmetrics.registerFontFamily(
        DEFAULT_FONT,
        normal=DEFAULT_FONT,
        bold=bold_name,
        italic=DEFAULT_FONT,
        boldItalic=bold_name,
    )
    _fonts_registered = True

# ---------------------------------------------------------------------------
# 内联 HTML 标签映射: markdown2 输出 → reportlab Paragraph 支持的标签
# ---------------------------------------------------------------------------
_INLINE_TAG_MAP: dict[str, str] = {
    "strong": "b",
    "em": "i",
    "del": "strike",
    "strike": "strike",
}


def _map_inline_tags(html: str) -> str:
    """将 markdown2 的内联标签映射为 reportlab 兼容标签。

    reportlab ``Paragraph`` 支持 ``<b>`` / ``<i>`` / ``<strike>`` /
    ``<font>`` / ``<a>`` / ``<br/>`` 等子集, 但不支持 ``<strong>`` /
    ``<em>`` / ``<del>`` / ``<code>``。本函数做简单的标签名替换。
    """
    result = html
    for src, dst in _INLINE_TAG_MAP.items():
        result = result.replace(f"<{src}>", f"<{dst}>")
        result = result.replace(f"</{src}>", f"</{dst}>")
    # <code> → <font face="..."> (reportlab 支持 <font face="...">)
    result = result.replace("<code>", f'<font face="{_CODE_FONT_RL}">')
    result = result.replace("</code>", "</font>")
    return result


# ---------------------------------------------------------------------------
# PdfConverter
# ---------------------------------------------------------------------------
class PdfConverter(Converter):
    """将 HTML 转换为 PDF 文档 (基于 reportlab)。

    流程: HTML → Block 列表 → reportlab Flowable 列表 → SimpleDocTemplate。
    """

    @property
    def format(self) -> str:
        return "pdf"

    def convert(self, html: str, output_path: Path) -> None:
        """构建 PDF 并写入 ``output_path``。

        Args:
            html:        markdown2 渲染后的 HTML 片段。
            output_path: ``.pdf`` 文件路径。
        """
        blocks = parse_html_to_blocks(html)
        _register_fonts()
        styles = _build_styles()
        story = _blocks_to_story(blocks, styles)

        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=A4,
            leftMargin=25 * mm,
            rightMargin=25 * mm,
            topMargin=25 * mm,
            bottomMargin=25 * mm,
        )
        doc.build(story)


# ---------------------------------------------------------------------------
# 样式构建
# ---------------------------------------------------------------------------
def _build_styles() -> dict[str, ParagraphStyle]:
    """构建 PDF 用样式集, 基于 reportlab 样板并扩展自定义样式。

    所有正文样式 (Normal / Heading / Quote / ListItem) 统一使用内嵌的
    OPPO Sans; 代码块 (Code) 使用 reportlab 内置 ``_CODE_FONT_RL``
    (默认 Courier)。Heading 采用 ``OPPO Sans-Bold`` 变体名, 后续引入
    独立 Bold TTF 后自动生效。
    """
    base = getSampleStyleSheet()
    styles: dict[str, ParagraphStyle] = {}
    bold_name = f"{DEFAULT_FONT}-Bold"

    # 正文 / 标题 / 代码: 基于样板派生, 覆盖 fontName
    text_styles = {
        "Normal": DEFAULT_FONT,
        "Code": _CODE_FONT_RL,
    }
    for name, font in text_styles.items():
        styles[name] = ParagraphStyle(
            name, parent=base[name], fontName=font,
        )
    # Heading 1-6 使用 Bold 变体名
    for level in range(1, 7):
        name = f"Heading{level}"
        styles[name] = ParagraphStyle(
            name, parent=base[name], fontName=bold_name,
        )

    # 引用样式: 左缩进 + 灰色左边框 + 斜体
    styles["Quote"] = ParagraphStyle(
        "Quote",
        parent=base["Normal"],
        fontName=DEFAULT_FONT,
        leftIndent=20,
        rightIndent=10,
        borderColor=colors.HexColor("#dfe2e5"),
        borderPadding=(6, 6, 6, 10),
        borderWidth=0,
        textColor=colors.HexColor("#6a737d"),
        spaceBefore=6,
        spaceAfter=6,
    )
    # 列表项样式
    styles["ListItem"] = ParagraphStyle(
        "ListItem",
        parent=base["Normal"],
        fontName=DEFAULT_FONT,
        leftIndent=18,
        spaceBefore=2,
        spaceAfter=2,
    )
    return styles


# ---------------------------------------------------------------------------
# Block → Flowable 映射
# ---------------------------------------------------------------------------
def _blocks_to_story(blocks: list[Block],
                     styles: dict[str, ParagraphStyle]) -> list:
    """将 Block 列表转换为 reportlab Flowable 列表。"""
    story = []
    for block in blocks:
        flowables = _block_to_flowables(block, styles)
        story.extend(flowables)
        story.append(Spacer(1, 4 * mm))
    # 移除末尾多余 Spacer
    if story and isinstance(story[-1], Spacer):
        story.pop()
    return story


def _block_to_flowables(block: Block,
                        styles: dict[str, ParagraphStyle]) -> list:
    """将单个 Block 映射为 reportlab Flowable 列表。"""
    match block.type:
        case "heading":
            style_name = f"Heading{min(max(block.level, 1), 6)}"
            return [Paragraph(_map_inline_tags(block.text), styles[style_name])]

        case "paragraph":
            return [Paragraph(_map_inline_tags(block.text), styles["Normal"])]

        case "list":
            items = [
                ListItem(Paragraph(_map_inline_tags(item), styles["ListItem"]))
                for item in block.items
            ]
            return [ListFlowable(
                items,
                bulletType="1" if block.ordered else "bullet",
                start="1" if block.ordered else None,
            )]

        case "code":
            return [Preformatted(block.text, styles["Code"])]

        case "quote":
            return [Paragraph(_map_inline_tags(block.text), styles["Quote"])]

        case "table":
            return [_build_table(block, styles)]

        case "hr":
            return [HRFlowable(width="100%", thickness=0.5,
                               color=colors.HexColor("#eee"),
                               spaceBefore=4, spaceAfter=4)]

        case _:
            return []


def _build_table(block: Block, styles: dict[str, ParagraphStyle]) -> Table:
    """从 Block 构建带样式的 reportlab Table。"""
    # 将每个单元格内容包裹为 Paragraph (支持内联格式)
    header_row = [
        Paragraph(_map_inline_tags(cell), styles["Normal"]) for cell in block.headers
    ]
    data_rows = [
        [Paragraph(_map_inline_tags(cell), styles["Normal"]) for cell in row]
        for row in block.rows
    ]
    data = [header_row, *data_rows] if header_row else data_rows

    col_count = max(len(r) for r in data) if data else 1
    # 补齐不等列数 (防御性)
    for row in data:
        while len(row) < col_count:
            row.append(Paragraph("", styles["Normal"]))

    table = Table(data, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dfe2e5")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f6f8fa")),
        ("FONTNAME", (0, 0), (-1, 0), f"{DEFAULT_FONT}-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), DEFAULT_FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f6f8fa")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table
