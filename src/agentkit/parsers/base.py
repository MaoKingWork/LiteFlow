"""parsers.base —— 输出解析器抽象基类与解析结果。

本模块定义 AgentKit 解析器子包的统一抽象层。LLM 的文本输出需要被解析为
结构化对象（pydantic 模型等）才能被下游 Step 消费，不同解析策略（纯文本 /
JSON / Markdown 代码块等）通过继承 ``Parser`` 实现。

设计原则：
    - 高度模块化：仅依赖 ``pydantic`` 与标准库（abc / typing），无循环依赖。
    - 宽容降级：``ParseResult`` 同时承载成功与失败，调用方按 ``ok`` 分支处理。
    - 可拓展：新增解析器只需继承 ``Parser`` 并实现 ``parse``。
    - 类型注解完整，中文 docstring 与注释。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class ParseResult:
    """解析结果。

    封装一次解析的输出，无论成功或失败都返回本类型（不抛异常），
    便于调用方统一处理与聚合。

    Attributes:
        success: 是否解析成功。
        value:   解析得到的值（成功时为结构化对象或文本，失败时通常为 None）。
        error:   失败原因描述（成功时为空字符串）。
    """

    def __init__(self, success: bool, value: Any = None, error: str = ""):
        self.success = success
        self.value = value
        self.error = error

    @property
    def ok(self) -> bool:
        """解析是否成功（``success`` 的语义化别名）。"""
        return self.success

    def __repr__(self) -> str:
        if self.success:
            return f"ParseResult(ok=True, value={self.value!r})"
        return f"ParseResult(ok=False, error={self.error!r})"


class Parser(ABC):
    """输出解析器抽象基类。

    所有具体解析器（``TextParser`` / ``PydanticParser`` 等）均继承本类
    并实现 ``parse`` 方法。

    Attributes:
        target_model: 期望解析得到的 pydantic 模型类。为 None 表示不转结构化对象
                      （如纯文本解析器）。子类可在类属性或实例属性上覆盖。
    """

    # 类级默认值；子类可覆盖或在实例上覆盖
    target_model: type[BaseModel] | None = None

    @abstractmethod
    def parse(self, text: str) -> ParseResult:
        """把文本解析为结构化结果。

        Args:
            text: 待解析的原始文本（通常是 LLM 输出）。

        Returns:
            ParseResult: 解析结果。成功时 ``ok=True`` 且 ``value`` 为结构化对象；
                        失败时 ``ok=False`` 且 ``error`` 描述原因。
        """


__all__ = ["ParseResult", "Parser"]
