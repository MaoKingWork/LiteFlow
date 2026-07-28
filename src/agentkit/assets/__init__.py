"""agentkit.assets —— 内嵌字体与静态资源。

字体文件存放于 ``assets/fonts/`` 目录, 供:

    - PDF 转换器 (reportlab ``TTFont`` 注册, 真正内嵌进 PDF)
    - DOCX 转换器 (python-docx 按字体名引用, CJK + Latin 双设)
    - Web 前端 (``/fonts/`` 静态路由, ``@font-face`` 加载)

新增字体时只需将 ``.ttf`` 放入 ``fonts/`` 目录, 并在 ``FONT_FILES``
字典中注册逻辑名 → 文件名映射, 即可被各转换器与前端统一引用。
"""

from __future__ import annotations

from pathlib import Path

#: 资源根目录 (本 ``__init__.py`` 所在目录)
ASSETS_DIR: Path = Path(__file__).resolve().parent

#: 字体文件目录
FONTS_DIR: Path = ASSETS_DIR / "fonts"

#: 逻辑名 → 字体文件名映射。新增字体在此注册即可。
#: 逻辑名同时作为 reportlab / python-docx / CSS 中的 font-family 标识。
FONT_FILES: dict[str, str] = {
    "OPPO Sans": "OPPO Sans 4.0.ttf",
}

#: 默认正文字体 (优先级最高的内嵌字体)
DEFAULT_FONT: str = "OPPO Sans"

#: 代码块字体 (后续可替换为专用等宽字体, 如 JetBrains Mono)
CODE_FONT: str = "Courier New"


def get_font_path(logical_name: str) -> Path:
    """按逻辑名返回字体文件的绝对路径。

    Args:
        logical_name: ``FONT_FILES`` 中注册的逻辑名 (如 ``"OPPO Sans"``)。

    Returns:
        字体 ``.ttf`` 文件的绝对 ``Path``。

    Raises:
        KeyError: 逻辑名未在 ``FONT_FILES`` 中注册。
        FileNotFoundError: 字体文件不存在于 ``FONTS_DIR``。
    """
    filename = FONT_FILES[logical_name]
    path = FONTS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"字体文件不存在: {path} (逻辑名 {logical_name!r})"
        )
    return path
