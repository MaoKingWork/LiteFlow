"""tools.file —— 文件读写工具。

提供三个 Tool,分别覆盖文件读取、写入与目录列举:

    - ``file.read``  (source)  读取单个文件文本内容
    - ``file.write`` (sink)    写入文本到文件(覆盖或追加)
    - ``file.list``  (source)  列举目录下文件(支持 glob 过滤与递归)

设计要点:
    - 零依赖:仅用标准库 ``pathlib`` / ``os`` / ``fnmatch``。
    - 路径安全:默认所有路径相对 ``FILE_ROOT`` 解析,``..`` 越权被拒绝;
      需要访问绝对路径时设 ``FILE_ALLOW_ABSOLUTE=1``(显式开启,强默认安全)。
    - 编码统一:默认 UTF-8,可通过参数覆盖。
    - 写入策略:默认覆盖,``append=True`` 追加;``mkdir=True`` 自动创建父目录。
    - 大文件保护:``file.read`` 默认限制 1MB,可通过 ``max_size`` 调整;
      超限抛 ``ValueError``,避免一次性加载到 Context 导致内存膨胀。
    - 错误边界:文件不存在 / 权限不足 / 路径越权抛 ``ValueError`` / ``OSError``,
      交由 ToolStep retry 机制处理。

配置(环境变量):

    - ``FILE_ROOT``:           根目录,默认当前工作目录。
    - ``FILE_ALLOW_ABSOLUTE``: 设为 ``1`` / ``true`` / ``yes`` 允许绝对路径。
    - ``FILE_DEFAULT_ENCODING``:默认编码,未设置则 UTF-8。
    - ``FILE_MAX_READ_BYTES``:  ``file.read`` 默认上限字节数,默认 1MB。
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from agentkit.tools.base import Tool, tool

if TYPE_CHECKING:
    from agentkit.core.context import Context


# ---------------------------------------------------------------------------
# 配置读取:集中解析环境变量,便于测试与未来扩展
# ---------------------------------------------------------------------------
_TRUTHY = {"1", "true", "yes", "on"}


def _get_env_bool(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量(大小写不敏感)。

    ``1`` / ``true`` / ``yes`` / ``on`` 视为真;其余非空值为假。
    """
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in _TRUTHY


def _resolve_root(root: str | None) -> Path:
    """解析根目录:``root`` 参数 > ``FILE_ROOT`` 环境变量 > 当前工作目录。

    返回绝对路径且已 ``resolve()``,便于后续 ``is_relative_to`` 校验。
    """
    root_path = root or os.environ.get("FILE_ROOT") or os.getcwd()
    return Path(root_path).resolve()


def _safe_resolve(path: str, root: Path, allow_absolute: bool) -> Path:
    """将用户输入路径解析为受控的绝对路径。

    解析规则:

        - 相对路径:拼接 ``root`` 后 ``resolve()``
        - 绝对路径:仅当 ``allow_absolute=True`` 时接受,否则拒绝

    安全校验:解析后路径必须位于 ``root`` 之下(或等于 ``root``),
    否则视为越权访问,抛 ``ValueError``。

    Args:
        path:           用户输入的路径(相对或绝对)。
        root:           根目录(已 resolve)。
        allow_absolute: 是否允许绝对路径。

    Returns:
        Path: 已 resolve 的绝对路径,保证在 ``root`` 之下。

    Raises:
        ValueError: 路径越权或绝对路径未授权。
    """
    p = Path(path)
    if p.is_absolute():
        if not allow_absolute:
            raise ValueError(
                f"拒绝访问绝对路径 {path!r}:未启用 FILE_ALLOW_ABSOLUTE。"
                " 请使用相对路径,或设置环境变量 FILE_ALLOW_ABSOLUTE=1。"
            )
        resolved = p.resolve()
    else:
        resolved = (root / p).resolve()

    # is_relative_to 在 Python 3.9+ 可用;3.14 项目硬依赖已满足
    if not resolved.is_relative_to(root):
        raise ValueError(
            f"路径越权:{path!r} 解析为 {resolved},不在根目录 {root} 之下。"
        )
    return resolved


def _get_encoding(encoding: str | None) -> str:
    """编码解析:``encoding`` 参数 > ``FILE_DEFAULT_ENCODING`` > UTF-8。"""
    return encoding or os.environ.get("FILE_DEFAULT_ENCODING") or "utf-8"


