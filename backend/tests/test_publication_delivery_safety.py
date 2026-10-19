from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx

from app.models import (
    FileAsset,
    SocialAccountConnection,
    SocialPlatform,
    SocialPublication,
    SocialPublicationStatus,
)
from app.publication import PublicationService
from app.social_providers import SocialProviderError, YouTubeProvider


def test_youtube_transport_error_is_delivery_unknown_and_not_retryable(
    monkeypatch,
) -> None:
    request = httpx.Request("POST", "https://www.googleapis.com/upload/youtube/v3/videos")
    monkeypatch.setattr(
        httpx,
        "post",
        Mock(side_effect=httpx.ReadTimeout("timed out", request=request)),
    )

    provider = YouTubeProvider(None, None, None)

    with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=5)):
        try:
            provider.publish("token", "video.mp4", "title", "description")
        except SocialProviderError as exc:
            assert exc.code == "delivery_unknown"
            assert exc.retryable is False
            assert "ReadTimeout" in str(exc)
        else:
            raise AssertionError("Expected an ambiguous delivery error")


def test_youtube_server_error_is_delivery_unknown_and_not_retryable(
    monkeypatch,
) -> None:
    response = httpx.Response(
        503,
        json={"error": {"errors": [{"reason": "backendError"}]}},
        request=httpx.Request(
            "POST", "https://www.googleapis.com/upload/youtube/v3/videos"
        ),
    )
    monkeypatch.setattr(httpx, "post", Mock(return_value=response))

    provider = YouTubeProvider(None, None, None)

    with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=5)):
        try:
            provider.publish("token", "video.mp4", "title", "description")
        except SocialProviderError as exc:
            assert exc.code == "delivery_unknown"
            assert exc.retryable is False
        else:
            raise AssertionError("Expected an ambiguous delivery error")


def test_youtube_multipart_related_request_and_sanitized_bad_request(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def post(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["url"] = args[0]
        captured["headers"] = kwargs["headers"]
        captured["body"] = b"".join(kwargs["content"])
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "metadata must not be persisted",
                    "errors": [{"reason": "invalidUploadRequest"}],
                }
            },
            request=httpx.Request("POST", args[0]),
        )

    monkeypatch.setattr(httpx, "post", post)
    provider = YouTubeProvider(None, None, None)

    video = MagicMock()
    video.read.side_effect = [b"video", b""]
    context = MagicMock()
    context.__enter__.return_value = video
    with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=5)), patch.object(
        Path, "open", return_value=context
    ):
        try:
            provider.publish("token", "video.mp4", "title", "description")
        except SocialProviderError as exc:
            assert exc.code == "youtube_http_400_invalidUploadRequest"
            assert str(exc) == "YouTube rejected upload (HTTP 400, reason=invalidUploadRequest)"
        else:
            raise AssertionError("Expected a bad request error")

    headers = captured["headers"]
    body = captured["body"]
    assert captured["url"] == "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=multipart&part=snippet,status"
    assert isinstance(headers, dict)
    assert headers["Content-Type"].startswith("multipart/related; boundary=")
    assert "Authorization" in headers
    assert isinstance(body, bytes)
    assert int(headers["Content-Length"]) == len(body)
    assert b"Content-Disposition: form-data" not in body
    assert body.index(b"application/json; charset=UTF-8") < body.index(b"Content-Type: video/mp4")
    assert b'"privacyStatus":"private"' in body


def test_delivery_unknown_stops_after_one_attempt() -> None:
    publication = SimpleNamespace(
        id=111,
        status=SocialPublicationStatus.PENDING,
        social_account_connection_id=10,
        file_asset_id=847,
        error_message=None,
    )
    connection = SimpleNamespace(platform=SocialPlatform.YOUTUBE)
    asset = SimpleNamespace(workflow_execution_id=1565)
    session = Mock()

    def get(model, object_id):  # type: ignore[no-untyped-def]
        if model is SocialPublication and object_id == 111:
            return publication
        if model is SocialAccountConnection and object_id == 10:
            return connection
        if model is FileAsset and object_id == 847:
            return asset
        return None

    session.get.side_effect = get
    publisher = Mock()
    events = Mock()
    service = PublicationService(session, Mock(), events, lambda _platform: publisher)

    service.execute(111)

    # V3 stores a Carousel intent only. No legacy YouTube/Reels provider can
    # consume its image list, so the worker must not change PENDING state.
    publisher.publish.assert_not_called()
    assert publication.status is SocialPublicationStatus.PENDING
    session.add.assert_not_called()
