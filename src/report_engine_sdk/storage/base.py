"""Abstract storage backend for persisting rendered reports."""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """Abstract storage backend for persisting rendered reports.

    Concrete implementations isolate the file-landing concern from the core
    engine. ``ReportEngine`` depends on this abstraction (dependency injection)
    so the persistence target (local disk, S3, memory) can be swapped without
    touching core logic.
    """

    @abstractmethod
    def save(self, filename: str, content: str) -> str:
        """Save content to storage and return a URI.

        Args:
            filename: Logical filename (or key) under which to store the content.
            content: Text content to persist.

        Returns:
            A URI string identifying the stored artifact
            (e.g. ``file://...``, ``s3://...``, ``memory://...``).
        """
        ...
