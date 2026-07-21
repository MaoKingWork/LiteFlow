"""parsers.json_parser —— JSON 解析器。

本模块提供 ``JSONParser``，把 LLM 文本输出解析为 Python 原生对象
（``dict`` / ``list`` / 标量）。复用 ``PydanticParser.extract_json`` 的
JSON 提取策略（支持裸 JSON、```` ```json ```` 代码块、带前后说明文本的
JSON），但不做 pydantic 模型校验。

适用于 :class:`~agentkit.steps.llm_step.LLMStep` 配置 ``output_format="json"``
的场景：框架自动 ``json.loads``，下游 Step 通过 ``{{var}}`` 拿到的是
``dict`` / ``list`` 而非 ``str``。

与 :class:`~agentkit.parsers.pydantic_parser.PydanticParser` 的区别：
    - ``JSONParser``：只做 JSON 语法解析，无 schema 校验，返回原生 dict/list。
    - ``PydanticParser``：JSON 解析 + pydantic 模型校验，返回模型实例。

设计原则：
    - 复用 ``PydanticParser.extract_json`` 的 JSON 提取逻辑，避免重复实现。
    - 仅依赖标准库 ``json`` 与本包 ``parsers.base`` / ``parsers.pydantic_parser``。
    - 失败不抛异常，统一通过 ``ParseResult`` 表达，复用 LLMStep 输出契约保障链。
"""

from __future__ import annotations

import json

from agentkit.parsers.base import ParseResult, Parser
from agentkit.parsers.pydantic_parser import PydanticParser


class JSONParser(Parser):
    """JSON 解析器：把 LLM 文本输出解析为 Python 原生对象。

    复用 :meth:`PydanticParser.extract_json` 提取 JSON 子串（支持裸 JSON、
    ```` ```json ```` 代码块、带前后说明文本的 JSON），再 ``json.loads`` 为
    ``dict`` / ``list`` / 标量。

    失败返回 ``ParseResult(ok=False)``，由 LLMStep 输出契约保障链处理重试。

    用法示例::

        p = JSONParser()
        p.parse('{"name": "杰克", "age": 30}')
        # -> ParseResult(ok=True, value={"name": "杰克", "age": 30})

        p.parse('好的:\\n```json\\n{"x": 1}\\n```\\n请查收')
        # -> ParseResult(ok=True, value={"x": 1})

        p.parse('不是 JSON')
        # -> ParseResult(ok=False, error="无法从输出中提取 JSON")
    """

    def parse(self, text: str) -> ParseResult:
        """解析文本为 Python 原生 JSON 对象。

        流程：
            1. 从文本提取 JSON 子串（复用 ``PydanticParser.extract_json``）。
            2. ``json.loads`` 解析为 dict / list / 标量。
            3. 成功返回 ``ParseResult(ok=True, value=<原生对象>)``。
            4. 失败返回 ``ParseResult(ok=False, error=<可读错误>)``。

        Args:
            text: 待解析的原始文本（通常是 LLM 输出）。

        Returns:
            ParseResult: 解析结果。
        """
        json_str = PydanticParser.extract_json(text)
        if json_str is None:
            return ParseResult(
                success=False,
                error="无法从输出中提取 JSON",
            )
        try:
            return ParseResult(success=True, value=json.loads(json_str))
        except json.JSONDecodeError as e:
            return ParseResult(
                success=False,
                error=f"JSON 语法错误: {e}",
            )


__all__ = ["JSONParser"]
