"""parsers —— 解析器子包。

将 LLM 输出或结构化文本解析为目标对象：
    - base:           Parser 抽象基类与 ParseResult 数据类
    - pydantic_parser: 基于 pydantic 模型解析 JSON / 结构化输出
    - text_parser:    纯文本解析，支持模板与正则
"""

from agentkit.parsers.base import Parser, ParseResult
from agentkit.parsers.pydantic_parser import PydanticParser
from agentkit.parsers.text_parser import TextParser

__all__ = ["Parser", "ParseResult", "PydanticParser", "TextParser"]
