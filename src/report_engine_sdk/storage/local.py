"""Local filesystem storage backend."""

import os
from pathlib import Path
from urllib.parse import quote

from .base import StorageBackend


class LocalStorage(StorageBackend):
    """Persist rendered reports to the local filesystem.

    Each :meth:`save` call writes a file under ``output_dir`` and returns a
    ``file://`` URI pointing at the resulting absolute path.
    """

    def __init__(self, output_dir: str) -> None:
        """Initialize the local storage backend.

        Args:
            output_dir: Directory where rendered files will be written.
                Created (along with parents) if it does not already exist.
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save(self, filename: str, content: str) -> str:
        """Write content to ``<output_dir>/<filename>`` and return a ``file://`` URI.

        Args:
            filename: Name of the file to write (relative to ``output_dir``).
            content: Text content to persist (UTF-8).

        Returns:
            A ``file://`` URI pointing at the absolute path of the written file.
        """
        target = self.output_dir / filename
        target.write_text(content, encoding="utf-8")
        abs_path = os.path.abspath(str(target))
        # Normalize OS-native separators to forward slashes for a valid URI.
        uri_path = Path(abs_path).as_posix()
        if not uri_path.startswith("/"):
            # Windows drive letter (e.g. "C:/...") needs a leading slash.
            uri_path = "/" + uri_path
        return f"file://{quote(uri_path)}"
