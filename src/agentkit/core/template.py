"""core.template —— 共享模板与表达式引擎。

本模块是 AgentKit 各 Step 类型的共享基础设施,负责三件事:

    1. 模板解析:把字符串中的 {{var}} 占位符替换为 Context 中的值,
       把 ${ENV_VAR} 替换为环境变量。支持点号路径 {{order.id}}。
    2. 块语法:支持 {{#if expr}}...{{/if}}(含 {{#else}})与
       {{#each list}}...{{/each}} 条件与循环,避免复杂 prompt 只能在 Python
       侧预处理。块内可嵌套,each 循环体可用 {{this}} / {{index}} 引用当前元素。
    3. 表达式求值:对 {{intent}} == "query" / len('{{ids}}') > 0
       这类布尔/算术表达式求值,供 ConditionStep 的 when 与 {{#if}} 使用。

安全设计:eval_expression 绝不使用 eval,而是把 {{var}} 替换为 Python
字面量后,用基于 ast 的有限求值器递归求值,只放行白名单节点(常量 / 比较 /
布尔运算 / 一元 / 二元 / 少量内建函数 / 字面量容器),禁止 import / 属性
访问 / 任意函数调用,杜绝代码注入。块语法的条件求值复用 eval_expression,
不引入新的求值通路。

设计原则:
- 高度模块化:仅依赖标准库(re / os / ast)与 TYPE_CHECKING 下的 Context,
  不依赖其他 agentkit 子模块,无循环依赖。
- 安全:ast 有限求值器,白名单节点,禁止危险操作。
- 类型注解完整,中文 docstring 与注释。

公开 API:
    - TemplateError:      模板/表达式解析或求值错误
    - resolve_template:   把 {{var}} / ${ENV} 替换为值(str 化),支持块语法
    - resolve_value:      通用解析(整体单 {{var}} 返回原对象;dict/list 递归)
    - eval_expression:    对表达式求值(ast 有限求值器)
"""

from __future__ import annotations

import ast
import copy
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentkit.core.context import Context


__all__ = [
    "TemplateError",
    "resolve_template",
    "resolve_value",
    "eval_expression",
]

# 非贪婪匹配 {{ ... }},允许内部空白;捕获组为变量路径(如 order.id)。
_VAR_PATTERN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
# 匹配 ${VAR_NAME};环境变量名以大写字母/下划线开头。
_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
# 检测整体是否为单个 {{var}} 引用(首尾空白允许),用于 resolve_value 返回原对象。
_SINGLE_VAR_PATTERN = re.compile(r"^\s*\{\{\s*([^}]+?)\s*\}\}\s*$")
# 裸字面量替换:null / true / false(用 word boundary 避免误伤变量名)。
_NULL_PATTERN = re.compile(r"\bnull\b")
_TRUE_PATTERN = re.compile(r"\btrue\b")
_FALSE_PATTERN = re.compile(r"\bfalse\b")
# 块语法标签:{{#if EXPR}} / {{#each EXPR}} / {{#else}} / {{/if}} / {{/each}}。
# 命名组 open / expr / close 便于区分开标签、else、闭标签三类。
_BLOCK_TAG_PATTERN = re.compile(
    r"\{\{#(?P<open>if|each)\s+(?P<expr>.+?)\}\}"
    r"|\{\{#else\}\}"
    r"|\{\{/(?P<close>if|each)\}\}"
)

# eval_expression 允许调用的内建函数白名单。
_ALLOWED_FUNCS = {"len", "str", "int", "float", "bool", "abs", "min", "max"}


class TemplateError(Exception):
    """模板/表达式解析或求值错误。"""


def _resolve_path(path: str, ctx: "Context") -> Any:
    """按点号路径从 Context 取值。

    形如 order.user.id:先 ctx.get('order'),再依次取 ['user'] / ['id']
    (dict 用 [key],对象用 getattr)。任一段缺失抛 KeyError。
    """
    segments = path.split(".")
    first = segments[0]
    current: Any = ctx.get(first)
    for seg in segments[1:]:
        current = _descend(current, seg, path)
    return current


