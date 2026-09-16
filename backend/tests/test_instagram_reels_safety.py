from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app.models import (
    FileAsset,
    SocialAccountConnection,
    SocialPlatform,
    SocialPublication,
    SocialPublicationAttemptStatus,
    SocialPublicationStatus,
)
from app.publication import PublicationService
from app.social_providers import InstagramProvider, PublishedPost, SocialProviderError


def _response(status: int, payload: dict[str, object], method: str, url: str) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request(method, url))


def _provider() -> InstagramProvider:
    return InstagramProvider(None, None, None, "v24.0", 0, 3)


def test_reel_waits_for_finished_persists_ids_and_verifies_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts: list[tuple[str, dict[str, object], dict[str, str]]] = []
    gets = iter(
        [
            _response(200, {"status_code": "IN_PROGRESS"}, "GET", "https://meta.test/container"),
            _response(200, {"status_code": "FINISHED"}, "GET", "https://meta.test/container"),
            _response(
                200,
                {"id": "media-1", "media_type": "VIDEO", "permalink": "https://instagram.com/reel/1"},
                "GET",
                "https://meta.test/media",
            ),
        ]
    )

    def post(url: str, **kwargs: object) -> httpx.Response:
        posts.append((url, kwargs["data"], kwargs["headers"]))  # type: ignore[arg-type]
        payload = {"id": "container-1"} if url.endswith("/media") else {"id": "media-1"}
        return _response(200, payload, "POST", url)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: next(gets))
    metadata: dict[str, object] = {}
    commits = Mock()

    result = _provider().publish("secret-token", "account-1", "https://s3.example/video", "caption", metadata, commits)

    assert result == PublishedPost("media-1", "https://instagram.com/reel/1")
    assert [item[0] for item in posts] == [
        "https://graph.instagram.com/v24.0/account-1/media",
        "https://graph.instagram.com/v24.0/account-1/media_publish",
    ]
    assert posts[0][1] == {"media_type": "REELS", "video_url": "https://s3.example/video", "caption": "caption"}
    assert posts[1][1] == {"creation_id": "container-1"}
    assert all(item[2] == {"Authorization": "Bearer secret-token"} for item in posts)
    assert metadata["instagram_container_id"] == "container-1"
    assert metadata["instagram_container_status"] == "FINISHED"
    assert metadata["instagram_media_id"] == "media-1"
    assert metadata["instagram_reconciliation_required"] is False
    assert commits.call_count >= 5


@pytest.mark.parametrize("status", ["ERROR", "EXPIRED"])
def test_failed_container_never_sends_media_publish(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    post = Mock(return_value=_response(200, {"id": "container-1"}, "POST", "https://meta.test/media"))
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", Mock(return_value=_response(200, {"status_code": status}, "GET", "https://meta.test/container")))

    with pytest.raises(SocialProviderError, match=status) as error:
        _provider().publish("token", "account", "https://s3.example/video", "caption", {}, Mock())

    assert error.value.code == "instagram_container_failed"
    assert post.call_count == 1


def test_existing_container_resumes_without_creating_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = Mock(return_value=_response(200, {"id": "media-1"}, "POST", "https://meta.test/publish"))
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(
        httpx,
        "get",
        Mock(
            side_effect=[
                _response(200, {"status_code": "FINISHED"}, "GET", "https://meta.test/container"),
                _response(200, {"id": "media-1", "media_type": "REELS", "permalink": "https://instagram.com/reel/1"}, "GET", "https://meta.test/media"),
            ]
        ),
    )
    metadata: dict[str, object] = {"instagram_container_id": "container-1"}

    _provider().publish("token", "account", "https://s3.example/video", "caption", metadata, Mock())

    assert post.call_count == 1
    assert post.call_args.args[0].endswith("/media_publish")


def test_published_container_with_saved_media_id_only_reconciles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = Mock()
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(
        httpx,
        "get",
        Mock(
            side_effect=[
                _response(200, {"status_code": "PUBLISHED"}, "GET", "https://meta.test/container"),
                _response(200, {"id": "media-1", "media_type": "REELS", "permalink": "https://instagram.com/reel/1"}, "GET", "https://meta.test/media"),
            ]
        ),
    )

    result = _provider().publish(
        "token",
        "account",
        "https://s3.example/video",
        "caption",
        {"instagram_container_id": "container-1", "instagram_media_id": "media-1"},
        Mock(),
    )

    assert result.external_post_id == "media-1"
    post.assert_not_called()


@pytest.mark.parametrize("stage", ["create", "publish"])
def test_post_timeout_is_delivery_unknown_and_never_retried(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    request = httpx.Request("POST", "https://graph.instagram.com/v24.0/account/media")
    post = Mock()
    if stage == "create":
        post.side_effect = httpx.ReadTimeout("timed out", request=request)
        metadata: dict[str, object] = {}
        get = Mock()
    else:
        post.side_effect = [
            _response(200, {"id": "container-1"}, "POST", "https://meta.test/media"),
            httpx.ReadTimeout("timed out", request=request),
        ]
        metadata = {}
        get = Mock(return_value=_response(200, {"status_code": "FINISHED"}, "GET", "https://meta.test/container"))
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)

    with pytest.raises(SocialProviderError) as error:
        _provider().publish("token", "account", "https://s3.example/video", "caption", metadata, Mock())

    assert error.value.code == "delivery_unknown"
    assert metadata["instagram_reconciliation_required"] is True
    assert post.call_count == (1 if stage == "create" else 2)


def test_media_id_is_preserved_when_final_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        Mock(
            side_effect=[
                _response(200, {"id": "container-1"}, "POST", "https://meta.test/media"),
                _response(200, {"id": "media-1"}, "POST", "https://meta.test/publish"),
            ]
        ),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        Mock(
            side_effect=[
                _response(200, {"status_code": "FINISHED"}, "GET", "https://meta.test/container"),
                _response(503, {}, "GET", "https://meta.test/media"),
            ]
        ),
    )
    metadata: dict[str, object] = {}

    with pytest.raises(SocialProviderError) as error:
        _provider().publish("token", "account", "https://s3.example/video", "caption", metadata, Mock())

    assert error.value.code == "instagram_media_verification_pending"
    assert metadata["instagram_media_id"] == "media-1"
    assert metadata["instagram_reconciliation_required"] is True


