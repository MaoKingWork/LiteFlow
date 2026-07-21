"""Storage backend abstractions for persisting rendered reports.

Public API:
    * :class:`StorageBackend` -- abstract base class.
    * :class:`LocalStorage` -- writes to the local filesystem (``file://`` URIs).
    * :class:`MemoryStorage` -- in-memory store for tests/previews (``memory://`` URIs).
    * :class:`S3Storage` -- Amazon S3 store (``https://`` URIs; boto3 lazily imported).
"""

from .base import StorageBackend
from .local import LocalStorage
from .memory import MemoryStorage
from .s3 import S3Storage

__all__ = ["StorageBackend", "LocalStorage", "MemoryStorage", "S3Storage"]
