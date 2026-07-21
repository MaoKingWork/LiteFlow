"""parsers.text_parser —— 纯文本解析器。

本模块提供 ``TextParser``，是 ``Parser`` 的最简实现：直接返回剥离首尾
空白后的文本，``ParseResult.ok`` 恒为 ``True``。

适用于 LLM 输出无需结构化解析的场景（如自由文本摘要、报告段落）。需要
JSON 解析请用 :class:`~agentkit.parsers.json_parser.JSONParser`；需要
schema 校验请用 :class:`~agentkit.parsers.pydantic_parser.PydanticParser`。

设计原则：
    - 仅依赖本包 ``parsers.base``，无循环依赖。
    - 失败不抛异常，统一通过 ``ParseResult`` 表达。
"""

from __future__ import annotations

from agentkit.parsers.base import ParseResult, Parser


class TextParser(Parser):
    """纯文本解析器：直接返回 ``text.strip()``。

    用法示例::

        tp = TextParser()
        pr = tp.parse("  hello  ")  # ok=True, value="hello"
    """

    def parse(self, text: str) -> ParseResult:
        """返回剥离首尾空白后的文本。

        Args:
            text: 待解析的原始文本。

        Returns:
            ParseResult: 始终 ``ok=True``，value 为 ``text.strip()``。
        """
        return ParseResult(success=True, value=text.strip())


__all__ = ["TextParser"]
