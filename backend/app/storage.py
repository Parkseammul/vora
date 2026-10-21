"""Private video storage adapters; database keys never expose local filesystem paths."""

import shutil
from pathlib import Path
from typing import Protocol


class StorageProvider(Protocol):
    def local_path(self, storage_key: str) -> Path | None: ...

    def upload_private(self, storage_key: str, source: Path, content_type: str) -> None: ...

    def materialize_private(self, storage_key: str, destination: Path) -> None: ...

    def presigned_get_url(self, storage_key: str, expires_seconds: int = 3600) -> str: ...

    def exists(self, storage_key: str) -> bool: ...


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
        if not source.is_file():
            raise FileNotFoundError("Video file was not found")
        destination = (self._uploads_root / storage_key).resolve()
        try:
            destination.relative_to(self._uploads_root)
        except ValueError as exc:
            raise ValueError("Storage key escapes uploads root") from exc
        if destination != source.resolve():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    def materialize_private(self, storage_key: str, destination: Path) -> None:
        source = self.local_path(storage_key)
        if source is None:
            raise FileNotFoundError("Private storage file was not found")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def presigned_get_url(self, storage_key: str, expires_seconds: int = 3600) -> str:
        raise RuntimeError("Instagram publishing requires S3 private storage")

    def exists(self, storage_key: str) -> bool:
        return self.local_path(storage_key) is not None


class S3StorageProvider:
    """Stores final videos privately and grants providers a time-limited read URL."""

    def __init__(self, bucket: str, region: str | None = None) -> None:
        if not bucket.strip():
            raise ValueError("S3_BUCKET is required when STORAGE_PROVIDER=s3")
        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            region_name=region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
            endpoint_url=f"https://s3.{region}.amazonaws.com" if region else None,
        )

    def local_path(self, storage_key: str) -> Path | None:
        return None

    def upload_private(self, storage_key: str, source: Path, content_type: str) -> None:
        if not source.is_file():
            raise FileNotFoundError("Video file was not found")
        self._client.upload_file(
            str(source), self._bucket, storage_key, ExtraArgs={"ContentType": content_type}
        )

    def materialize_private(self, storage_key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self._bucket, storage_key, str(destination))

    def presigned_get_url(self, storage_key: str, expires_seconds: int = 3600) -> str:
        return str(
            self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": storage_key},
                ExpiresIn=expires_seconds,
            )
        )

    def exists(self, storage_key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=storage_key)
        except self._client.exceptions.ClientError as exc:  # type: ignore[union-attr]
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True