def test_saved_publish_request_without_media_id_never_posts_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = Mock()
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(
        httpx,
        "get",
        Mock(return_value=_response(200, {"status_code": "FINISHED"}, "GET", "https://meta.test/container")),
    )
    metadata: dict[str, object] = {
        "instagram_container_id": "container-1",
        "instagram_publish_requested_at": "2026-09-16T00:00:00+00:00",
    }

    with pytest.raises(SocialProviderError) as error:
        _provider().publish("token", "account", "https://s3.example/video", "caption", metadata, Mock())

    assert error.value.code == "delivery_unknown"
    assert metadata["instagram_reconciliation_required"] is True
    post.assert_not_called()


def test_delivery_unknown_stops_publication_after_one_attempt() -> None:
    publication = SimpleNamespace(
        id=1,
        status=SocialPublicationStatus.PENDING,
        social_account_connection_id=2,
        file_asset_id=3,
        error_message=None,
    )
    connection = SimpleNamespace(platform=SocialPlatform.INSTAGRAM)
    asset = SimpleNamespace(workflow_execution_id=4)
    session = Mock()

    def get(model: object, object_id: int) -> object | None:
        if model is SocialPublication and object_id == 1:
            return publication
        if model is SocialAccountConnection and object_id == 2:
            return connection
        if model is FileAsset and object_id == 3:
            return asset
        return None

    session.get.side_effect = get
    publisher = Mock()
    publisher.publish.side_effect = SocialProviderError("unknown", code="delivery_unknown")
    service = PublicationService(session, Mock(), Mock(), lambda _platform: publisher)

    service.execute(1)

    attempts = [
        call.args[0]
        for call in session.add.call_args_list
        if call.args and call.args[0].__class__.__name__ == "SocialPublicationAttempt"
    ]
    assert publisher.publish.call_count == 1
    assert publication.status is SocialPublicationStatus.FAILED
    assert len(attempts) == 1
    assert attempts[0].status is SocialPublicationAttemptStatus.FAILED


def test_publishing_instagram_attempt_resumes_without_creating_another_attempt() -> None:
    publication = SimpleNamespace(
        id=1,
        status=SocialPublicationStatus.PUBLISHING,
        social_account_connection_id=2,
        file_asset_id=3,
        error_message=None,
        external_post_id=None,
        external_post_url=None,
        published_at=None,
    )
    connection = SimpleNamespace(platform=SocialPlatform.INSTAGRAM)
    asset = SimpleNamespace(workflow_execution_id=4)
    resumed = SimpleNamespace(
        attempt_no=1,
        status=SocialPublicationAttemptStatus.RUNNING,
        metadata_={"instagram_container_id": "container-1"},
    )
    session = Mock()

    def get(model: object, object_id: int) -> object | None:
        if model is SocialPublication and object_id == 1:
            return publication
        if model is SocialAccountConnection and object_id == 2:
            return connection
        if model is FileAsset and object_id == 3:
            return asset
        return None

    session.get.side_effect = get
    session.scalar.return_value = resumed
    publisher = Mock()
    publisher.publish.return_value = PublishedPost("media-1", "https://instagram.com/reel/1")
    service = PublicationService(session, Mock(), Mock(), lambda _platform: publisher)

    service.execute(1)

    publisher.publish.assert_called_once()
    assert publication.status is SocialPublicationStatus.SUCCESS
    assert not any(
        call.args and call.args[0].__class__.__name__ == "SocialPublicationAttempt"
        for call in session.add.call_args_list
    )


def test_unresolved_historical_delivery_blocks_force_republish() -> None:
    asset = SimpleNamespace(id=847)
    connection = SimpleNamespace(id=10, platform=SocialPlatform.INSTAGRAM)
    previous_publication = SimpleNamespace(status=SocialPublicationStatus.FAILED)
    previous_attempt = SimpleNamespace(metadata_={"instagram_publish_requested_at": "saved"})
    session = Mock()
    session.scalar.return_value = connection
    session.execute.return_value.all.return_value = [(previous_publication, previous_attempt)]
    publisher = Mock()
    publisher.reconcile.return_value = None
    service = PublicationService(session, Mock(), Mock(), lambda _platform: publisher)
    service._approved_video_asset = Mock(return_value=asset)  # type: ignore[method-assign]
    execution = SimpleNamespace(user_id=1)

    with pytest.raises(ValueError, match="unresolved delivery result"):
        service.request_publish(
            execution,
            [SocialPlatform.INSTAGRAM],
            SimpleNamespace(youtube_title="title", youtube_description="", instagram_caption="caption"),
            force_republish=True,
        )

    publisher.reconcile.assert_called_once()
    session.add.assert_not_called()