def _get_max_read_bytes(max_size: int | None) -> int:
    """读取上限解析:``max_size`` 参数 > ``FILE_MAX_READ_BYTES`` > 1MB。"""
    if max_size is not None and max_size > 0:
        return max_size
    env_val = os.environ.get("FILE_MAX_READ_BYTES")
    if env_val:
        try:
            return max(int(env_val), 1)
        except ValueError:
            pass
    return 1 * 1024 * 1024  # 1MB


# ---------------------------------------------------------------------------
# file.read —— 读取文本文件
# ---------------------------------------------------------------------------
class FileReadParams(BaseModel):
    """文件读取参数。"""

    path: str = Field(..., description="文件路径(相对 FILE_ROOT 或绝对路径)")
    encoding: str | None = Field(None, description="编码(默认 UTF-8)")
    max_size: int | None = Field(
        None,
        description="最大读取字节数(默认 1MB,超限报错以防内存膨胀)",
    )


@tool("file.read", role="source")
class FileReadTool(Tool):
    """读取文本文件内容。

    默认按 UTF-8 解码,返回内容字符串与字节大小。读前先校验大小,
    超过 ``max_size`` 抛 ``ValueError``,避免一次性读入 GB 级文件污染 Context。

    返回结构::

        {
            "path":     "/abs/path/to/file",
            "content":  "...",       # 文本内容
            "size":     1024,        # 字节数
            "encoding": "utf-8",
        }
    """

    description = "读取文本文件内容"

    @property
    def param_model(self) -> type[BaseModel]:
        return FileReadParams

    async def call(self, params: dict, ctx: "Context") -> dict:
        """读取文件。

        Args:
            params: ``FileReadParams`` 对应的 dict,``path`` 必填。
            ctx:    会话上下文(只读,本工具未使用)。

        Returns:
            dict: ``{"path", "content", "size", "encoding"}``。

        Raises:
            ValueError: 路径越权 / 文件过大。
            FileNotFoundError: 文件不存在。
            OSError: 其他 IO 错误(权限等)。
        """
        path_str: str = params["path"]
        root = _resolve_root(None)
        target = _safe_resolve(path_str, root, _get_env_bool("FILE_ALLOW_ABSOLUTE"))
        encoding = _get_encoding(params.get("encoding"))
        max_size = _get_max_read_bytes(params.get("max_size"))

        # 读前校验大小:stat 比 read 更便宜,先 stat 再决定是否读
        stat = target.stat()
        if stat.st_size > max_size:
            raise ValueError(
                f"文件大小 {stat.st_size} 字节超过上限 {max_size}。"
                " 可通过 max_size 参数或 FILE_MAX_READ_BYTES 环境变量提升上限。"
            )

        content = target.read_text(encoding=encoding)
        return {
            "path": str(target),
            "content": content,
            "size": stat.st_size,
            "encoding": encoding,
        }


# ---------------------------------------------------------------------------
# file.write —— 写入文本文件
# ---------------------------------------------------------------------------
class FileWriteParams(BaseModel):
    """文件写入参数。"""

    path: str = Field(..., description="文件路径(相对 FILE_ROOT 或绝对路径)")
    content: str = Field(..., description="文本内容")
    encoding: str | None = Field(None, description="编码(默认 UTF-8)")
    append: bool = Field(False, description="追加模式(默认 False 覆盖)")
    mkdir: bool = Field(
        True, description="自动创建父目录(默认 True)"
    )


