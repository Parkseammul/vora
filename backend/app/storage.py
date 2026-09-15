"""Private video storage adapters; database keys never expose local filesystem paths."""

from pathlib import Path
from typing import Protocol


class StorageProvider(Protocol):
    def local_path(self, storage_key: str) -> Path | None: ...

    def upload_private(self, storage_key: str, source: Path, content_type: str) -> None: ...

    def presigned_get_url(self, storage_key: str, expires_seconds: int = 3600) -> str: ...


class LocalStorageProvider:
    """Keeps existing local development and E2E files where video generation writes them."""

    def __init__(self, uploads_root: Path) -> None:
        self._uploads_root = uploads_root.resolve()

    def local_path(self, storage_key: str) -> Path | None:
        candidate = (self._uploads_root / storage_key).resolve()
        try:
            candidate.relative_to(self._uploads_root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def upload_private(self, storage_key: str, source: Path, content_type: str) -> None:
        # Local generation already owns the canonical file; no duplicate copy is required.
        if self.local_path(storage_key) is None and not source.is_file():
            raise FileNotFoundError("Video file was not found")

    def presigned_get_url(self, storage_key: str, expires_seconds: int = 3600) -> str:
        raise RuntimeError("Instagram publishing requires S3 private storage")


class S3StorageProvider:
    """Stores final videos privately and grants providers a time-limited read URL."""

    def __init__(self, bucket: str, region: str | None = None) -> None:
        if not bucket.strip():
            raise ValueError("S3_BUCKET is required when STORAGE_PROVIDER=s3")
        import boto3  # type: ignore[import-untyped]

        self._bucket = bucket
        self._client = boto3.client("s3", region_name=region)

    def local_path(self, storage_key: str) -> Path | None:
        return None

    def upload_private(self, storage_key: str, source: Path, content_type: str) -> None:
        if not source.is_file():
            raise FileNotFoundError("Video file was not found")
        self._client.upload_file(
            str(source), self._bucket, storage_key, ExtraArgs={"ContentType": content_type}
        )

    def presigned_get_url(self, storage_key: str, expires_seconds: int = 3600) -> str:
        return str(
            self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": storage_key},
                ExpiresIn=expires_seconds,
            )
        )