def _descend(obj: Any, seg: str, full_path: str) -> Any:
    """从 obj 取下一级 seg。dict 用 [],对象用 getattr。缺失抛 KeyError。"""
    if isinstance(obj, dict):
        if seg in obj:
            return obj[seg]
        raise KeyError(f"路径 {full_path!r} 在字段 {seg!r} 处缺失(dict 无此键)")
    try:
        # 先尝试 item 访问(覆盖 FrozenDict / MappingProxyType),排除序列类型
        if hasattr(obj, "__getitem__") and not isinstance(
            obj, (str, bytes, list, tuple)
        ):
            try:
                return obj[seg]
            except (KeyError, IndexError, TypeError):
                pass
        # 回退到属性访问(覆盖普通对象 / ReadOnlyProxy)
        if hasattr(obj, seg):
            return getattr(obj, seg)
    except Exception:
        pass
    raise KeyError(
        f"路径 {full_path!r} 在字段 {seg!r} 处缺失(类型 {type(obj).__name__})"
    )


def _to_plain_literal(value: Any) -> Any:
    """递归把冻结/代理对象转为可被 ast 解析的纯 Python 字面量。

    Context 存储的值会被冻结(dict→FrozenDict、list/tuple→tuple、对象→
    ReadOnlyProxy),其 ``repr`` 是构造调用(如 ``FrozenDict({...})``)无法被
    ast 解析。本函数先 ``copy.deepcopy`` 解冻(FrozenDict→dict、
    ReadOnlyProxy→目标深拷贝),再把容器统一转为 dict/list,标量原样返回,
    使 ``repr`` 结果始终是合法的 Python 字面量。

    Args:
        value: 任意值(可能被冻结)。

    Returns:
        Any: 纯 Python 字面量(dict / list / str / int / float / bool / None)。
    """
    # deepcopy 解冻:FrozenDict.__deepcopy__ 返回可变 dict;
    # ReadOnlyProxy.__deepcopy__ 返回目标的可变深拷贝;tuple 保留为 tuple。
    try:
        value = copy.deepcopy(value)
    except Exception:
        # deepcopy 失败时尽量继续(下游兜底用 str)
        pass
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, dict):
        return {k: _to_plain_literal(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain_literal(v) for v in value]
    if isinstance(value, (set, frozenset)):
        # set/frozenset 字面量在表达式中易与 dict 字面量混淆,统一转 list
        return [_to_plain_literal(v) for v in value]
    # pydantic BaseModel:model_dump 转 dict
    if hasattr(value, "model_dump"):
        try:
            return _to_plain_literal(value.model_dump())
        except Exception:
            pass
    # 兜底:转 str,避免 repr 产生不可解析的构造调用
    return str(value)


def _to_literal_str(value: Any) -> str:
    """把值转为可放进表达式的 Python 字面量字符串。

    bool 必须在 int 之前判断(因 bool 是 int 子类)。容器/冻结对象经
    :func:`_to_plain_literal` 解冻后 ``repr``,保证 ast 可解析为字面量。

    Args:
        value: 任意值。

    Returns:
        str: 可嵌入表达式的字面量(标量裸值;str/容器带引号/括号)。
    """
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return repr(value)
    # 容器/冻结对象:解冻后 repr,得到合法 dict/list 字面量
    return repr(_to_plain_literal(value))


def resolve_template(text: str, ctx: "Context") -> str:
    """把字符串里的 {{var}} 替换为 Context 中的值,${ENV} 替换为环境变量。

    支持块语法(在变量替换前处理):

    - ``{{#if path}}...{{/if}}``:path 为点号路径(如 ``extracted.truncated``),
      取值后按 Python truthy 判断:truthy 保留块体,falsy 移除。
    - ``{{#if path}}...{{#else}}...{{/if}}``:truthy 取前段,falsy 取后段。
    - ``{{#each list}}...{{/each}}``:对 list 每个元素渲染块体,循环体内可用
      ``{{this}}``(当前元素)与 ``{{index}}``(当前序号)引用。块体可嵌套。

    复杂布尔条件(如 ``count > 3 and flag``)请使用 ConditionStep,其 ``when``
    支持完整表达式求值。块语法的 ``if`` 刻意限定为路径式,以避免 ``{{}}`` 与
    块标签 ``}}`` 的定界冲突,保持解析简洁与安全。

    处理顺序:``${ENV}`` → 块语法 → ``{{var}}``。块体内的 ``{{var}}`` 在块
    求值后由统一的变量替换处理;``{{#each}}`` 块体在每次迭代的局部作用域下
    完整解析(故能正确引用 ``{{this}}`` / ``{{index}}``)。

    - {{order.id}}:点号路径取值后 str() 化替换;None -> 'None'。
    - ${MY_VAR}:os.environ.get;缺失时保留原文(环境变量可能运行时才设置)。
    - 非 str 输入原样返回。

    Raises:
        KeyError: 引用的变量在 Context 中不存在。
        TemplateError: 块语法不闭合或 each 目标不可迭代。
    """
    if not isinstance(text, str):
        return text  # type: ignore[return-value]

    def _env_repl(m: re.Match) -> str:
        name = m.group(1)
        return os.environ.get(name, m.group(0))

    text = _ENV_PATTERN.sub(_env_repl, text)
    # 块语法处理(可能递归调用 resolve_template 处理 each 块体)
    text = _resolve_blocks(text, ctx)

    def _var_repl(m: re.Match) -> str:
        path = m.group(1).strip()
        value = _resolve_path(path, ctx)
        return "None" if value is None else str(value)

    return _VAR_PATTERN.sub(_var_repl, text)


# ---------------------------------------------------------------------------
# 块语法实现:{{#if}} / {{#else}} / {{#each}}
# ---------------------------------------------------------------------------
# 设计说明:
#   - 块语法在变量替换之前处理(见 resolve_template),使块体内的 {{var}} 能
#     在正确的作用域下(尤其 {{#each}} 的 this/index)被替换。
#   - {{#if}} 的条件复用 eval_expression,不引入新的求值通路,保持安全模型一致。
#   - {{#each}} 对每个元素构造 _ScopeContext(叠加 this/index 的局部作用域),
#     再对块体完整调用 resolve_template,使块体内的嵌套块与 {{var}} 均正确解析。
#   - 嵌套块通过 _extract_block 的深度计数正确匹配开闭标签。


# 哨兵:区分"未传 default"与"显式传 None 作为 default"(对齐 Context.get 语义)。
# 仅供 _ScopeContext.get 使用,使缺失 key 时透传给父 Context 抛 KeyError。
_MISSING: Any = object()


class _ScopeContext:
    """临时作用域上下文:在父 Context 上叠加局部变量。

    仅供 ``{{#each}}`` 循环使用,为块体提供 ``this`` / ``index`` 两个局部变量,
    其余查找透传到父 Context。实现 Context 的 ``get`` 契约(缺失抛 KeyError),
    使其可被 :func:`_resolve_path` / :func:`resolve_template` 透明使用(duck typing)。

    Attributes:
        _parent:    父 Context(或另一个 _ScopeContext,支持嵌套 each)。
        _overrides: 局部变量覆盖(如 ``{"this": item, "index": i}``)。
    """

    __slots__ = ("_parent", "_overrides")

    def __init__(self, parent: Any, overrides: dict[str, Any]) -> None:
        self._parent = parent
        self._overrides = overrides

    def get(self, key: str, default: Any = _MISSING) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        if default is _MISSING:
            return self._parent.get(key)
        return self._parent.get(key, default)


def _extract_block(
    text: str, pos: int, block_type: str
) -> tuple[str, str | None, str]:
    """从 ``pos`` 起查找与 ``block_type`` 匹配的闭标签。

    用深度计数正确处理同类型块的嵌套(如 if 内嵌 if、each 内嵌 each);
    不同类型块的标签不计入深度(各自独立匹配)。``{{#else}}`` 仅在顶层
    (depth==1)且 ``block_type == "if"`` 时记录为分支点(each 不支持 else)。

    Args:
        text:       完整模板文本。
        pos:        开标签结束位置(块体起始)。
        block_type: ``"if"`` 或 ``"each"``。

    Returns:
        tuple: ``(body, else_body, rest)``。
            - body: 开标签到 else/闭标签之间的内容。
            - else_body: else 到闭标签之间的内容;无 else 时为 None。
            - rest: 闭标签之后的剩余文本。

    Raises:
        TemplateError: 未找到匹配的闭合标签。
    """
    depth = 1
    i = pos
    else_start: int | None = None
    while True:
        m = _BLOCK_TAG_PATTERN.search(text, i)
        if m is None:
            raise TemplateError(
                f"块 {{{{#{block_type}}}}} 未闭合,缺少 {{{{/{block_type}}}}}"
            )
        open_tag = m.group("open")
        close_tag = m.group("close")
        is_else = open_tag is None and close_tag is None
        if open_tag == block_type:
            depth += 1
        elif close_tag == block_type:
            depth -= 1
            if depth == 0:
                close_start = m.start()
                close_end = m.end()
                if else_start is not None:
                    body = text[pos:else_start]
                    else_body = text[else_start + len("{{#else}}") : close_start]
                else:
                    body = text[pos:close_start]
                    else_body = None
                return body, else_body, text[close_end:]
        elif is_else:
            # 仅记录顶层(当前块)的 else,嵌套块的 else 归属其自身块
            if depth == 1 and block_type == "if" and else_start is None:
                else_start = m.start()
        i = m.end()


def _resolve_each_items(expr: str, ctx: "Context") -> list:
    """解析 ``{{#each EXPR}}`` 的迭代目标。

    EXPR 作为点号路径(:func:`_resolve_path`)取值;None 视为空列表(容错);
    list / tuple 转为 list;其他类型抛 :class:`TemplateError`。

    Args:
        expr: 路径表达式(如 ``orders`` / ``order.items``)。
        ctx:  当前上下文。

    Returns:
        list: 可迭代的元素列表。

    Raises:
        TemplateError: 目标非 list/tuple/None。
        KeyError: 路径不存在。
    """
    value = _resolve_path(expr, ctx)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    raise TemplateError(
        f"{{{{#each {expr}}}}} 目标不是可迭代序列: {type(value).__name__}"
    )


def _resolve_blocks(text: str, ctx: "Context") -> str:
    """递归处理 ``{{#if}}`` / ``{{#each}}`` 块语法,返回展开后的文本。

    块体内的 ``{{var}}`` 不在此处理(if 块留给外层 resolve_template 统一替换;
    each 块在每次迭代以局部作用域完整调用 resolve_template)。嵌套块通过递归
    与 :func:`_extract_block` 的深度计数正确处理。

    不含块标签的文本原样返回,交由外层变量替换处理。

    Args:
        text: 已完成 ENV 替换的模板文本。
        ctx:  当前上下文(或 _ScopeContext)。

    Returns:
        str: 块语法展开后的文本(仍可能含 ``{{var}}``,待外层替换)。
    """
    m = _BLOCK_TAG_PATTERN.search(text)
    if m is None:
        return text
    # 块必须以开标签起始;孤立的 {{#else}} / {{/if}} 当普通文本(不在此处理)
    if m.group("open") is None:
        return text

    block_type = m.group("open")
    expr = m.group("expr").strip()
    prefix = text[: m.start()]
    body, else_body, rest = _extract_block(text, m.end(), block_type)

    if block_type == "if":
        # if 的 EXPR 为点号路径(如 extracted.truncated),取值后按 Python
        # truthy 判断。复杂布尔条件请使用 ConditionStep(支持完整表达式)。
        # 这样设计避免 {{}} 与块标签 }} 的定界冲突,保持解析简洁与安全。
        value = _resolve_path(expr, ctx)
        chosen = body if value else (else_body or "")
        # if 不改变作用域:chosen 内的嵌套块递归处理,{{var}} 留给外层替换
        rendered = _resolve_blocks(chosen, ctx)
    else:  # each
        items = _resolve_each_items(expr, ctx)
        parts: list[str] = []
        for idx, item in enumerate(items):
            scope: Any = _ScopeContext(ctx, {"this": item, "index": idx})
            # each 块体需在局部作用域下完整解析(块 + var),故调 resolve_template
            parts.append(resolve_template(body, scope))
        rendered = "".join(parts)

    # rest 递归处理后续块(若有)
    return prefix + rendered + _resolve_blocks(rest, ctx)


def resolve_value(template: Any, ctx: "Context") -> Any:
    """通用模板解析。

    - str:先解析 ${ENV} 再解析 {{var}}。若整个字符串就是一个 {{var}} 引用,
      返回 Context 中的原始对象(不 str 化,保留 dict/list/对象结构);否则
      返回拼接后的 str。
    - dict:对每个 value 递归解析。
    - list:对每个元素递归解析。
    - 其他类型:原样返回。

    Raises:
        KeyError: 引用的变量不存在。
    """
    if isinstance(template, str):
        return _resolve_str_value(template, ctx)
    if isinstance(template, dict):
        return {k: resolve_value(v, ctx) for k, v in template.items()}
    if isinstance(template, list):
        return [resolve_value(v, ctx) for v in template]
    return template


def _resolve_str_value(s: str, ctx: "Context") -> Any:
    """解析单个字符串模板。整体单 {{var}} 返回原始对象,否则返回拼接 str。"""
    m = _SINGLE_VAR_PATTERN.match(s)
    if m:
        return _resolve_path(m.group(1).strip(), ctx)
    return resolve_template(s, ctx)


def eval_expression(expr: str, ctx: "Context") -> Any:
    """对表达式求值(用于 ConditionStep 的 when)。

    表达式形如:
        '{{intent}}' == "query"
        len('{{orders.user_ids}}') > 0
        '{{x}}' != null
        '{{count}}' > 3 and '{{count}}' < 10

    求值策略:
        1. 把每个 {{var}} 替换为 _to_literal_str(_resolve_path(var, ctx));
        2. 把裸 null/true/false 替换为 None/True/False(word boundary);
        3. 用 ast.parse(expr, mode="eval") 解析;
        4. 递归访问 AST 节点求值,只放行白名单节点,其他抛 TemplateError。

    Raises:
        TemplateError: 表达式包含不支持的节点或语法错误。
        KeyError: 引用的变量不存在。
    """

    def _var_repl(m: re.Match) -> str:
        path = m.group(1).strip()
        value = _resolve_path(path, ctx)
        return _to_literal_str(value)

    expr = _VAR_PATTERN.sub(_var_repl, expr)
    expr = _NULL_PATTERN.sub("None", expr)
    expr = _TRUE_PATTERN.sub("True", expr)
    expr = _FALSE_PATTERN.sub("False", expr)

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise TemplateError(f"表达式语法错误: {expr!r}: {e}") from e

    try:
        return _eval_node(tree.body)
    except TemplateError:
        raise
    except KeyError:
        raise
    except Exception as e:
        raise TemplateError(f"表达式求值失败: {expr!r}: {e}") from e


# AST 比较运算符映射
_CMP_OPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

# AST 二元运算符映射
_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
    ast.FloorDiv: lambda a, b: a // b,
}