@tool("file.write", role="sink")
class FileWriteTool(Tool):
    """写入文本到文件。

    默认覆盖写入;``append=True`` 时追加到文件末尾。``mkdir=True`` 自动创建
    父目录(默认开启,避免反复前置 mkdir)。

    返回结构::

        {
            "path":   "/abs/path/to/file",
            "size":   1024,    # 写入字节数
            "mode":   "write" | "append",
            "created": True | False,   # 是否新建了文件
        }
    """

    description = "写入文本到文件(覆盖或追加)"

    @property
    def param_model(self) -> type[BaseModel]:
        return FileWriteParams

    async def call(self, params: dict, ctx: "Context") -> dict:
        """写入文件。

        Args:
            params: ``FileWriteParams`` 对应的 dict,``path`` / ``content`` 必填。
            ctx:    会话上下文(只读,本工具未使用)。

        Returns:
            dict: ``{"path", "size", "mode", "created"}``。

        Raises:
            ValueError: 路径越权。
            OSError: IO 错误(权限 / 磁盘满等)。
        """
        path_str: str = params["path"]
        content: str = params["content"]
        append: bool = params.get("append", False)
        mkdir: bool = params.get("mkdir", True)

        root = _resolve_root(None)
        target = _safe_resolve(path_str, root, _get_env_bool("FILE_ALLOW_ABSOLUTE"))
        encoding = _get_encoding(params.get("encoding"))

        existed = target.exists()
        if mkdir and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)

        # 先编码得到字节,再用字节数写,保证 size 准确(避免多字节字符计数误差)
        data = content.encode(encoding)
        mode = "ab" if append else "wb"
        with target.open(mode) as f:
            f.write(data)

        return {
            "path": str(target),
            "size": len(data),
            "mode": "append" if append else "write",
            "created": not existed,
        }


# ---------------------------------------------------------------------------
# file.list —— 列举目录
# ---------------------------------------------------------------------------
class FileListParams(BaseModel):
    """目录列举参数。"""

    dir: str = Field(
        ".",
        description="目录路径(相对 FILE_ROOT 或绝对路径,默认 '.' 即根目录)",
    )
    pattern: str | None = Field(
        None,
        description="glob / fnmatch 模式(如 '*.txt' / '*.json'),默认列举全部",
    )
    recursive: bool = Field(
        False, description="是否递归子目录(默认 False)"
    )
    include_dirs: bool = Field(
        True, description="结果是否包含子目录(默认 True)"
    )


@tool("file.list", role="source")
class FileListTool(Tool):
    """列举目录下文件与子目录。

    支持按 ``pattern`` 过滤(fnmatch 语法,如 ``*.txt``)与递归列举。
    每项返回相对 ``dir`` 的路径名、绝对路径、字节数与是否目录标记。

    返回结构::

        {
            "dir":   "/abs/dir",
            "count": 2,
            "files": [
                {"name": "a.txt", "path": "/abs/dir/a.txt", "size": 100, "is_dir": False},
                {"name": "sub",   "path": "/abs/dir/sub",   "size": 0,    "is_dir": True},
            ],
        }
    """

    description = "列举目录下文件(支持 glob 过滤与递归)"

    @property
    def param_model(self) -> type[BaseModel]:
        return FileListParams

    async def call(self, params: dict, ctx: "Context") -> dict:
        """列举目录。

        Args:
            params: ``FileListParams`` 对应的 dict。
            ctx:    会话上下文(只读,本工具未使用)。

        Returns:
            dict: ``{"dir", "count", "files"}``。

        Raises:
            ValueError: 路径越权。
            FileNotFoundError: 目录不存在。
            NotADirectoryError: 路径不是目录。
        """
        dir_str: str = params.get("dir") or "."
        pattern: str | None = params.get("pattern")
        recursive: bool = params.get("recursive", False)
        include_dirs: bool = params.get("include_dirs", True)

        root = _resolve_root(None)
        target_dir = _safe_resolve(dir_str, root, _get_env_bool("FILE_ALLOW_ABSOLUTE"))

        if not target_dir.exists():
            raise FileNotFoundError(f"目录不存在: {target_dir}")
        if not target_dir.is_dir():
            raise NotADirectoryError(f"不是目录: {target_dir}")

        # 选迭代器:递归用 rglob,否则 iterdir
        iterator = target_dir.rglob("*") if recursive else target_dir.iterdir()

        files: list[dict[str, Any]] = []
        for entry in iterator:
            # 过滤:目录 / 文件类型
            is_dir = entry.is_dir()
            if is_dir and not include_dirs:
                continue
            # pattern 过滤:仅按文件名匹配
            if pattern and not fnmatch(entry.name, pattern):
                continue
            try:
                size = entry.stat().st_size if not is_dir else 0
            except OSError:
                # 软链失效等场景:不阻塞列举,记为 -1
                size = -1
            files.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "size": size,
                    "is_dir": is_dir,
                }
            )

        # 文件名稳定排序,避免不同文件系统返回顺序差异
        files.sort(key=lambda x: (not x["is_dir"], x["name"]))

        return {
            "dir": str(target_dir),
            "count": len(files),
            "files": files,
        }
