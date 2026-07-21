"""parsers.pydantic_parser —— 基于 pydantic 的结构化输出解析器。

本模块提供 ``PydanticParser``，是 AgentKit 输出契约保障链的关键组件之一。
LLMStep 的输出契约如下::

    LLM 最终输出
        └─> PydanticParser.parse
              ├─ 成功 -> 返回模型实例,流程继续
              └─ 失败 -> 附加 retry_hint 重试
                          └─ 重试耗尽 -> 降级到 fallback_model
                                          └─ 仍失败 -> 执行 on_exhausted

``PydanticParser`` 负责其中的「解析 + 修复重试入口」:
    - ``parse``:把 LLM 文本输出解析为 ``target_model`` 实例,失败返回带 ``error``
      的 ``ParseResult``,LLMStep 据此决定是否重试。
    - ``parse_with_retry_hint``:解析并产出供 LLM 的修复提示文本,LLMStep 把它
      拼到下一次请求的 messages 里实现修复重试。

JSON 提取策略:
    1. 优先匹配 ```` ```json ... ``` ```` 或 ```` ``` ... ``` ```` 代码块;
    2. 退回到 ``text.strip()``,用「平衡括号扫描」从第一个 ``{``/``[`` 提取
       第一个完整的 JSON 子串(可处理 JSON 后有说明文字、JSON 内部字符串里
       含括号等情况——通过简单状态机跳过字符串字面量内的括号)。

设计原则:
    - 高度模块化:仅依赖 ``parsers.base`` + ``pydantic`` + 标准库(re/json),
      不依赖其他 agentkit 子模块,无循环依赖。
    - 可拓展:继承 ``Parser`` 基类,新增解析策略只需子类化。
    - 修复重试入口清晰:``parse_with_retry_hint`` 是 LLMStep 保障链的关键。
    - 失败不抛异常,统一通过 ``ParseResult`` 表达,便于调用方聚合处理。
    - 类型注解完整,中文 docstring 与注释。
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ValidationError

from agentkit.parsers.base import Parser, ParseResult


class PydanticParser(Parser):
    """结构化解析器:把 LLM 文本输出解析为 ``target_model`` 实例。

    支持修复重试入口:解析失败时返回带 ``error`` 的 ``ParseResult``,
    LLMStep 据此附加报错信息重试。

    Attributes:
        target_model: 期望解析得到的 pydantic 模型类。为 None 时降级为纯文本
                      解析(与 ``TextParser`` 一致,直接返回 ``text.strip()``)。
                      可在构造时传入或设置类/实例属性。

    用法示例::

        class Order(BaseModel):
            id: int
            name: str
            amount: float

        p = PydanticParser(Order)
        # 成功:裸 JSON
        p.parse('{"id": 1, "name": "foo", "amount": 9.9}')
        # -> ParseResult(ok=True, value=Order(id=1, name='foo', amount=9.9))

        # 成功:代码块 + 前后说明文字
        p.parse('好的:\\n```json\\n{"id": 2, ...}\\n```\\n请查收')

        # 失败:字段缺失 -> 返回 error,可据此重试
        r = p.parse('{"id": 3, "name": "baz"}')
        # -> ParseResult(ok=False, error='结构校验失败: ...')

        # 修复重试入口
        result, hint = p.parse_with_retry_hint('{"id": 3, "name": "baz"}')
        # 失败时 hint 非空,可拼到下一次 LLM 请求中
    """

    def __init__(self, target_model: type[BaseModel] | None = None):
        """构造解析器。

        Args:
            target_model: 期望解析得到的 pydantic 模型类。为 None 时降级为
                纯文本解析(直接返回 ``text.strip()``)。也可在构造后通过
                设置 ``self.target_model`` 属性变更。
        """
        # 显式传入的 target_model 覆盖类属性默认值
        if target_model is not None:
            self.target_model = target_model

    # ------------------------------------------------------------------
    # 公开解析接口
    # ------------------------------------------------------------------
    def parse(self, text: str) -> ParseResult:
        """解析文本为 ``target_model`` 实例。

        解析流程:
            1. ``target_model`` 为 None:降级为返回 ``text.strip()``
               (与 ``TextParser`` 一致)。
            2. 从文本中提取 JSON(支持裸 JSON、```` ```json ```` 代码块、
               带前后说明文本的 JSON)。
            3. ``json.loads`` + ``target_model.model_validate``。
            4. 成功返回 ``ParseResult(success=True, value=<模型实例>)``。
            5. 失败返回 ``ParseResult(success=False, error=<可读错误信息>,
               value=None)``,``error`` 供修复重试使用。

        Args:
            text: 待解析的原始文本(通常是 LLM 输出)。

        Returns:
            ParseResult: 解析结果。详见上方流程说明。
        """
        # 1. 无目标模型:降级为纯文本,直接返回剥离空白后的原文
        if self.target_model is None:
            return ParseResult(success=True, value=text.strip())

        # 2. 提取 JSON 子串
        json_str = self.extract_json(text)
        if json_str is None:
            return ParseResult(
                success=False,
                error="无法从输出中提取 JSON",
            )

        # 3. JSON 语法解析
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            return ParseResult(
                success=False,
                error=f"JSON 语法错误: {e}",
            )
        except (TypeError, ValueError) as e:
            # 防御性:json.loads 极少数情况下可能抛 TypeError/ValueError
            return ParseResult(
                success=False,
                error=f"JSON 解析异常: {e}",
            )

        # 4. pydantic 模型校验
        try:
            model_instance = self.target_model.model_validate(data)
        except ValidationError as e:
            return ParseResult(
                success=False,
                error=f"结构校验失败: {self._format_validation_error(e)}",
            )

        # 5. 成功
        return ParseResult(success=True, value=model_instance)

    def parse_with_retry_hint(self, text: str) -> tuple[ParseResult, str]:
        """覆盖：失败时附带 target_model 的 schema 摘要，引导 LLM 按结构输出。"""
        result = self.parse(text)
        if result.ok:
            return result, ""
        schema_summary = self._schema_summary()
        hint = (
            f"上一次输出解析失败,错误:\n{result.error}\n"
            f"请严格按以下结构输出合法 JSON,不要包含额外说明:\n{schema_summary}"
        )
        return result, hint

    # ------------------------------------------------------------------
    # JSON 提取
    # ------------------------------------------------------------------
    @staticmethod
    def extract_json(text: str) -> str | None:
        """从文本提取 JSON 字符串。

        提取策略(按优先级):
            1. 匹配 ```` ```json ... ``` ```` 或 ```` ``` ... ``` ```` 代码块,
               取代码块内部内容;
            2. 退回到 ``text.strip()``,用「平衡括号扫描」从第一个 ``{``/``[``
               开始,计数括号深度,找到匹配的闭合位置,提取子串。

        平衡括号扫描通过简单状态机跳过字符串字面量内的括号(处理 JSON 内部
        字符串里含 ``{``/``}`` 的情况),并能处理 JSON 后有说明文字的情况。

        Args:
            text: 原始文本。

        Returns:
            str | None: 提取到的 JSON 字符串;都无法提取时返回 None。
        """
        # 候选文本列表:先代码块内容,再整段 strip
        candidates: list[str] = []

        # 1. 尝试 ```json ... ``` 或 ``` ... ``` 代码块
        code_block = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if code_block:
            candidates.append(code_block.group(1).strip())

        # 2. 退回到整段 strip
        candidates.append(text.strip())

        # 对每个候选执行平衡括号扫描,取第一个成功的结果
        for candidate in candidates:
            extracted = PydanticParser._scan_balanced(candidate)
            if extracted is not None:
                return extracted
        return None

    @staticmethod
    def _scan_balanced(s: str) -> str | None:
        """从字符串中扫描第一个平衡的 ``{...}`` / ``[...]`` 子串。

        使用简单状态机:
            - 遇到 ``"`` 进入/退出字符串字面量;
            - 字符串内遇 ``\\`` 转义下一字符(跳过);
            - 字符串外的 ``{`` / ``[`` 累加深度,``}`` / ``]`` 递减深度;
            - 深度归零时返回闭合位置对应的子串。

        Args:
            s: 候选文本(已 strip)。

        Returns:
            str | None: 第一个平衡的 JSON 子串;不存在时返回 None。
        """
        # 定位第一个 { 或 [
        start = -1
        for i, ch in enumerate(s):
            if ch in "{[":
                start = i
                break
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(s)):
            ch = s[i]
            if escape:
                # 上一字符是反斜杠,本字符被转义,直接跳过
                escape = False
                continue
            if in_string:
                if ch == "\\":
                    # 进入转义态,下一字符跳过
                    escape = True
                elif ch == '"':
                    # 字符串闭合
                    in_string = False
                continue
            # 字符串外
            if ch == '"':
                in_string = True
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    # 平衡闭合,返回子串
                    return s[start : i + 1]
        # 括号未平衡(EOF 提前到达):返回 None
        return None

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _format_validation_error(e: ValidationError) -> str:
        """把 ``ValidationError`` 格式化为可读的多行字符串。

        每条错误形如 ``loc -> msg (type)``,其中:
            - ``loc``: 字段路径(元组,如 ``('amount',)``);
            - ``msg``: 错误信息(如 ``Field required``);
            - ``type``: 错误类型(如 ``missing``)。

        Args:
            e: pydantic 抛出的 ``ValidationError``。

        Returns:
            str: 多行错误描述。
        """
        errors = e.errors()
        if not errors:
            return str(e)
        lines = []
        for err in errors:
            loc = err.get("loc", ())
            # loc 是 tuple,用 '.' 拼接成路径字符串
            loc_str = ".".join(str(part) for part in loc) if loc else "<root>"
            msg = err.get("msg", "")
            etype = err.get("type", "")
            lines.append(f"{loc_str} -> {msg} ({etype})")
        return "\n".join(lines)

    def _schema_summary(self) -> str:
        """生成 ``target_model`` 的 schema 摘要(properties 的 key 列表)。

        用于 ``parse_with_retry_hint`` 中给 LLM 的修复提示,避免把完整
        ``model_json_schema()`` 输出(可能很长)塞给 LLM。

        Returns:
            str: schema 摘要。无 ``target_model`` 或取 schema 失败时返回空串。
        """
        if self.target_model is None:
            return ""
        try:
            schema = self.target_model.model_json_schema()
        except Exception:
            return ""
        props = schema.get("properties", {})
        if not props:
            return ""
        keys = list(props.keys())
        return "字段: " + ", ".join(keys)


__all__ = ["PydanticParser"]
