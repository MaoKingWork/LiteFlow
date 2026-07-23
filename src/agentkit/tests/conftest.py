"""测试共享 fixtures。

提供零网络零 token 的测试基础设施:
    - 全局注册表隔离(autouse):每个 test 结束后恢复 Tool/Agent/Skill/LLM 注册表快照
    - MockClient:预设响应的 LLM 客户端
    - 临时检查点存储:基于 tmp_path 的 LocalCheckpointStore
    - 测试工具:echo / fail 供 ToolStep / Function Call 使用
    - 测试 Step:_Setter / _Callback / _Blocking / _Fail 供 workflow / cancel 测试使用
    - RecordingHooks:记录生命周期事件顺序,供断言 hook 序列
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from agentkit.core.checkpoint import LocalCheckpointStore
from agentkit.core.context import Context
from agentkit.core.hooks import ErrorAction, LifecycleHooks
from agentkit.llm.mock import MockClient
from agentkit.steps.base import BaseStep
from agentkit.tools.base import Tool, register as register_tool


# ---------------------------------------------------------------------------
# 全局注册表隔离 (autouse)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_global_registries():
    """每个测试前后快照/恢复全局注册表,保证测试间互不污染。"""
    import agentkit.tools.base as _tb
    import agentkit.core.agent as _ag
    import agentkit.skill.registry as _sr
    import agentkit.llm as _llm

    snap_tools = dict(_tb._GLOBAL_REGISTRY._tools)
    snap_agents = dict(_ag._GLOBAL_AGENT_REGISTRY._classes)
    snap_skills = dict(_sr._GLOBAL_SKILL_REGISTRY._manifests)
    snap_default_client = _llm._DEFAULT_CLIENT
    yield
    _tb._GLOBAL_REGISTRY._tools = snap_tools
    _ag._GLOBAL_AGENT_REGISTRY._classes = snap_agents
    _sr._GLOBAL_SKILL_REGISTRY._manifests = snap_skills
    _llm._DEFAULT_CLIENT = snap_default_client


# ---------------------------------------------------------------------------
# MockClient
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_client() -> MockClient:
    """空 MockClient;测试中用 add_response 或 script 追加响应。"""
    return MockClient(responses=[])


# ---------------------------------------------------------------------------
# 检查点存储
# ---------------------------------------------------------------------------
@pytest.fixture
def checkpoint_store(tmp_path) -> LocalCheckpointStore:
    """基于 tmp_path 的 LocalCheckpointStore,测试结束自动清理。"""
    return LocalCheckpointStore(base_dir=str(tmp_path / "checkpoints"))


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------
class EchoTool(Tool):
    """原样返回 params 的测试工具。"""

    name = "test.echo"
    description = "echo back params"
    role = "action"

    async def call(self, params: dict, ctx: Context) -> dict:
        return {"echo": params}


class FailTool(Tool):
    """总是抛 RuntimeError 的测试工具。"""

    name = "test.fail"
    description = "always fails"
    role = "action"

    async def call(self, params: dict, ctx: Context) -> dict:
        raise RuntimeError("FailTool 故意失败")


@pytest.fixture
def echo_tool() -> EchoTool:
    """注册 test.echo 工具并返回实例。"""
    t = EchoTool()
    register_tool(t)
    return t


@pytest.fixture
def fail_tool() -> FailTool:
    """注册 test.fail 工具。"""
    t = FailTool()
    register_tool(t)
    return t


# ---------------------------------------------------------------------------
# 测试 Step(不经注册表,直接 Python API 构造)
# ---------------------------------------------------------------------------
class SetterStep(BaseStep):
    """把固定值写入 ctx 的简单 Step。"""

    type = "_setter"

    def __init__(self, id: str = "", key: str = "", value: Any = None) -> None:
        super().__init__(id=id, output=key)
        self._key = key
        self._value = value

    async def run(self, ctx: Context) -> Context:
        ctx.set(self._key, self._value)
        return ctx


class CallbackStep(BaseStep):
    """执行回调后写入 output 的 Step(用于 mid-run 触发 cancel_token)。"""

    type = "_callback"

    def __init__(self, id: str = "", callback: Callable[[], None] | None = None,
                 output: str | None = None) -> None:
        super().__init__(id=id, output=output)
        self._callback = callback

    async def run(self, ctx: Context) -> Context:
        if self._callback is not None:
            self._callback()
        if self.output:
            ctx.set(self.output, "done")
        return ctx


class BlockingStep(BaseStep):
    """长时间阻塞的 Step(用于 immediate cancel 测试)。"""

    type = "_blocking"

    def __init__(self, id: str = "", delay: float = 30.0,
                 output: str | None = None) -> None:
        super().__init__(id=id, output=output, timeout=delay + 10)
        self._delay = delay

    async def run(self, ctx: Context) -> Context:
        await asyncio.sleep(self._delay)
        if self.output:
            ctx.set(self.output, "done")
        return ctx


class FailStep(BaseStep):
    """总是抛 RuntimeError 的 Step。"""

    type = "_fail"

    def __init__(self, id: str = "", output: str | None = None) -> None:
        super().__init__(id=id, output=output)

    async def run(self, ctx: Context) -> Context:
        raise RuntimeError(f"FailStep {self.id!r} 故意失败")


# ---------------------------------------------------------------------------
# RecordingHooks —— 记录生命周期事件顺序
# ---------------------------------------------------------------------------
class RecordingHooks(LifecycleHooks):
    """记录所有生命周期事件,供断言 hook 序列一一对应。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def before_workflow(self, wf, ctx) -> None:
        self.events.append("before_workflow")

    async def after_workflow(self, wf, ctx, result) -> None:
        self.events.append("after_workflow")

    async def before_step(self, step, ctx) -> None:
        self.events.append(f"before_step:{step.id or step.type}")

    async def after_step(self, step, ctx, trace) -> None:
        self.events.append(f"after_step:{step.id or step.type}")

    async def on_step_error(self, step, ctx, error) -> ErrorAction:
        self.events.append(f"on_step_error:{step.id or step.type}")
        return ErrorAction.RAISE

    async def on_tool_call(self, tool, params, result) -> None:
        self.events.append(f"on_tool_call:{getattr(tool, 'name', tool)}")

    async def on_llm_call(self, agent, prompt, response, usage) -> None:
        self.events.append("on_llm_call")


@pytest.fixture
def recording_hooks() -> RecordingHooks:
    return RecordingHooks()
