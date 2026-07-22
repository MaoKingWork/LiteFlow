"""md_convert.html_converter —— HTML 转换器。

将 markdown2 渲染后的 HTML 片段包裹为完整的 HTML 文档 (含 ``<!DOCTYPE>``
声明、``<head>`` 与基础 CSS), 写入 ``.html`` 文件。

零外部依赖: 仅用标准库 ``pathlib``。
"""

from __future__ import annotations

from pathlib import Path

from agentkit.tools.md_convert.base import Converter

#: 基础 CSS: 提供可读的默认排版 (正文 / 标题 / 代码 / 表格 / 引用)
_CSS = """\
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
line-height:1.6;color:#333;max-width:800px;margin:2rem auto;padding:0 1rem}
h1,h2,h3,h4,h5,h6{margin-top:1.5em;margin-bottom:.5em;line-height:1.3}
h1{font-size:2em;border-bottom:1px solid #eee;padding-bottom:.3em}
h2{font-size:1.5em;border-bottom:1px solid #eee;padding-bottom:.3em}
code{font-family:"SF Mono",Consolas,"Liberation Mono",Menlo,monospace;
font-size:.9em;background:#f6f8fa;padding:.2em .4em;border-radius:3px}
pre{background:#f6f8fa;padding:1em;border-radius:6px;overflow-x:auto}
pre code{background:none;padding:0}
blockquote{border-left:4px solid #dfe2e5;padding:0 1em;color:#6a737d;margin:0 0 1em}
table{border-collapse:collapse;width:100%;margin:1em 0}
th,td{border:1px solid #dfe2e5;padding:6px 13px}
th{background:#f6f8fa;font-weight:600}
tr:nth-child(2n){background:#f6f8fa}
a{color:#0366d6;text-decoration:none}
a:hover{text-decoration:underline}
hr{border:none;border-top:2px solid #eee;margin:2em 0}
img{max-width:100%}
"""

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
{body}
</body>
</html>
"""


class HtmlConverter(Converter):
    """将 HTML 片段包裹为完整 HTML 文档并写入文件。

    输出为带 CSS 的独立 HTML 文件, 可直接在浏览器中打开查看。
    """

    @property
    def format(self) -> str:
        return "html"

    def convert(self, html: str, output_path: Path) -> None:
        """写入完整 HTML 文档。

        Args:
            html:        markdown2 渲染后的 HTML 片段。
            output_path: ``.html`` 文件路径。
        """
        # 从输出文件名推导 title
        title = output_path.stem
        document = _HTML_TEMPLATE.format(
            lang="zh-CN",
            title=title,
            css=_CSS,
            body=html,
        )
        output_path.write_text(document, encoding="utf-8")
