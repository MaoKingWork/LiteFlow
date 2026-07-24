"""cli serve 子命令 + pyproject 脚本测试（F2）。

出口标准（对齐 P1 §F2）：
    - `agentkit serve --help` 显示子命令
    - `agentkit serve` 缺 uvicorn 时退出码 1 + 安装提示
    - `agentkit serve` 覆盖 config 的 server_host/port/token/cors_origins
    - pyproject [project.scripts] 含 agentkit 入口
    - pyproject server extra 含 fastapi/uvicorn/sse-starlette
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from agentkit.cli import main


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------
def test_serve_help_shows_subcommand(capsys):
    """`agentkit serve --help` 显示 serve 子命令。"""
    with pytest.raises(SystemExit):
        main(["serve", "--help"])
    captured = capsys.readouterr()
    assert "serve" in captured.out
    assert "--dir" in captured.out
    assert "--host" in captured.out
    assert "--port" in captured.out
    assert "--token" in captured.out
    assert "--cors-origins" in captured.out


def test_main_help_lists_serve(capsys):
    """`agentkit --help` 列出 serve 子命令。"""
    with pytest.raises(SystemExit):
        main(["--help"])
    captured = capsys.readouterr()
    assert "serve" in captured.out


# ---------------------------------------------------------------------------
# config 覆盖
# ---------------------------------------------------------------------------
def test_serve_overrides_config(tmp_path, monkeypatch, capsys):
    """`agentkit serve` 覆盖 config 的 server_host/port/token/cors_origins。

    用 monkeypatch 拦截 uvicorn.run 与 create_app,验证 config 被正确覆盖。
    """
    from agentkit.config import get_default, reset_default

    # 记录传给 uvicorn.run 的参数
    captured = {"app": None, "host": None, "port": None}

    def _fake_uvicorn_run(app, host, port):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    def _fake_create_app(workflow_dir):
        # 返回一个 dummy app（不需要真实 FastAPI）
        return {"_dummy_app": True, "workflow_dir": workflow_dir}

    # 懒加载 patch:cli 内部 import uvicorn / server.app
    import agentkit.server.app as app_mod
    monkeypatch.setattr(app_mod, "create_app", _fake_create_app)

    # uvicorn 是独立模块,需要 patch import 系统
    # 用 sys.modules 注入一个 mock uvicorn
    import types

    mock_uvicorn = types.ModuleType("uvicorn")
    mock_uvicorn.run = _fake_uvicorn_run
    monkeypatch.setitem(sys.modules, "uvicorn", mock_uvicorn)

    try:
        rc = main([
            "serve",
            "--dir", str(tmp_path),
            "--host", "0.0.0.0",
            "--port", "9999",
            "--token", "secret123",
            "--cors-origins", "http://localhost:3000",
            "--cors-origins", "http://localhost:5173",
        ])
        assert rc == 0

        # config 被覆盖
        assert get_default("server_host") == "0.0.0.0"
        assert get_default("server_port") == 9999
        assert get_default("server_token") == "secret123"
        assert get_default("server_cors_origins") == [
            "http://localhost:3000",
            "http://localhost:5173",
        ]

        # uvicorn.run 被调用,host/port 来自 config 最终值
        assert captured["app"] is not None
        assert captured["host"] == "0.0.0.0"
        assert captured["port"] == 9999
    finally:
        reset_default("server_host")
        reset_default("server_port")
        reset_default("server_token")
        reset_default("server_cors_origins")


def test_serve_keeps_defaults_when_not_specified(tmp_path, monkeypatch):
    """未指定的参数保留 config 默认值。"""
    from agentkit.config import get_default, reset_default

    captured = {"host": None, "port": None}

    def _fake_uvicorn_run(app, host, port):
        captured["host"] = host
        captured["port"] = port

    import types

    mock_uvicorn = types.ModuleType("uvicorn")
    mock_uvicorn.run = _fake_uvicorn_run
    monkeypatch.setitem(sys.modules, "uvicorn", mock_uvicorn)

    import agentkit.server.app as app_mod
    monkeypatch.setattr(
        app_mod, "create_app", lambda wd: {"_dummy": True}
    )

    # 不指定任何 host/port/token → 用 config 默认
    original_host = get_default("server_host")
    original_port = get_default("server_port")
    try:
        rc = main(["serve", "--dir", str(tmp_path)])
        assert rc == 0
        assert captured["host"] == original_host
        assert captured["port"] == original_port
    finally:
        reset_default("server_host")
        reset_default("server_port")


# ---------------------------------------------------------------------------
# 依赖缺失
# ---------------------------------------------------------------------------
def test_serve_without_uvicorn_returns_1(tmp_path, monkeypatch, capsys):
    """未安装 uvicorn 时退出码 1 + 安装提示。"""
    # 让 import uvicorn 失败
    import sys

    # 备份真实 uvicorn
    real_uvicorn = sys.modules.get("uvicorn")
    monkeypatch.setitem(sys.modules, "uvicorn", None)

    # create_app 仍可用（不依赖 uvicorn）
    import agentkit.server.app as app_mod
    monkeypatch.setattr(
        app_mod, "create_app", lambda wd: {"_dummy": True}
    )

    rc = main(["serve", "--dir", str(tmp_path)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "uvicorn" in captured.err.lower()
    assert "agentkit[server]" in captured.err

    # 恢复
    if real_uvicorn is not None:
        sys.modules["uvicorn"] = real_uvicorn


# ---------------------------------------------------------------------------
# pyproject
# ---------------------------------------------------------------------------
def _project_root():
    """定位项目根目录（含 pyproject.toml）。

    agentkit.__file__ = <root>/src/agentkit/__init__.py
    向上 3 级:__init__.py → agentkit → src → <root>
    """
    import agentkit
    import pathlib

    return pathlib.Path(agentkit.__file__).resolve().parent.parent.parent


def test_pyproject_has_scripts_entry():
    """pyproject.toml [project.scripts] 含 agentkit 入口。"""
    import tomllib  # Python 3.11+

    pyproject_path = _project_root() / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    assert "project" in data
    assert "scripts" in data["project"]
    assert "agentkit" in data["project"]["scripts"]
    assert data["project"]["scripts"]["agentkit"] == "agentkit.cli:main"


def test_pyproject_server_extra_has_dependencies():
    """pyproject server extra 含 fastapi/uvicorn/sse-starlette。"""
    import tomllib

    pyproject_path = _project_root() / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    optional = data["project"]["optional-dependencies"]
    assert "server" in optional
    server_deps = optional["server"]
    # 转为字符串便于匹配
    server_deps_str = " ".join(server_deps)
    assert "fastapi" in server_deps_str
    assert "uvicorn" in server_deps_str
    assert "sse-starlette" in server_deps_str

    # dev extra 也应包含 server 依赖（测试用）
    assert "dev" in optional
    dev_deps_str = " ".join(optional["dev"])
    assert "uvicorn" in dev_deps_str
    assert "httpx" in dev_deps_str


# ---------------------------------------------------------------------------
# CLI 入口可用性（subprocess）
# ---------------------------------------------------------------------------
def test_cli_entry_help_works():
    """`python -m agentkit.cli --help` 可用。

    不依赖 pyproject [project.scripts] 安装,直接调 Python 模块。
    需设置 PYTHONPATH=src 让子进程能 import agentkit。
    """
    import os

    env = os.environ.copy()
    src_path = str(_project_root() / "src")
    env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "agentkit.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 0
    assert "serve" in result.stdout
