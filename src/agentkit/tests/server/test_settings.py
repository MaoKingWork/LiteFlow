"""server.settings —— Server 配置快照测试（C1 + C2）。

出口标准（对齐 P1 §C1 + §C2）：
    - get_default("server_*") 全部可读,返回默认值
    - set_default / reset_default 对 server_* 生效
    - get_default("server_unknown") 抛 KeyError
    - ServerSettings.from_config() 返回所有默认值
    - set_default 覆盖后 from_config() 反映新值
    - AGENTKIT_SERVER_TOKEN 环境变量可读取 token
"""
from __future__ import annotations

import pytest

from agentkit.config import get_default, reset_default, set_default
from agentkit.server.settings import ServerSettings


# ---------------------------------------------------------------------------
# C1: config server_* 配置项
# ---------------------------------------------------------------------------
def test_get_default_server_keys():
    """get_default('server_*') 全部可读,返回默认值。"""
    assert get_default("server_host") == "127.0.0.1"
    assert get_default("server_port") == 8000
    assert get_default("server_token") == ""
    assert get_default("server_cors_origins") == []
    assert get_default("server_event_queue_size") == 1000
    assert get_default("server_event_log_max_events") == 100000
    assert get_default("server_artifact_max_size") == 100 * 1024 * 1024
    assert get_default("server_artifact_max_total") == 1024 * 1024 * 1024
    assert get_default("server_gc_interval_seconds") == 6 * 3600
    assert get_default("server_gc_orphan_grace_seconds") == 24 * 3600


def test_set_default_server_keys():
    """set_default 覆盖后 get_default 返回新值。"""
    original = get_default("server_port")
    try:
        set_default("server_port", 9000)
        assert get_default("server_port") == 9000
        set_default("server_host", "0.0.0.0")
        assert get_default("server_host") == "0.0.0.0"
        set_default("server_token", "abc123")
        assert get_default("server_token") == "abc123"
        set_default("server_cors_origins", ["http://localhost:3000"])
        assert get_default("server_cors_origins") == ["http://localhost:3000"]
    finally:
        reset_default("server_port")
        reset_default("server_host")
        reset_default("server_token")
        reset_default("server_cors_origins")
    assert get_default("server_port") == original


def test_reset_default_server_keys():
    """reset_default 后回退到内置默认值。"""
    set_default("server_port", 9999)
    reset_default("server_port")
    assert get_default("server_port") == 8000


def test_unknown_key_raises():
    """get_default('server_unknown') 抛 KeyError。"""
    with pytest.raises(KeyError):
        get_default("server_unknown")


def test_set_unknown_key_raises():
    """set_default 未知 key 抛 KeyError(防止拼写错误)。"""
    with pytest.raises(KeyError):
        set_default("server_unknown_key", "value")


# ---------------------------------------------------------------------------
# C2: ServerSettings.from_config
# ---------------------------------------------------------------------------
def test_from_config_defaults():
    """ServerSettings.from_config() 返回所有默认值。"""
    settings = ServerSettings.from_config()
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.token == ""
    assert settings.cors_origins == []
    assert settings.event_queue_size == 1000
    assert settings.event_log_max_events == 100000
    assert settings.artifact_max_size == 100 * 1024 * 1024
    assert settings.artifact_max_total == 1024 * 1024 * 1024
    assert settings.gc_interval_seconds == 6 * 3600.0
    assert settings.gc_orphan_grace_seconds == 24 * 3600.0


def test_from_config_overrides():
    """set_default 覆盖后 from_config() 反映新值。"""
    try:
        set_default("server_host", "0.0.0.0")
        set_default("server_port", 9000)
        set_default("server_token", "config_token")
        set_default("server_cors_origins", ["https://example.com"])
        set_default("server_event_queue_size", 2000)

        settings = ServerSettings.from_config()
        assert settings.host == "0.0.0.0"
        assert settings.port == 9000
        assert settings.token == "config_token"
        assert settings.cors_origins == ["https://example.com"]
        assert settings.event_queue_size == 2000
    finally:
        reset_default("server_host")
        reset_default("server_port")
        reset_default("server_token")
        reset_default("server_cors_origins")
        reset_default("server_event_queue_size")


def test_settings_is_frozen():
    """ServerSettings 不可变(frozen=True)。"""
    settings = ServerSettings(
        host="127.0.0.1", port=8000, token="", cors_origins=[]
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        settings.host = "0.0.0.0"  # type: ignore[misc]
    with pytest.raises(Exception):
        settings.port = 9000  # type: ignore[misc]


def test_token_from_env(monkeypatch):
    """AGENTKIT_SERVER_TOKEN 环境变量可读取 token(config token 为空时)。"""
    # 清空 config 的 server_token,确保 env 生效
    monkeypatch.setenv("AGENTKIT_SERVER_TOKEN", "env_secret_token")
    try:
        reset_default("server_token")
        # config server_token 为空,回落 env
        settings = ServerSettings.from_config()
        assert settings.token == "env_secret_token"
    finally:
        reset_default("server_token")


def test_token_config_overrides_env(monkeypatch):
    """config server_token 非空时优先于 env。"""
    monkeypatch.setenv("AGENTKIT_SERVER_TOKEN", "env_token")
    try:
        set_default("server_token", "config_token")
        settings = ServerSettings.from_config()
        assert settings.token == "config_token"
    finally:
        reset_default("server_token")


def test_token_no_config_no_env(monkeypatch):
    """config 和 env 都无 token 时为空字符串。"""
    monkeypatch.delenv("AGENTKIT_SERVER_TOKEN", raising=False)
    try:
        reset_default("server_token")
        settings = ServerSettings.from_config()
        assert settings.token == ""
    finally:
        reset_default("server_token")


def test_from_config_module_level_function():
    """模块级 from_config() 函数等价于 ServerSettings.from_config()。"""
    from agentkit.server.settings import from_config

    s1 = ServerSettings.from_config()
    s2 = from_config()
    assert s1 == s2
