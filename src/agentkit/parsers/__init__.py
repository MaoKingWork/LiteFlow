"""parsers —— 解析器子包。

将 LLM 输出或结构化文本解析为目标对象：
    - base:           Parser 抽象基类与 ParseResult 数据类
    - text_parser:    纯文本解析器（直接返回 strip 后的文本）
    - json_parser:    JSON 解析器（json.loads 为 dict/list/标量）
    - pydantic_parser: 基于 pydantic 模型解析 JSON + schema 校验
"""

from agentkit.parsers.base import Parser, ParseResult
from agentkit.parsers.json_parser import JSONParser
from agentkit.parsers.pydantic_parser import PydanticParser
from agentkit.parsers.text_parser import TextParser

__all__ = ["Parser", "ParseResult", "TextParser", "JSONParser", "PydanticParser"]
