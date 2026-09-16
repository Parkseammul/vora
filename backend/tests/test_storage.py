import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.config import Config

from app.storage import LocalStorageProvider, S3StorageProvider


def test_s3_storage_uploads_without_public_acl_and_generates_presigned_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = Mock()
    client.generate_presigned_url.return_value = "https://example.test/presigned"
    boto3_client = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=boto3_client))
    source = tmp_path / "final.mp4"
    source.write_bytes(b"video")

    storage = S3StorageProvider("private-bucket", "ap-northeast-2")
    storage.upload_private("final/asset.mp4", source, "video/mp4")
    url = storage.presigned_get_url("final/asset.mp4", 900)

    client.upload_file.assert_called_once_with(
        str(source),
        "private-bucket",
        "final/asset.mp4",
        ExtraArgs={"ContentType": "video/mp4"},
    )
    client.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "private-bucket", "Key": "final/asset.mp4"},
        ExpiresIn=900,
    )
    assert url == "https://example.test/presigned"
    boto3_client.assert_called_once()
    _, client_kwargs = boto3_client.call_args
    assert client_kwargs["region_name"] == "ap-northeast-2"
    assert client_kwargs["endpoint_url"] == "https://s3.ap-northeast-2.amazonaws.com"
    assert isinstance(client_kwargs["config"], Config)


def test_local_storage_behavior_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "final.mp4"
    source.write_bytes(b"video")
    storage = LocalStorageProvider(tmp_path)

    storage.upload_private("final.mp4", source, "video/mp4")

    assert storage.local_path("final.mp4") == source
    assert storage.local_path("../outside.mp4") is None
