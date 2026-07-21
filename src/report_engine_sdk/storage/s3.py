"""Amazon S3 storage backend (boto3 is loaded lazily)."""

from __future__ import annotations

from urllib.parse import quote

from .base import StorageBackend


class S3Storage(StorageBackend):
    """Persist rendered reports to Amazon S3.

    The ``boto3`` dependency is imported lazily inside :meth:`__init__` so that
    simply importing this module (or the ``storage`` package) does not require
    ``boto3`` to be installed. Only constructing an :class:`S3Storage` instance
    triggers the import.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
    ) -> None:
        """Initialize the S3 storage backend.

        Args:
            bucket: Name of the target S3 bucket.
            prefix: Optional key prefix prepended to every saved object.
                Surrounding slashes are stripped.
            region: AWS region of the bucket. If ``None``, boto3's default
                region resolution is used and the returned URI omits the
                region segment.

        Raises:
            ImportError: If ``boto3`` is not installed.
        """
        try:
            import boto3  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised only without boto3
            raise ImportError(
                "boto3 is required for S3Storage. "
                "Install it with: pip install boto3"
            ) from exc

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region
        self.client = boto3.client("s3", region_name=region)

    def save(self, filename: str, content: str) -> str:
        """Upload content to S3 and return an HTTPS URI to the object.

        Args:
            filename: Object key suffix (appended to ``prefix``).
            content: Text content to upload (UTF-8 encoded).

        Returns:
            An ``https://<bucket>.s3.<region>.amazonaws.com/<key>`` URI,
            or ``https://<bucket>.s3.amazonaws.com/<key>`` when no region is
            configured.
        """
        if self.prefix:
            key = f"{self.prefix}/{filename}"
        else:
            key = filename
        key = key.strip("/")
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content.encode("utf-8"),
        )
        quoted_key = quote(key)
        if self.region:
            return (
                f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{quoted_key}"
            )
        return f"https://{self.bucket}.s3.amazonaws.com/{quoted_key}"
