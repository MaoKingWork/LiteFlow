"""core.conversation —— 会话缓存核心模块。

本模块实现 AgentKit 的会话缓存能力：把 LLM 多轮对话历史以「内联 + 旁路」
双路径持久化到 :class:`Context`，支持跨轮 continue / fork 分支与会话级
一致性校验。

设计要点（对齐 ``docs/conversation-cache-design.md`` v0.5 §3 / §7.1 /
§8.1.2 / §8.2）：
    - **双路径存储**：
        * 内联（``_type=conversation``）：小会话直接冻结进 Context，与普通
          Step output 同构，snapshot/restore 天然支持。
        * 旁路（``_type=conversation_ref``）：大会话（> ``large_object_threshold``）
          经 :class:`ConversationStore` 内容寻址落盘，Context 仅持 ref + meta。
          旁路路径绕过 Context 的 LargeRef 机制——LargeRef 在 snapshot 时只保留
          摘要会丢历史，而 conversation_ref 是小对象，snapshot 完整保留 ref，
          restore 后可从 store 回填完整 messages。
    - **内容寻址去重**：同内容同 ref（md5），多轮增量只新增变化块。
    - **派生属性指纹**：tools_sig / system_hash 作为一致性校验与缓存前缀稳定锚。
    - **一致性校验档位**：strict 档 provider/model/tools 不一致即抛
      (正确性风险)；system 不一致仅 warning (性能问题)。passthrough 档
      全部降级为 warning/debug。
    - **不隐式改消息**：:class:`MessageNormalizer` 默认 identity，strict /
      passthrough 均不自动调用，避免破坏缓存前缀稳定性。

模块化原则：
    - 仅依赖标准库 + :mod:`agentkit.llm.base`（运行时）与
      :mod:`agentkit.config`（延迟导入，避免模块加载期触发）。
    - :class:`Context` / :class:`AgentConfig` 仅在 ``TYPE_CHECKING`` 下导入，
      避免循环依赖。

公开 API：
    - CONVERSATION_TYPE / CONVERSATION_REF_TYPE: 类型标记常量
    - ConversationTypeError / ConversationCompatError: 异常类
    - messages_to_dicts / dicts_to_messages: LLMMessage 序列化
    - pack_conversation / unpack_conversation: 内联格式封装/解析
    - ConversationStore / LocalConversationStore: 内容寻址会话存储
    - set_conversation_store / get_conversation_store: 全局存储实例注入
    - fork_at_user: 按 user 消息索引分支
    - check_compatibility: 会话一致性校验
    - load_and_validate: 统一加载入口（内联 + 旁路）
    - save_conversation: 统一保存入口（自动选内联/旁路）
    - MessageNormalizer: 消息格式适配扩展点
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
from typing import TYPE_CHECKING

from agentkit.llm.base import LLMMessage, ToolCall

if TYPE_CHECKING:
    from agentkit.core.agent import AgentConfig
    from agentkit.core.context import Context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# T1.1 类型标记常量 + 异常类
# ---------------------------------------------------------------------------
CONVERSATION_TYPE = "conversation"
"""内联会话存储的类型标记。"""

CONVERSATION_REF_TYPE = "conversation_ref"
"""旁路会话存储（仅持 ref）的类型标记。"""


class ConversationTypeError(ValueError):
    """key 存在但非会话类型时抛出，提示可能重名冲突。

    当 Context 中某 key 存在但其值既非 ``CONVERSATION_TYPE`` 也非
    ``CONVERSATION_REF_TYPE`` 时抛出。常见原因是与普通 Step output 变量重名，
    例如 ``output: chat`` 与 ``conversation.key: chat`` 冲突。
    """

    def __init__(self, key: str, actual: object) -> None:
        super().__init__(
            f"Context key {key!r} 不是有效会话数据 "
            f"(实际类型: {type(actual).__name__})。"
            f"可能是与普通 Step output 变量重名冲突。"
        )


class ConversationCompatError(ValueError):
    """strict 档工具签名 / provider / model 不一致时抛出。

    历史中的 tool_calls 与新 schema 错位属正确性风险，strict 档直接拒绝；
    调用方需切换 ``compat: passthrough`` 并自行承担风险，或显式提供
    :class:`MessageNormalizer` 做格式转换。
    """


# ---------------------------------------------------------------------------
# T1.2 序列化：LLMMessage ↔ dict
# ---------------------------------------------------------------------------
def messages_to_dicts(messages: list[LLMMessage]) -> list[dict]:
    """LLMMessage 列表 → 可 JSON 序列化的 dict 列表。

    使用 :func:`dataclasses.asdict` 递归转换，保留所有字段
    （role / content / tool_calls / tool_call_id / name / reasoning_content）。
    嵌套的 :class:`ToolCall` 也会被递归转为 dict
    （id / name / arguments），便于 ``json.dumps``。

    Args:
        messages: LLMMessage 列表。

    Returns:
        list[dict]: 可直接 ``json.dumps`` 的 dict 列表。
    """
    return [dataclasses.asdict(m) for m in messages]


def dicts_to_messages(dicts: list[dict]) -> list[LLMMessage]:
    """dict 列表 → LLMMessage 列表（反序列化）。

    与 :func:`messages_to_dicts` 互为逆运算。嵌套的 tool_calls dict 会被
    还原为 :class:`ToolCall` 实例。

    Args:
        dicts: 由 :func:`messages_to_dicts` 产生的 dict 列表。

    Returns:
        list[LLMMessage]: 还原后的消息列表。
    """
    messages: list[LLMMessage] = []
    for d in dicts:
        tool_calls: list[ToolCall] | None = None
        tcs = d.get("tool_calls")
        if tcs:
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    name=tc["name"],
                    arguments=tc["arguments"],
                )
                for tc in tcs
            ]
        messages.append(
            LLMMessage(
                role=d["role"],
                content=d.get("content"),
                tool_calls=tool_calls,
                tool_call_id=d.get("tool_call_id"),
                name=d.get("name"),
                reasoning_content=d.get("reasoning_content"),
            )
        )
    return messages


# ---------------------------------------------------------------------------
# T1.3 派生属性指纹
# ---------------------------------------------------------------------------
def _tools_signature(tools: list[str]) -> str:
    """工具名排序去重后取短 md5（前 8 位），作为 tools 一致性指纹。

    排序去重保证「相同工具集不同顺序」生成相同签名，避免列表顺序抖动
    导致误判不一致。例如 ``["search", "calc"]`` 与 ``["calc", "search"]``
    均得到 ``md5("calc,search")[:8]``。

    Args:
        tools: 工具名列表。

    Returns:
        str: 8 位 hex 指纹。
    """
    sorted_tools = sorted(set(tools))
    return hashlib.md5(",".join(sorted_tools).encode()).hexdigest()[:8]


def _hash_system(system_text: str) -> str:
    """system 消息短哈希（md5 前 8 位）。

    缓存前缀稳定锚 + 一致性校验双用：system 变更意味着 prompt 前缀断裂，
    历史缓存命中率将下降（性能问题，非正确性问题）。

    Args:
        system_text: system 提示词文本。

    Returns:
        str: 8 位 hex 哈希。
    """
    return hashlib.md5(system_text.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# T1.4 内联格式封装/解析
# ---------------------------------------------------------------------------
def pack_conversation(messages: list[LLMMessage], agent: AgentConfig) -> dict:
    """封装为带完整 meta 快照的内联存储格式。

    返回结构::

        {
            "_type": "conversation",
            "messages": [...],
            "meta": {
                "provider": "...",
                "model": "...",
                "tools_sig": "...",
                "system_hash": "...",
                "cached_tokens": 0,
            },
        }

    meta 携带全部派生属性快照，供 :func:`check_compatibility` 校验。
    ``cached_tokens`` 初始为 0，由 :func:`save_conversation` 累计填充。

    Args:
        messages: 完整消息列表。
        agent:   当前 AgentConfig（用于派生 meta）。

    Returns:
        dict: 内联存储格式。
    """
    return {
        "_type": CONVERSATION_TYPE,
        "messages": messages_to_dicts(messages),
        "meta": {
            "provider": agent.provider or "",
            "model": agent.model or "",
            "tools_sig": _tools_signature(agent.tools),
            "system_hash": _hash_system(agent.system or ""),
            "cached_tokens": 0,
        },
    }


def unpack_conversation(raw: object, key: str) -> tuple[list[LLMMessage], dict]:
    """解析内联存储格式，返回 (messages, meta)。

    类型不匹配时抛 :class:`ConversationTypeError`，提示可能重名冲突。

    Args:
        raw: Context.get 返回的值（可能是 FrozenDict / dict）。
        key: 存储键名（用于异常文案定位）。

    Returns:
        tuple[list[LLMMessage], dict]: (消息列表, meta dict)。

    Raises:
        ConversationTypeError: raw 非会话类型（缺失 ``_type=conversation``）。
    """
    from collections.abc import Mapping

    if not isinstance(raw, Mapping) or raw.get("_type") != CONVERSATION_TYPE:
        raise ConversationTypeError(key, raw)
    return dicts_to_messages(list(raw["messages"])), dict(raw.get("meta", {}))


# ---------------------------------------------------------------------------
# T1.5 ConversationStore 协议 + LocalConversationStore
# ---------------------------------------------------------------------------
class ConversationStore:
    """内容寻址会话存储协议。

    ``save`` 返回 ref key（内容 md5），``load`` 按 ref 回填字节。
    内容寻址保证同内容同 ref，天然去重——多轮增量只新增变化块。

    实现方约定：
        - ``save(data)`` 幂等：同 bytes 返回同 ref，不重复落盘。
        - ``load(ref)`` 返回原始 bytes。
        - ``exists(ref)`` 判断 ref 是否已落盘。
    """

    def save(self, data: bytes) -> str:
        """保存 bytes，返回内容寻址 ref（md5）。"""
        raise NotImplementedError

    def load(self, ref: str) -> bytes:
        """按 ref 加载 bytes。"""
        raise NotImplementedError

    def exists(self, ref: str) -> bool:
        """判断 ref 是否已落盘。"""
        raise NotImplementedError


class LocalConversationStore(ConversationStore):
    """内容寻址本地会话存储。

    同内容同 ref（去重），原子 rename 落盘。目录布局::

        {base_dir}/conversations/{ref}

    写序（对齐 :class:`ArtifactStore` 五步写序，去掉事件发布）：
        1. 写 ``{ref}.tmp``
        2. ``flush + fsync + close``
        3. ``os.replace(.tmp → {ref})`` —— 同文件系统内原子

    崩溃窗口兜底：
        - 1-3 之间崩溃 → ``.tmp`` 残留，GCSweeper 直接删
        - 3 之后崩溃 → 完整文件，内容寻址去重保证可重建

    Args:
        base_dir: 存储根目录，默认从
                  ``config.get_default("conversation_store_base_dir")`` 读取。
    """

    def __init__(self, base_dir: str | None = None) -> None:
        from agentkit.config import get_default

        self._base = base_dir or get_default("conversation_store_base_dir")
        self._conv_dir = os.path.join(self._base, "conversations")

    def _ref_path(self, ref: str) -> str:
        """ref 对应的最终路径。"""
        return os.path.join(self._conv_dir, ref)

    def _tmp_path(self, ref: str) -> str:
        """写入临时路径（rename 前）。"""
        return os.path.join(self._conv_dir, f"{ref}.tmp")

    def save(self, data: bytes) -> str:
        """保存 bytes，返回内容寻址 ref（md5 hex）。

        同内容同 ref（内容寻址去重）：若 ref 文件已存在则直接返回，不重复落盘。

        Args:
            data: 会话序列化后的字节。

        Returns:
            str: md5 hex ref。
        """
        ref = hashlib.md5(data).hexdigest()
        path = self._ref_path(ref)

        # 内容寻址去重：已存在则直接返回
        if os.path.exists(path):
            return ref

        os.makedirs(self._conv_dir, exist_ok=True)
        tmp_path = self._tmp_path(ref)

        # 步骤 1-2: 写 .tmp + flush + fsync + close
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        # 步骤 3: 原子 rename（同文件系统内）
        os.replace(tmp_path, path)
        return ref

    def load(self, ref: str) -> bytes:
        """按 ref 加载 bytes。

        Args:
            ref: md5 hex ref。

        Returns:
            bytes: 原始会话字节。

        Raises:
            FileNotFoundError: ref 文件不存在。
        """
        with open(self._ref_path(ref), "rb") as f:
            return f.read()

    def exists(self, ref: str) -> bool:
        """判断 ref 是否已落盘。"""
        return os.path.exists(self._ref_path(ref))


# ---------------------------------------------------------------------------
# T1.6 全局存储实例注入
# ---------------------------------------------------------------------------
_global_store: ConversationStore | None = None


def set_conversation_store(store: ConversationStore) -> None:
    """注入全局 ConversationStore 实例。

    默认懒加载 :class:`LocalConversationStore`；调用方可注入自定义实现
    （如远程存储）以替换默认行为。测试中常注入基于 ``tmp_path`` 的实例
    避免污染工作目录。

    Args:
        store: ConversationStore 实例。
    """
    global _global_store
    _global_store = store


def get_conversation_store() -> ConversationStore:
    """获取全局 ConversationStore。

    未注入时懒加载 :class:`LocalConversationStore`（使用 config 默认 base_dir）。

    Returns:
        ConversationStore: 当前全局实例。
    """
    global _global_store
    if _global_store is None:
        _global_store = LocalConversationStore()
    return _global_store


# ---------------------------------------------------------------------------
# T1.7 fork_at_user
# ---------------------------------------------------------------------------
def fork_at_user(
    messages: list[LLMMessage],
    fork_at: int | str = "last",
    new_prompt: str | None = None,
) -> list[LLMMessage]:
    """按 user 消息索引分支，返回新列表（不修改输入）。

    - ``fork_at="last"``（默认）：截断到最后一个 user 消息，去掉其后所有
      （含末尾 assistant），保留该 user。
    - ``fork_at=N``（整数）：截断到第 N 个 user 消息（0-indexed），保留该
      user，去掉其后所有。
    - ``new_prompt`` 非 None 时：替换分支点 user 消息的 content。

    典型场景：continue 模式用 ``fork_at="last"`` + ``new_prompt`` 重写最后
    一轮 user；fork 模式用 ``fork_at=N`` 回到第 N 轮重走分支。

    Args:
        messages:  原始消息列表（不被修改）。
        fork_at:   分支点；``"last"`` 或非负整数（user 消息的 0-indexed 序号）。
        new_prompt: 替换分支点 user 消息 content 的新提示词；``None`` 不替换。

    Returns:
        list[LLMMessage]: 截断后的新消息列表。

    Raises:
        ValueError: 会话无 user 消息，或 ``fork_at`` 超出 user 消息范围，
                    或 ``fork_at`` 类型非法。
    """
    # 1. 找出所有 user 消息的索引（在 messages 中的位置）
    user_indices = [i for i, m in enumerate(messages) if m.role == "user"]
    if not user_indices:
        raise ValueError("会话中无 user 消息，无法 fork")

    # 2. 确定截断点（在 messages 中的位置）
    if fork_at == "last":
        cut_idx = user_indices[-1]
    elif isinstance(fork_at, int) and not isinstance(fork_at, bool):
        # bool 是 int 子类，需显式排除（True/False 不应被当作 1/0）
        if fork_at < 0 or fork_at >= len(user_indices):
            raise ValueError(
                f"fork_at={fork_at} 超出 user 消息范围 "
                f"[0, {len(user_indices) - 1}]"
            )
        cut_idx = user_indices[fork_at]
    else:
        raise ValueError(f"fork_at 仅支持 'last' 或整数，得到 {fork_at!r}")

    # 3. 截断到 cut_idx（含该 user 消息）
    result = list(messages[: cut_idx + 1])

    # 4. 替换 prompt（如果提供）
    if new_prompt is not None:
        result[-1] = LLMMessage(role="user", content=new_prompt)

    return result


# ---------------------------------------------------------------------------
# T1.8 check_compatibility
# ---------------------------------------------------------------------------
def check_compatibility(
    conv_meta: dict,
    agent: AgentConfig,
    compat: str,
    system_override: bool = False,
) -> None:
    """校验会话 meta 与当前 agent 的全部派生属性一致性。

    检查项（按严重度）：
        - **tools_sig 不一致**：工具集已变 → 历史中的 tool_calls 与新 schema
          错位，属正确性风险。strict 档 raise；passthrough 档 warning。
        - **provider/model 不一致**：跨 provider 消息结构约束不同，属正确性
          风险。同上。
        - **system_hash 不一致**：system 已变 → 缓存前缀断裂（性能问题，非
          正确性）。strict 档 warning（``system_override`` 显式覆盖时静默）；
          passthrough 档 debug。

    Args:
        conv_meta:        会话 meta dict
                         （provider / model / tools_sig / system_hash / cached_tokens）。
        agent:            当前 AgentConfig。
        compat:          ``"strict"`` | ``"passthrough"``。
        system_override: 是否显式设置了 system_override（True 时 system
                         不一致静默，因调用方已知并接受）。

    Raises:
        ConversationCompatError: strict 档下 tools_sig 或 provider/model 不一致。
    """
    sig = _tools_signature(agent.tools)
    sys_h = _hash_system(agent.system or "")
    pm_mismatch = (
        conv_meta.get("provider", "") != (agent.provider or "")
        or conv_meta.get("model", "") != (agent.model or "")
    )
    tool_mismatch = conv_meta.get("tools_sig", "") != sig
    sys_mismatch = conv_meta.get("system_hash", "") != sys_h

    if tool_mismatch or pm_mismatch:
        if compat == "strict":
            raise ConversationCompatError(
                f"会话与当前 agent 不一致: provider/model="
                f"{conv_meta.get('provider')}/{conv_meta.get('model')} → "
                f"{agent.provider}/{agent.model}, tools_sig 变更={tool_mismatch}。"
                f"continue/fork 会致 tool_calls 历史与 schema 错位。"
                f"如确需切换，设置 conversation.compat: passthrough 并自行承担风险。"
            )
        logger.warning("passthrough: 会话与 agent 不一致，未做格式转换")

    if sys_mismatch and not system_override:
        if compat == "strict":
            logger.warning("会话 system 与当前 agent 不一致，缓存前缀将断裂")
        else:
            logger.debug("passthrough: 会话 system 与 agent 不一致")


# ---------------------------------------------------------------------------
# T1.9 load_and_validate
# ---------------------------------------------------------------------------
def load_and_validate(
    ctx: Context,
    from_key: str,
    agent: AgentConfig,
    compat: str,
    system_override: bool = False,
) -> tuple[list[LLMMessage], dict]:
    """加载会话：内联直接取 messages，旁路 ref 从 ConversationStore 回填。

    加载后做 :func:`check_compatibility` 校验。

    Args:
        ctx:             Context 实例。
        from_key:        会话存储键名（已渲染的最终 key）。
        agent:           当前 AgentConfig。
        compat:          ``"strict"`` | ``"passthrough"``。
        system_override: 是否显式设置了 system_override。

    Returns:
        tuple[list[LLMMessage], dict]: (消息列表, meta dict)。

    Raises:
        KeyError:                   from_key 不存在（由 ctx.get 抛出）。
        ConversationTypeError:      key 存在但非会话类型。
        ConversationCompatError:     strict 档一致性校验失败。
    """
    from collections.abc import Mapping

    raw = ctx.get(from_key)  # 缺失会抛 KeyError

    if isinstance(raw, Mapping) and raw.get("_type") == CONVERSATION_REF_TYPE:
        # 旁路存储：从 ConversationStore 回填
        store = get_conversation_store()
        blob = store.load(raw["ref"])
        messages = dicts_to_messages(json.loads(blob.decode("utf-8")))
        meta = dict(raw.get("meta", {}))
    else:
        # 内联存储
        messages, meta = unpack_conversation(raw, from_key)

    check_compatibility(meta, agent, compat, system_override)
    return messages, meta


# ---------------------------------------------------------------------------
# T1.10 save_conversation
# ---------------------------------------------------------------------------
def save_conversation(
    ctx: Context,
    key: str,
    messages: list[LLMMessage],
    agent: AgentConfig,
    *,
    cached_tokens_total: int = 0,
) -> None:
    """保存会话：大于阈值走旁路存储（Context 仅持 ref），否则内联冻结。

    旁路路径绕过 Context 的 LargeRef 机制——LargeRef 在 snapshot 时只保留摘要
    会丢历史，而 conversation_ref 是小对象（ref + meta），snapshot 完整保留，
    断点续传不丢历史。

    Args:
        ctx:                 Context 实例。
        key:                 存储键名（已渲染的最终 key）。
        messages:            完整消息列表。
        agent:               当前 AgentConfig（用于派生 meta）。
        cached_tokens_total: 累计 cached_tokens 总值（由调用方 LLMStep 累计）。
    """
    from agentkit.config import get_default

    # 1. 构造内联格式的 conv（用于序列化 + meta）
    conv = pack_conversation(messages, agent)

    # 2. 写入累计 cached_tokens（由调用方累计好的总值）
    conv["meta"]["cached_tokens"] = cached_tokens_total

    # 3. 序列化 messages 为 bytes（用于判断大小 + 旁路存储）
    blob = json.dumps(conv["messages"], ensure_ascii=False).encode("utf-8")

    # 4. 判断大小，选择内联/旁路
    threshold = int(get_default("large_object_threshold"))

    if len(blob) > threshold:
        # 旁路存储：messages 落盘，Context 仅持 ref + meta
        store = get_conversation_store()
        ref = store.save(blob)
        ctx.set(
            key,
            {
                "_type": CONVERSATION_REF_TYPE,
                "store": "local",
                "ref": ref,
                "size": len(blob),
                "meta": conv["meta"],  # meta 内联，校验无需回读旁路
            },
        )
    else:
        # 内联冻结（ctx.set 会自动 _deep_freeze）
        ctx.set(key, conv)


# ---------------------------------------------------------------------------
# T1.11 MessageNormalizer 扩展点
# ---------------------------------------------------------------------------
class MessageNormalizer:
    """消息格式适配协议（默认 identity，不做转换）。

    provider 可注册专属 normalizer（如 ``AnthropicNormalizer`` 合并连续同
    角色消息、tool_calls 结构转换、多模态 content part 重排）。

    strict / passthrough 均不自动调用——需显式配置 normalizer 才生效，
    避免"隐式改消息"破坏缓存前缀稳定性。调用方在 :func:`load_and_validate`
    之后、发送给 LLM 之前显式调用 ``normalize``。

    子类覆盖 :meth:`normalize` 实现 provider 专属适配逻辑。
    """

    def normalize(
        self,
        messages: list[LLMMessage],
        conv_meta: dict,
        agent: AgentConfig,
    ) -> list[LLMMessage]:
        """返回适配后的消息列表（默认原样返回）。

        Args:
            messages:  原始消息列表。
            conv_meta: 会话 meta dict（可用于条件判断）。
            agent:     当前 AgentConfig。

        Returns:
            list[LLMMessage]: 适配后的消息列表（默认与输入相同）。
        """
        return messages


__all__ = [
    "CONVERSATION_TYPE",
    "CONVERSATION_REF_TYPE",
    "ConversationTypeError",
    "ConversationCompatError",
    "messages_to_dicts",
    "dicts_to_messages",
    "pack_conversation",
    "unpack_conversation",
    "ConversationStore",
    "LocalConversationStore",
    "set_conversation_store",
    "get_conversation_store",
    "fork_at_user",
    "check_compatibility",
    "load_and_validate",
    "save_conversation",
    "MessageNormalizer",
]