def _eval_node(node: ast.AST) -> Any:
    """递归求值 AST 节点(白名单)。遇到非白名单节点抛 TemplateError。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        if node.id == "None":
            return None
        raise TemplateError(f"不允许的名称引用: {node.id!r}")
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator)
            op_func = _CMP_OPS.get(type(op))
            if op_func is None:
                raise TemplateError(f"不支持的比较运算符: {type(op).__name__}")
            if not op_func(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value_node in node.values:
                result = _eval_node(value_node)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            result = False
            for value_node in node.values:
                result = _eval_node(value_node)
                if result:
                    return result
            return result
        raise TemplateError(f"不支持的布尔运算符: {type(node.op).__name__}")
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise TemplateError(f"不支持的一元运算符: {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        op_func = _BIN_OPS.get(type(node.op))
        if op_func is None:
            raise TemplateError(f"不支持的二元运算符: {type(node.op).__name__}")
        return op_func(left, right)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise TemplateError("仅允许直接调用内建函数,禁止属性调用")
        func_name = node.func.id
        if func_name not in _ALLOWED_FUNCS:
            raise TemplateError(f"不允许调用的函数: {func_name!r}")
        func = _BUILTINS[func_name]
        args = [_eval_node(a) for a in node.args]
        kwargs = {
            kw.arg: _eval_node(kw.value) for kw in node.keywords if kw.arg
        }
        return func(*args, **kwargs)
    if isinstance(node, ast.List):
        return [_eval_node(e) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(e) for e in node.elts)
    if isinstance(node, ast.Set):
        return {_eval_node(e) for e in node.elts}
    if isinstance(node, ast.Dict):
        return {
            _eval_node(k): _eval_node(v)
            for k, v in zip(node.keys, node.values)
        }
    raise TemplateError(f"不支持的表达式节点: {type(node).__name__}")


# 内建函数表(延迟绑定,便于维护)
_BUILTINS = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
}
