"""skill.registry —— Skill 能力包注册表。

本模块定义 AgentKit 中 Skill（能力包）的清单数据类 ``SkillManifest``
与 ``SkillRegistry`` 注册表。

设计要点：
    - ``SkillManifest`` 是 ``skill.yaml`` 解析后的内存表示，承载 Skill 的
      元信息（名称、版本、描述、提示词文件、输出契约、工具引用等）。
    - ``SkillRegistry`` 维护 ``name -> SkillManifest`` 映射，重名抛
      ``ValueError``，按名取不存在抛 ``KeyError``。
    - 本任务**仅实现注册表**；loader（从文件系统加载）与 merger（多 Skill
      合并）由后续任务实现。
    - 模块独立，仅依赖 ``pydantic`` 与 ``dataclasses``，不依赖其他
      agentkit 子模块，避免循环依赖。

公开 API：
    - SkillManifest:    Skill 清单数据类
    - SkillRegistry:    Skill 注册表
    - register_skill:   注册 Skill 到全局注册表
    - get_skill:        按名取 SkillManifest
    - list_skills:      列出所有已注册 Skill 名
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# SkillManifest —— Skill 清单数据类
# ---------------------------------------------------------------------------
@dataclass
class SkillManifest:
    """Skill 清单数据类。

    ``skill.yaml`` 解析后的内存表示。loader 负责从文件系统读取并填充
    本对象，registry 负责按名存取。

    Attributes:
        name:                   Skill 唯一名称（注册键）。
        version:                语义化版本号，默认 ``0.0.0``。
        description:            自然语言描述。
        system_file:            ``system_prompt.txt`` 相对路径（相对 ``base_dir``）。
        system_prompt:          加载后的提示词内容（loader 填充）。
        output_model:           输出契约 Pydantic Model，``None`` 表示自由文本。
        max_tool_iterations:    Function Call 最大轮次；``0`` 表示继承 Agent 默认。
        tools:                  注册后的工具名列表（指向 ToolRegistry）。
        requires_mcp:           依赖的 MCP 服务名列表。
        prompt_injection_append: 追加注入到系统提示词末尾的内容。
        base_dir:               Skill 包根目录，用于解析 ``system_file`` 等相对路径。
    """

    name: str
    version: str = "0.0.0"
    description: str = ""
    system_file: str | None = None
    system_prompt: str = ""
    output_model: type[BaseModel] | None = None
    max_tool_iterations: int = 0
    tools: list[str] = field(default_factory=list)
    requires_mcp: list[str] = field(default_factory=list)
    prompt_injection_append: str = ""
    base_dir: str = ""


# ---------------------------------------------------------------------------
# SkillRegistry —— Skill 注册表
# ---------------------------------------------------------------------------
class SkillRegistry:
    """Skill 注册表。

    维护 ``name -> SkillManifest`` 映射。``register`` 接受 ``SkillManifest``
    实例，重名抛 ``ValueError``。
    """

    def __init__(self) -> None:
        self._manifests: dict[str, SkillManifest] = {}

    def register(self, manifest: SkillManifest) -> None:
        """注册 SkillManifest。

        Args:
            manifest: Skill 清单实例。

        Raises:
            ValueError: ``manifest.name`` 已被注册。
        """
        if manifest.name in self._manifests:
            raise ValueError(f"Skill 名 {manifest.name!r} 已注册")
        self._manifests[manifest.name] = manifest

    def get(self, name: str) -> SkillManifest:
        """按名取 SkillManifest。

        Args:
            name: Skill 注册名。

        Returns:
            SkillManifest: 对应的清单实例。

        Raises:
            KeyError: 名称为空或未注册。
        """
        if name not in self._manifests:
            raise KeyError(name)
        return self._manifests[name]

    def has(self, name: str) -> bool:
        """判断 Skill 是否已注册。"""
        return name in self._manifests

    def list(self) -> list[str]:
        """返回所有已注册 Skill 名。"""
        return list(self._manifests.keys())

    def clear(self) -> None:
        """清空注册表（测试用）。"""
        self._manifests.clear()


# ---------------------------------------------------------------------------
# 全局注册表与便捷函数
# ---------------------------------------------------------------------------
_GLOBAL_SKILL_REGISTRY = SkillRegistry()


def register_skill(manifest: SkillManifest) -> None:
    """将 SkillManifest 注册到全局注册表。重名抛 ``ValueError``。"""
    _GLOBAL_SKILL_REGISTRY.register(manifest)


def get_skill(name: str) -> SkillManifest:
    """从全局注册表按名取 SkillManifest。不存在抛 ``KeyError``。"""
    return _GLOBAL_SKILL_REGISTRY.get(name)


def has_skill(name: str) -> bool:
    """判断 Skill 是否已注册到全局注册表。"""
    return _GLOBAL_SKILL_REGISTRY.has(name)


def list_skills() -> list[str]:
    """返回全局注册表中所有 Skill 名。"""
    return _GLOBAL_SKILL_REGISTRY.list()


__all__ = [
    "SkillManifest",
    "SkillRegistry",
    "register_skill",
    "get_skill",
    "has_skill",
    "list_skills",
]
