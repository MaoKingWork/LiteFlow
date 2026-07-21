"""config —— AgentKit 全局配置中枢。

本模块是整个框架的配置中枢，集中管理所有默认值。核心模块（core / steps /
llm / tools 等）应从此处读取默认配置，而非在各自代码中硬编码。

设计原则：
    - 高度模块化：本模块不依赖任何其他 agentkit 子模块，可独立 import
    - 易于配置：所有可调参数集中于此，每项均带中文注释说明用途与使用方
    - 可拓展：新增默认值只需在 ``_DEFAULTS`` 中追加一行
    - 运行时可覆盖：通过 ``set_default`` 临时修改，``reset_default`` 恢复

线程安全说明：
    本模块采用模块级 dict 存储配置，单进程内同步访问是安全的。
    多线程并发读写时，建议在应用启动阶段完成 ``set_default`` 配置，
    运行阶段只读访问 ``get_default``。如需跨线程动态修改，请加外部锁。

公开 API：
    - get_default(key):         获取某项默认值
    - set_default(key, value):  运行时覆盖某项默认值
    - get_config():             返回所有默认值的只读副本
    - reset_default(key):       将某项重置回内置默认值
    - RetryPolicy:              重试策略数据类
    - default_retry_policy():   基于默认值构造 RetryPolicy 的便利函数
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast


# ---------------------------------------------------------------------------
# 内置默认值表
# ---------------------------------------------------------------------------
# 设计说明：
#   - 新增配置项只需在此处追加一行，无需改动任何函数实现
#   - 每项注释格式：用途说明 + 被哪个模块使用
#   - key 命名采用 snake_case，与 Python 变量风格一致
_DEFAULTS: dict[str, Any] = {
    # Context 判定"大对象"的阈值（字节）。超过此值的对象在快照中仅保留摘要，
    # 避免上下文膨胀。被 core.context 使用。
    "large_object_threshold": 1 * 1024 * 1024,  # 1MB
    # 默认重试次数（不含首次执行）。被 core.agent / steps / tools 通用重试逻辑使用。
    "default_retry_count": 1,
    # 默认重试退避策略：fixed（固定间隔）| exponential（指数退避）。
    # 被 RetryPolicy 及执行重试的模块使用。
    "default_retry_backoff": "fixed",
    # 默认重试退避基准秒数。fixed 策略下即每次间隔；exponential 下为底数倍增基数。
    # 被 RetryPolicy 及执行重试的模块使用。
    "default_retry_base_seconds": 5.0,
    # ParallelStep 默认最大并发数。被 steps.parallel_step 使用。
    "default_max_concurrency": 5,
    # ParallelStep 默认整体超时秒数。被 steps.parallel_step 使用。
    "default_parallel_timeout_seconds": 60.0,
    # 单个 Step 默认执行超时秒数。被 core.workflow / 各 Step 基类使用。
    "default_step_timeout_seconds": 300.0,
    # LLMStep 在 Function Call 模式下的最大轮次（防死循环）。被 steps.llm_step 使用。
    "default_max_tool_iterations": 5,
    # LoopStep 最大迭代次数（硬上限，防死循环）。被 steps.loop_step 使用。
    "default_max_loop_iterations": 100,
    # Context 快照中大对象摘要的最大字符长度。被 core.context 快照逻辑使用。
    "context_snapshot_big_object_summary_len": 200,
    # LLM 单次请求默认超时秒数。被 llm.base / llm.openai 客户端使用。
    "llm_request_timeout_seconds": 120.0,
    # 默认 LLM 提供商名。被 llm.provider.resolve_provider 使用,当未显式指定
    # 提供商时用此值。预设值:"deepseek" / "deepseek-flash"。
    "default_llm_provider": "deepseek",
    # 是否默认装配可观测性 hooks(LoggingHooks + TokenAccountingHooks)。
    # 被 core.workflow 使用:Workflow 构造时若未显式传入 hooks 且此项为 True,
    # 则自动装配默认 hooks,使日志与 token 计量开箱即用,无需 --verbose。
    "default_hooks_enabled": True,
}


# ---------------------------------------------------------------------------
# 运行时覆盖表
# ---------------------------------------------------------------------------
# 仅存放被 set_default 修改过的项；get_default 优先读这里。
# reset_default 会从此处删除对应 key，使其回退到 _DEFAULTS。
# 注意：本表不保证线程安全，跨线程动态修改需外部加锁（见模块 docstring）。
_OVERRIDES: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def get_default(key: str) -> Any:
    """获取某项默认值。

    优先返回运行时覆盖值（``_OVERRIDES``），否则返回内置默认值（``_DEFAULTS``）。

    Args:
        key: 配置项名称（见 ``_DEFAULTS`` 的 key 列表）。

    Returns:
        Any: 配置项的当前值。

    Raises:
        KeyError: 当 ``key`` 既不在 ``_DEFAULTS`` 也不在 ``_OVERRIDES`` 中时。
    """
    if key in _OVERRIDES:
        return _OVERRIDES[key]
    if key in _DEFAULTS:
        return _DEFAULTS[key]
    raise KeyError(f"未知的配置项: {key!r}。可用项: {sorted(_DEFAULTS.keys())}")


def set_default(key: str, value: Any) -> None:
    """运行时覆盖某项默认值。

    Args:
        key: 配置项名称。必须已存在于 ``_DEFAULTS``，否则视为拼写错误并报错，
             以避免静默写入永远读不到的 key。
        value: 新值，类型由调用方自行保证与该配置项语义一致。

    Raises:
        KeyError: 当 ``key`` 不在 ``_DEFAULTS`` 中时（防止拼写错误导致配置丢失）。
    """
    if key not in _DEFAULTS:
        raise KeyError(
            f"无法设置未知配置项: {key!r}。可用项: {sorted(_DEFAULTS.keys())}"
        )
    _OVERRIDES[key] = value


def get_config() -> dict[str, Any]:
    """返回所有默认值的只读副本。

    合并 ``_DEFAULTS`` 与 ``_OVERRIDES``，返回一个新的 dict，
    调用方修改返回值不会影响内部状态。

    Returns:
        dict[str, Any]: 所有配置项当前生效值的浅拷贝。
    """
    # 先复制内置默认值，再用覆盖值合并，保证调用方拿到的是当前生效快照
    merged: dict[str, Any] = dict(_DEFAULTS)
    merged.update(_OVERRIDES)
    return merged


def reset_default(key: str) -> None:
    """将某项重置回内置默认值。

    若 ``key`` 未被覆盖过，则为 no-op。若 ``key`` 本身不是已知配置项，则报错。

    Args:
        key: 配置项名称。

    Raises:
        KeyError: 当 ``key`` 不在 ``_DEFAULTS`` 中时。
    """
    if key not in _DEFAULTS:
        raise KeyError(
            f"无法重置未知配置项: {key!r}。可用项: {sorted(_DEFAULTS.keys())}"
        )
    # pop 而非 del，避免 key 未被覆盖时抛 KeyError；拼写错误已由上面的校验拦截
    _OVERRIDES.pop(key, None)


# ---------------------------------------------------------------------------
# RetryPolicy —— 重试策略数据类
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetryPolicy:
    """重试策略数据类。

    封装重试相关参数，供 core.agent / steps / tools 等需要重试的模块使用。
    使用 ``dataclass(frozen=True)`` 使实例不可变，便于在多处共享而不被误改。

    Attributes:
        count:        重试次数（不含首次执行）。0 表示不重试。
        backoff:      退避策略，``fixed`` 或 ``exponential``。
        base_seconds: 退避基准秒数。
    """

    count: int
    backoff: str
    base_seconds: float

    @classmethod
    def from_config(cls) -> RetryPolicy:
        """从全局默认配置构造 RetryPolicy。

        读取 ``default_retry_count`` / ``default_retry_backoff`` /
        ``default_retry_base_seconds`` 三项默认值。

        Returns:
            RetryPolicy: 基于当前生效默认值构造的实例。
        """
        return cls(
            count=cast(int, get_default("default_retry_count")),
            backoff=cast(str, get_default("default_retry_backoff")),
            base_seconds=cast(float, get_default("default_retry_base_seconds")),
        )


def default_retry_policy() -> RetryPolicy:
    """便利函数：返回基于当前默认配置构造的 RetryPolicy。

    等价于 ``RetryPolicy.from_config()``，提供更简洁的调用入口。

    Returns:
        RetryPolicy: 基于当前生效默认值构造的重试策略实例。
    """
    return RetryPolicy.from_config()


__all__ = [
    "get_default",
    "set_default",
    "get_config",
    "reset_default",
    "RetryPolicy",
    "default_retry_policy",
]
