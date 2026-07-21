"""skill.loader —— Skill 包加载器。

本模块实现 AgentKit 中 Skill（能力包）的文件系统加载器 ``SkillLoader``，
负责读取 ``skill.yaml`` 清单，动态导入 ``output_schema.py``（输出契约）
与 ``tools.py``（专属工具），自动注册专属工具到全局 ``ToolRegistry``，
校验 ``requires.mcp`` 依赖，并将解析后的 ``SkillManifest`` 注册到全局
``SkillRegistry``。

Skill 包目录结构示例::

    skills/
    └── web_search/
        ├── skill.yaml              # 清单文件
        ├── system_prompt.txt       # 系统提示词片段
        ├── output_schema.py        # Pydantic 输出模型（含目标类）
        └── tools.py                # 专属工具实现（函数或 Tool 子类）

skill.yaml 示例::

    name: web_search
    version: "1.0.0"
    description: "联网搜索与网页内容提取能力"
    agent:
      system_file: system_prompt.txt
      output_model: output_schema.py:SearchResult
      max_tool_iterations: 5
    tools:
      - module: tools.py
        functions: [search, scrape]
    requires:
      mcp: ["filesystem"]
    prompt_injection:
      append_to_system: |
        你拥有联网搜索能力。

设计要点：
    - 高度模块化：仅依赖 ``skill.registry`` + ``tools.base`` + ``pydantic``
      + 标准库（importlib / yaml / pathlib / inspect），不依赖其他 agentkit
      子模块，避免循环依赖。
    - 可拓展：``tools.py`` 中的工具可以是 ``Tool`` 子类、``@tool`` 装饰的类、
      或裸 ``async`` 函数（由内置 ``_FunctionTool`` 适配包装）。
    - 健壮：清单缺失 / 字段非法 / 导入失败 / MCP 依赖未满足均抛
      ``SkillLoadError`` 并给出清晰错误信息；``load_all`` 中单个 skill 失败
      不中断整体，记录 warning 后跳过。
    - 缓存：已加载的 skill 不重复加载（``load`` 先查 ``SkillRegistry``）。

公开 API：
    - SkillLoader:    Skill 包加载器
    - load_skill:     便捷函数，一次性加载单个 skill
    - load_skills:    便捷函数，加载目录下所有 skill
    - SkillLoadError: Skill 加载错误
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import warnings
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from agentkit.skill.registry import (
    SkillManifest,
    get_skill,
    has_skill,
    register_skill,
)
from agentkit.tools.base import Tool, get_tool, register

__all__ = [
    "SkillLoader",
    "load_skill",
    "load_skills",
    "SkillLoadError",
]


# ---------------------------------------------------------------------------
# SkillLoadError —— Skill 加载错误
# ---------------------------------------------------------------------------
class SkillLoadError(Exception):
    """Skill 加载错误。

    在以下情形抛出：清单文件缺失、必需字段缺失、字段类型非法、
    ``output_schema.py`` / ``tools.py`` 动态导入失败、输出模型类未找到或
    非法、或 ``requires.mcp`` 依赖未满足。
    """


# ---------------------------------------------------------------------------
# _FunctionTool —— 裸函数到 Tool 的适配器
# ---------------------------------------------------------------------------
class _FunctionTool(Tool):
    """将裸可调用对象（通常为 ``async`` 函数）包装为 ``Tool``。

    ``tools.py`` 中的工具可能以裸函数形式定义，而非 ``Tool`` 子类。本类
    提供适配，使裸函数也能注册到全局 ``ToolRegistry`` 并被 Function Call
    调度。

    调用签名启发式：
        裸工具函数可能采用两种签名之一：
        (a) ``fn(**params)`` —— 按关键字接收扁平参数，如
            ``async def search(query: str) -> dict``；
        (b) ``fn(params, ctx)`` —— ``Tool`` 标准签名，接收参数 dict 与上下文。
        ``call`` 先尝试 (a)；若抛 ``TypeError``（形参不匹配）则回退到 (b)。
        同时兼容 ``async`` 与同步函数：若返回值为可 await 对象则自动 await。

        注意：此启发式可能误捕获函数体内自发抛出的 ``TypeError``。约定工具
        函数内部不应在参数解析阶段抛 ``TypeError``，以避免误判回退。
    """

    def __init__(self, fn: Any, name: str, role: str = "action") -> None:
        """初始化函数工具。

        Args:
            fn:   被包装的可调用对象（通常为 ``async`` 函数）。
            name: 工具注册名。
            role: 语义角色，默认 ``action``。
        """
        self._fn = fn
        self.name = name
        self.role = role
        # 描述取自函数 docstring，供 LLM Function Call 理解用途
        self.description = (fn.__doc__ or "").strip()

    async def call(self, params: dict, ctx: Any) -> dict:
        """执行被包装的函数。

        Args:
            params: 调用参数 dict。
            ctx:    会话上下文（透传给标准签名形式的函数）。

        Returns:
            dict: 函数执行结果。
        """
        fn = self._fn
        # 启发式分派：先按关键字扁平传参 (a)，不匹配则回退到标准签名 (b)。
        try:
            result = fn(**params)
        except TypeError:
            result = fn(params, ctx)
        # 兼容 async / sync：返回值可 await 则 await。
        if inspect.isawaitable(result):
            result = await result
        return result


# ---------------------------------------------------------------------------
# SkillLoader —— Skill 包加载器
# ---------------------------------------------------------------------------
class SkillLoader:
    """Skill 包加载器。

    读取 ``skill.yaml`` 清单，动态导入 ``output_schema.py`` 与 ``tools.py``，
    自动注册专属工具到全局 ``ToolRegistry``，校验 ``requires.mcp`` 依赖，
    并将 ``SkillManifest`` 注册到全局 ``SkillRegistry``。

    Attributes:
        skills_root:   skills 目录父目录（其下每个子目录是一个 skill 包）。
        available_mcp: 当前已配置的 MCP server 名列表；``None`` 表示不校验
                       MCP 依赖（允许延迟校验）。
    """

    def __init__(
        self,
        skills_root: str | Path,
        *,
        available_mcp: list[str] | None = None,
    ) -> None:
        """初始化加载器。

        Args:
            skills_root:   skills 目录父目录。
            available_mcp: 已配置的 MCP server 名列表，用于校验 ``requires.mcp``。
                           ``None`` 表示不校验。
        """
        self.skills_root: Path = Path(skills_root)
        self.available_mcp: list[str] | None = (
            list(available_mcp) if available_mcp is not None else None
        )

    # ------------------------------------------------------------------
    # 公开加载入口
    # ------------------------------------------------------------------
    def load(self, skill_name: str) -> SkillManifest:
        """加载单个 skill。

        流程：找 ``{skills_root}/{skill_name}/skill.yaml`` → 解析清单 →
        校验 MCP 依赖 → 读取系统提示词 → 导入输出模型 → 导入并注册工具 →
        注册到 ``SkillRegistry``。已加载则直接返回缓存。

        Args:
            skill_name: skill 包目录名（应与清单中的 ``name`` 一致）。

        Returns:
            SkillManifest: 加载并注册后的清单实例。

        Raises:
            SkillLoadError: 清单缺失 / 字段非法 / 导入失败 / MCP 依赖未满足。
        """
        # 缓存命中：已加载则直接返回，避免重复导入与注册
        if has_skill(skill_name):
            return get_skill(skill_name)

        skill_dir = self.skills_root / skill_name
        yaml_path = skill_dir / "skill.yaml"
        if not yaml_path.is_file():
            raise SkillLoadError(f"找不到 Skill 清单文件: {yaml_path}")

        # 解析 YAML 清单
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise SkillLoadError(f"解析 Skill 清单 {yaml_path} 失败: {e}") from e

        manifest = self._parse_manifest(skill_dir, data)

        # 早校验 MCP 依赖：失败时直接抛错，不污染工具注册表
        self._check_mcp(manifest)

        # 读取系统提示词
        if manifest.system_file:
            self._load_system_prompt(skill_dir, manifest)

        # 导入输出模型（output_schema.py:ClassName）
        agent_cfg = data.get("agent") or {}
        model_ref = agent_cfg.get("output_model")
        if model_ref:
            self._import_output_model(skill_dir, manifest, model_ref)

        # 导入并注册专属工具
        tools_cfg = data.get("tools") or []
        if tools_cfg:
            self._import_tools(skill_dir, manifest, tools_cfg)

        # 注册到全局 SkillRegistry
        register_skill(manifest)
        return manifest

    def load_all(self) -> list[SkillManifest]:
        """加载 ``skills_root`` 下所有含 ``skill.yaml`` 的子目录。

        单个 skill 加载失败不中断整体，记录 ``warning`` 后跳过。

        Returns:
            list[SkillManifest]: 成功加载的清单列表（按子目录名排序）。
        """
        results: list[SkillManifest] = []
        if not self.skills_root.is_dir():
            return results
        for child in sorted(self.skills_root.iterdir()):
            if not child.is_dir():
                continue
            if not (child / "skill.yaml").is_file():
                continue
            try:
                results.append(self.load(child.name))
            except Exception as e:  # noqa: BLE001 —— 整体加载需容错跳过
                warnings.warn(
                    f"加载 Skill {child.name!r} 失败,已跳过: {e}",
                    stacklevel=2,
                )
        return results

    # ------------------------------------------------------------------
    # 清单解析
    # ------------------------------------------------------------------
    def _parse_manifest(self, skill_dir: Path, data: dict) -> SkillManifest:
        """解析 ``skill.yaml`` dict 为 ``SkillManifest``。

        ``output_model`` 此时仍为 ``None``，由 ``_import_output_model`` 后续
        填充；``tools`` 为空列表，由 ``_import_tools`` 填充注册后的工具名。

        Args:
            skill_dir: skill 包根目录。
            data:      ``yaml.safe_load`` 得到的顶层 dict。

        Returns:
            SkillManifest: 未填充 ``output_model`` 的清单实例。

        Raises:
            SkillLoadError: 顶层非 dict、缺少必需字段 ``name``、或字段类型非法。
        """
        if not isinstance(data, dict):
            raise SkillLoadError(
                f"skill.yaml 顶层应为映射,实际为 {type(data).__name__}"
            )
        name = data.get("name")
        if not name:
            raise SkillLoadError("skill.yaml 缺少必需字段 'name'")

        # 校验各子节为映射类型，避免后续 .get 误用
        agent_cfg = data.get("agent") or {}
        if not isinstance(agent_cfg, dict):
            raise SkillLoadError(
                f"Skill {name} 的 'agent' 字段应为映射,实际为 {type(agent_cfg).__name__}"
            )
        requires_cfg = data.get("requires") or {}
        if not isinstance(requires_cfg, dict):
            raise SkillLoadError(
                f"Skill {name} 的 'requires' 字段应为映射,实际为 {type(requires_cfg).__name__}"
            )
        injection_cfg = data.get("prompt_injection") or {}
        if not isinstance(injection_cfg, dict):
            raise SkillLoadError(
                f"Skill {name} 的 'prompt_injection' 字段应为映射,"
                f"实际为 {type(injection_cfg).__name__}"
            )

        # max_tool_iterations 必须可转为整数
        try:
            max_iter = int(agent_cfg.get("max_tool_iterations", 0))
        except (TypeError, ValueError) as e:
            raise SkillLoadError(
                f"Skill {name} 的 agent.max_tool_iterations 必须为整数: {e}"
            ) from e

        # requires.mcp 必须为列表
        mcp_raw = requires_cfg.get("mcp") or []
        if not isinstance(mcp_raw, list):
            raise SkillLoadError(
                f"Skill {name} 的 'requires.mcp' 应为列表,实际为 {type(mcp_raw).__name__}"
            )

        return SkillManifest(
            name=str(name),
            version=str(data.get("version", "0.0.0")),
            description=str(data.get("description", "")),
            system_file=agent_cfg.get("system_file"),
            system_prompt="",
            output_model=None,
            max_tool_iterations=max_iter,
            tools=[],
            requires_mcp=[str(m) for m in mcp_raw],
            prompt_injection_append=str(injection_cfg.get("append_to_system", "")),
            base_dir=str(skill_dir),
        )

    # ------------------------------------------------------------------
    # 系统提示词
    # ------------------------------------------------------------------
    def _load_system_prompt(self, skill_dir: Path, manifest: SkillManifest) -> None:
        """读取 ``system_file`` 文本到 ``manifest.system_prompt``。

        Args:
            skill_dir: skill 包根目录。
            manifest:  待填充的清单实例。

        Raises:
            SkillLoadError: 提示词文件不存在或读取失败。
        """
        assert manifest.system_file is not None  # 由调用方保证非空
        path = skill_dir / manifest.system_file
        if not path.is_file():
            raise SkillLoadError(
                f"Skill {manifest.name} 的系统提示词文件不存在: {path}"
            )
        try:
            manifest.system_prompt = path.read_text(encoding="utf-8")
        except OSError as e:
            raise SkillLoadError(
                f"读取 Skill {manifest.name} 的系统提示词 {path} 失败: {e}"
            ) from e

    # ------------------------------------------------------------------
    # 输出模型导入
    # ------------------------------------------------------------------
    def _import_output_model(
        self,
        skill_dir: Path,
        manifest: SkillManifest,
        model_ref: str,
    ) -> None:
        """导入 ``output_schema.py:ClassName``，设置 ``manifest.output_model``。

        Args:
            skill_dir: skill 包根目录。
            manifest:  待填充的清单实例。
            model_ref: 形如 ``output_schema.py:SearchResult`` 的引用。

        Raises:
            SkillLoadError: 引用格式非法 / 文件不存在 / 类未找到 / 非 BaseModel 子类。
        """
        if ":" not in model_ref:
            raise SkillLoadError(
                f"Skill {manifest.name} 的 output_model 引用格式非法"
                f"(应为 'file.py:ClassName'): {model_ref!r}"
            )
        file_part, class_name = model_ref.split(":", 1)
        file_path = skill_dir / file_part
        if not file_path.is_file():
            raise SkillLoadError(
                f"Skill {manifest.name} 的输出模型文件不存在: {file_path}"
            )
        # 用唯一模块名避免多 skill 间冲突
        unique_name = f"agentkit_skill_{manifest.name}_{Path(file_part).stem}"
        module = self._import_module_from_file(file_path, unique_name, manifest.name)

        model_cls = getattr(module, class_name, None)
        if model_cls is None:
            raise SkillLoadError(
                f"Skill {manifest.name} 的输出模型文件 {file_part} "
                f"中找不到类 {class_name!r}"
            )
        if not (isinstance(model_cls, type) and issubclass(model_cls, BaseModel)):
            raise SkillLoadError(
                f"Skill {manifest.name} 的输出模型 {class_name!r} "
                f"不是 pydantic.BaseModel 子类"
            )
        manifest.output_model = model_cls

    # ------------------------------------------------------------------
    # 工具导入与注册
    # ------------------------------------------------------------------
    def _import_tools(
        self,
        skill_dir: Path,
        manifest: SkillManifest,
        tools_cfg: list[dict],
    ) -> None:
        """导入 ``tools.py``，注册其中的工具到全局 ``ToolRegistry``。

        工具名填入 ``manifest.tools``。``tools_cfg`` 形如::

            [{'module': 'tools.py', 'functions': ['search', 'scrape']}]

        支持的工具形态：
            - ``Tool`` 子类实例：直接注册（缺名时设默认名）；
            - ``Tool`` 子类：实例化后注册（``@tool`` 装饰的类已注册则跳过重复注册）；
            - 裸可调用对象（通常 ``async`` 函数）：由 ``_FunctionTool`` 包装后注册。

        Args:
            skill_dir:  skill 包根目录。
            manifest:   待填充的清单实例。
            tools_cfg:  tools 配置列表。

        Raises:
            SkillLoadError: 配置项非法 / 模块文件不存在 / 属性未找到 / 不可注册。
        """
        for cfg in tools_cfg:
            if not isinstance(cfg, dict):
                raise SkillLoadError(
                    f"Skill {manifest.name} 的 tools 配置项应为映射,"
                    f"实际为 {type(cfg).__name__}"
                )
            module_file = cfg.get("module", "tools.py")
            functions = cfg.get("functions") or []
            if not isinstance(functions, list):
                raise SkillLoadError(
                    f"Skill {manifest.name} 的 tools.functions 应为列表,"
                    f"实际为 {type(functions).__name__}"
                )
            file_path = skill_dir / module_file
            if not file_path.is_file():
                raise SkillLoadError(
                    f"Skill {manifest.name} 的工具模块文件不存在: {file_path}"
                )
            unique_name = f"agentkit_skill_{manifest.name}_{Path(module_file).stem}"
            module = self._import_module_from_file(
                file_path, unique_name, manifest.name
            )

            for fn_name in functions:
                attr = getattr(module, fn_name, None)
                if attr is None:
                    raise SkillLoadError(
                        f"Skill {manifest.name} 的工具模块 {module_file} "
                        f"中找不到属性 {fn_name!r}"
                    )
                default_name = f"{manifest.name}.{fn_name}"
                tool_instance = self._coerce_to_tool(
                    attr, default_name, manifest.name, fn_name
                )
                actual_name = self._register_tool(tool_instance, default_name)
                if actual_name not in manifest.tools:
                    manifest.tools.append(actual_name)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _import_module_from_file(
        self,
        file_path: Path,
        unique_name: str,
        skill_name: str,
    ) -> Any:
        """用 ``importlib`` 动态导入指定文件为唯一名的模块。

        Args:
            file_path:   目标 .py 文件绝对路径。
            unique_name: 注册到 ``sys.modules`` 的唯一模块名（避免冲突）。
            skill_name:  所属 skill 名（用于错误信息）。

        Returns:
            已执行的模块对象。

        Raises:
            SkillLoadError: spec 创建失败或模块执行抛异常。
        """
        try:
            spec = importlib.util.spec_from_file_location(unique_name, file_path)
        except Exception as e:  # noqa: BLE001
            raise SkillLoadError(
                f"Skill {skill_name} 加载模块 {file_path} 失败(spec 阶段): {e}"
            ) from e
        if spec is None or spec.loader is None:
            raise SkillLoadError(
                f"Skill {skill_name} 无法为 {file_path} 创建模块 spec"
            )
        module = importlib.util.module_from_spec(spec)
        # 先注入 sys.modules 再 exec，避免模块自引用时找不到自身
        sys.modules[unique_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:  # noqa: BLE001
            # 执行失败：清理 sys.modules 残留半成品
            sys.modules.pop(unique_name, None)
            raise SkillLoadError(
                f"Skill {skill_name} 执行模块 {file_path} 失败: {e}"
            ) from e
        return module

    def _check_mcp(self, manifest: SkillManifest) -> None:
        """校验 ``requires.mcp`` 依赖是否在 ``available_mcp`` 中。

        ``available_mcp`` 为 ``None`` 时跳过校验（允许延迟校验）。

        Args:
            manifest: 待校验的清单实例。

        Raises:
            SkillLoadError: 存在未配置的 MCP 依赖。
        """
        if self.available_mcp is None:
            return
        missing = [m for m in manifest.requires_mcp if m not in self.available_mcp]
        if missing:
            raise SkillLoadError(
                f"Skill {manifest.name} 依赖 MCP {missing},但未配置"
            )

    def _coerce_to_tool(
        self,
        attr: Any,
        default_name: str,
        skill_name: str,
        fn_name: str,
    ) -> Tool:
        """将 ``tools.py`` 中的属性规整为 ``Tool`` 实例。

        命名优先级：属性已是 ``Tool`` 实例/子类且带非空 ``name``（如 ``@tool``
        装饰）则沿用其 ``name``；否则使用 ``default_name``（``{skill}.{fn}``）。

        Args:
            attr:         从模块取出的属性。
            default_name: 缺省工具名 ``{skill_name}.{fn_name}``。
            skill_name:   所属 skill 名（错误信息用）。
            fn_name:      函数/属性名（错误信息用）。

        Returns:
            Tool: 可注册的工具实例。

        Raises:
            SkillLoadError: 属性非可注册形态。
        """
        # 1) 已是 Tool 实例（手动实例化或 @tool 装饰产物）
        if isinstance(attr, Tool):
            if not attr.name:
                attr.name = default_name
            return attr
        # 2) Tool 子类：实例化后注册（@tool 装饰的类此时 name 已设置）
        if inspect.isclass(attr) and issubclass(attr, Tool):
            instance = attr()
            if not instance.name:
                instance.name = default_name
            return instance
        # 3) 裸可调用对象（通常为 async 函数）：用 _FunctionTool 包装
        if callable(attr):
            return _FunctionTool(attr, default_name)
        raise SkillLoadError(
            f"Skill {skill_name} 的属性 {fn_name!r} 不是可注册的工具"
            f"(非 Tool 实例/子类/可调用对象)"
        )

    def _register_tool(self, tool_instance: Tool, default_name: str) -> str:
        """注册工具到全局 ``ToolRegistry``，返回工具名。

        若名称已被注册（如 ``@tool`` 装饰器在导入时已注册），校验注册表中
        存在同名工具后仅返回名称，不重复注册。

        Args:
            tool_instance: 待注册的工具实例。
            default_name:  缺省工具名（当实例无 name 时使用）。

        Returns:
            str: 工具注册名。

        Raises:
            SkillLoadError: 注册冲突且注册表中不存在同名工具。
        """
        if not tool_instance.name:
            tool_instance.name = default_name
        name = tool_instance.name
        try:
            register(tool_instance)
        except ValueError:
            # 名称已注册：可能 tools.py 中 @tool 装饰器在导入时已注册。
            # 校验全局注册表中确实存在该名称，确认后仅返回名称不重复注册。
            try:
                get_tool(name)
            except KeyError as e:
                raise SkillLoadError(
                    f"工具 {name!r} 注册冲突且注册表中不存在: {e}"
                ) from e
        return name


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------
def load_skill(
    skills_root: str | Path,
    skill_name: str,
    *,
    available_mcp: list[str] | None = None,
) -> SkillManifest:
    """便捷函数：一次性加载单个 skill。

    Args:
        skills_root:   skills 目录父目录。
        skill_name:    skill 包目录名。
        available_mcp: 已配置的 MCP server 名列表；``None`` 不校验。

    Returns:
        SkillManifest: 加载并注册后的清单实例。
    """
    return SkillLoader(skills_root, available_mcp=available_mcp).load(skill_name)


def load_skills(
    skills_root: str | Path,
    *,
    available_mcp: list[str] | None = None,
) -> list[SkillManifest]:
    """便捷函数：加载目录下所有 skill。

    Args:
        skills_root:   skills 目录父目录。
        available_mcp: 已配置的 MCP server 名列表；``None`` 不校验。

    Returns:
        list[SkillManifest]: 成功加载的清单列表。
    """
    return SkillLoader(skills_root, available_mcp=available_mcp).load_all()
