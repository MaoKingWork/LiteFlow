"""parsers.text_parser —— 纯文本解析器。

本模块提供 ``TextParser``，是 ``Parser`` 的最宽松实现：
    - ``target_model`` 为 None 时：直接返回剥离首尾空白后的文本。
    - ``target_model`` 非 None 时：尝试把文本当 JSON 解析为目标 pydantic 模型；
      解析失败则降级返回原文文本（不报错）。

这种"宽松降级"策略适用于 LLM 输出格式不稳定的场景：当模型未能严格遵循
JSON 指令时，仍能拿到原始文本供下游兜底处理。

设计原则：
    - 仅依赖 ``pydantic`` 与本包 ``parsers.base``，无循环依赖。
    - 失败不抛异常，统一通过 ``ParseResult`` 表达。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from agentkit.parsers.base import Parser, ParseResult


class TextParser(Parser):
    """纯文本解析器。

    行为由 ``target_model`` 决定：
        - ``target_model`` 为 None（默认）：直接返回 ``text.strip()``，
          ``ParseResult.ok=True``。
        - ``target_model`` 非 None：尝试 ``json.loads(text)`` 后
          ``target_model.model_validate``；任一步失败则降级返回 ``text.strip()``
          （``ParseResult.ok=True``，value 为 str）。

    降级语义说明：文本解析器对格式不做强制要求，因此即使 JSON 解析失败也视为
    "成功"并返回原文。需要严格校验的场景请使用 ``PydanticParser``。

    用法示例::

        # 纯文本
        tp = TextParser()
        pr = tp.parse("  hello  ")  # ok=True, value="hello"

        # 目标模型降级
        class M(BaseModel):
            x: int
        tp2 = TextParser()
        tp2.target_model = M
        tp2.parse('{"x": 5}')    # ok=True, value=M(x=5)
        tp2.parse("not json")    # ok=True, value="not json"（降级）
    """

    def parse(self, text: str) -> ParseResult:
        """解析文本。

        Args:
            text: 待解析的原始文本。

        Returns:
            ParseResult: 始终 ``ok=True``。
                - 无 ``target_model``：value 为 ``text.strip()``。
                - 有 ``target_model`` 且 JSON 解析 + 模型校验成功：value 为模型实例。
                - 有 ``target_model`` 但解析失败：value 降级为 ``text.strip()``。
        """
        # 无目标模型：纯文本模式，直接返回剥离空白后的文本
        if self.target_model is None:
            return ParseResult(success=True, value=text.strip())

        # 有目标模型：尝试 JSON -> 模型；失败降级为文本
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            # 非 JSON 文本：降级返回原文
            return ParseResult(success=True, value=text.strip())

        try:
            value = self.target_model.model_validate(data)
            return ParseResult(success=True, value=value)
        except ValidationError:
            # JSON 但结构不匹配模型：降级返回原文
            return ParseResult(success=True, value=text.strip())


__all__ = ["TextParser"]
