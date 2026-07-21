"""In-memory storage backend (for tests and live previews)."""

import uuid

from .base import StorageBackend


class MemoryStorage(StorageBackend):
    """In-memory storage backend that never touches disk.

    ``save()`` returns a ``memory://<uuid>`` URI. Stored content can be
    retrieved via :meth:`read`, which is useful in tests and previews.
    """

    def __init__(self) -> None:
        """Initialize an empty in-memory store."""
        self._files: dict[str, str] = {}

    def save(self, filename: str, content: str) -> str:
        """Store content in memory and return a ``memory://<uuid>`` URI.

        Args:
            filename: Logical name (ignored for keying; a UUID is used instead).
            content: Text content to persist in memory.

        Returns:
            A ``memory://<uuid>`` URI identifying the stored content.
        """
        uri = f"memory://{uuid.uuid4()}"
        self._files[uri] = content
        return uri

    def read(self, uri: str) -> str:
        """Retrieve stored content by URI.

        Args:
            uri: The ``memory://`` URI returned by :meth:`save`.

        Returns:
            The stored text content.

        Raises:
            KeyError: If the URI is not present in the store.
        """
        return self._files[uri]
