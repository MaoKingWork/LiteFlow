"""md_convert.pdf_converter —— PDF 转换器 (reportlab)。

将 markdown2 渲染后的 HTML 经 ``parse_html_to_blocks`` 解析为 Block 列表,
再映射为 reportlab platypus Flowable, 构建为 PDF 文档。

依赖: ``reportlab`` (可选, 仅在本模块被导入时加载)。

设计要点:
    - 内联 HTML 标签映射: ``<strong>``→``<b>``, ``<em>``→``<i>``,
      ``<code>``→``<font face="Courier">``, ``<del>``→``<strike>``。
      reportlab ``Paragraph`` 原生支持这些标签, 无需自行解析内联格式。
    - 块级映射: heading→Paragraph(HeadingN), paragraph→Paragraph(Normal),
      list→ListFlowable, code→Preformatted, quote→缩进Paragraph,
      table→Table, hr→HRFlowable。
    - 样式从 ``getSampleStyleSheet`` 扩展, 保持与 reportlab 默认风格一致。
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
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

from agentkit.tools.md_convert.base import Block, Converter, parse_html_to_blocks

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
    # <code> → <font face="Courier"> (reportlab 支持 <font face="...">)
    result = result.replace("<code>", '<font face="Courier">')
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
    """构建 PDF 用样式集, 基于 reportlab 样板并扩展自定义样式。"""
    base = getSampleStyleSheet()
    styles: dict[str, ParagraphStyle] = {}

    # 复用样板中的 Heading / Normal / Code
    for name in ("Heading1", "Heading2", "Heading3",
                 "Heading4", "Heading5", "Heading6", "Normal", "Code"):
        styles[name] = base[name]

    # 引用样式: 左缩进 + 灰色左边框 + 斜体
    styles["Quote"] = ParagraphStyle(
        "Quote",
        parent=base["Normal"],
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
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
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
