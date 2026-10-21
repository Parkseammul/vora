from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app.instagram_carousel import CarouselImagePreparer
from app.models import (
    SocialPlatform,
    SocialPublicationAttemptStatus,
    SocialPublicationStatus,
)
from app.publication import PublicationService
from app.social_providers import InstagramCarouselProvider, SocialProviderError


def _response(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("POST", "https://meta.test"))


def test_carousel_creates_ordered_children_parent_and_media(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = []
    post_results = iter([*[_response({"id": f"child-{number}"}) for number in range(1, 6)], _response({"id": "parent-1"}), _response({"id": "media-1"})])

    def post(_url: str, **kwargs: object) -> httpx.Response:
        calls.append(kwargs["data"])  # type: ignore[arg-type]
        return next(post_results)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", Mock(side_effect=[
        _response({"status_code": "FINISHED"}),
        _response({"id": "media-1", "permalink": "https://instagram.com/p/1"}),
    ]))
    metadata: dict[str, object] = {}
    result = InstagramCarouselProvider(None, None, None, "v24.0", 0, 1).publish_carousel(
        "token", "account", [f"https://cdn.example/{number}.jpg" for number in range(1, 6)], "caption", metadata, Mock()
    )
    assert result.external_post_id == "media-1"
    assert [item["image_url"] for item in calls[:5]] == [f"https://cdn.example/{number}.jpg" for number in range(1, 6)]
    assert calls[5] == {"media_type": "CAROUSEL", "children": "child-1,child-2,child-3,child-4,child-5", "caption": "caption"}
    assert calls[6] == {"creation_id": "parent-1"}


def test_carousel_timeout_after_publish_intent_never_posts_again(monkeypatch: pytest.MonkeyPatch) -> None:
    post = Mock()
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", Mock(return_value=_response({"status_code": "FINISHED"})))
    metadata: dict[str, object] = {
        "instagram_carousel_child_ids": [f"child-{number}" for number in range(1, 6)],
        "instagram_carousel_parent_id": "parent-1",
        "instagram_publish_requested_at": "2026-01-01T00:00:00+00:00",
    }
    with pytest.raises(SocialProviderError, match="unknown") as error:
        InstagramCarouselProvider(None, None, None, "v24.0", 0, 1).publish_carousel("token", "account", [f"https://cdn.example/{number}.jpg" for number in range(1, 6)], "caption", metadata, Mock())
    assert error.value.code == "delivery_unknown"
    post.assert_not_called()


def test_derived_jpeg_key_is_stable_and_existing_object_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = Mock()
    storage.exists.return_value = True
    storage.presigned_get_url.side_effect = lambda key, **_kwargs: f"https://cdn.example/{key}"
    preparer = CarouselImagePreparer(storage)
    assets = [SimpleNamespace(id=number, storage_key=f"original/{number}.png") for number in range(1, 6)]
    # No materialization/upload proves source PNG objects are untouched when a
    # deterministic derived JPEG already exists.
    assert preparer.prepare(99, assets) == [
        f"https://cdn.example/publications/99/instagram-carousel/{number}.jpg" for number in range(1, 6)
    ]
    storage.materialize_private.assert_not_called()
    storage.upload_private.assert_not_called()
    assert CarouselImagePreparer.storage_key(99, 1).endswith("/1.jpg")


def test_v3_worker_rejects_non_instagram_publication_before_provider_call() -> None:
    publication = SimpleNamespace(
        id=1,
        status=SocialPublicationStatus.PENDING,
        social_account_connection_id=2,
        node_execution_id=3,
    )
    connection = SimpleNamespace(platform=SocialPlatform.YOUTUBE)
    node = SimpleNamespace()
    session = Mock()
    session.scalar.return_value = publication
    session.get.side_effect = [connection, node]
    publisher = Mock()
    PublicationService(session, Mock(), Mock(), lambda _platform: publisher).execute(1)
    assert publication.status is SocialPublicationStatus.FAILED
    publisher.publish.assert_not_called()


def _claimed_publication() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, list[SimpleNamespace]]:
    return (
        SimpleNamespace(id=1, status=SocialPublicationStatus.PUBLISHING),
        SimpleNamespace(status=SocialPublicationAttemptStatus.RUNNING, finished_at=None, metadata_={}),
        SimpleNamespace(platform=SocialPlatform.INSTAGRAM),
        [],
    )


def test_unexpected_provider_exception_requires_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    publication, attempt, connection, assets = _claimed_publication()
    session, publisher = Mock(), Mock()
    publisher.publish.side_effect = RuntimeError("connection dropped after POST")
    service = PublicationService(session, Mock(), Mock(), lambda _platform: publisher)
    monkeypatch.setattr(service, "_claim", lambda _id: (publication, attempt, connection, assets))
    monkeypatch.setattr("app.publication.flag_modified", lambda *_args: None)

    service.execute(1)

    assert publication.status is SocialPublicationStatus.PUBLISHING
    assert attempt.status is SocialPublicationAttemptStatus.RUNNING
    assert attempt.finished_at is None
    assert attempt.metadata_["reconciliation_required"] is True


def test_publisher_factory_failure_is_recorded_as_terminal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    publication, attempt, connection, assets = _claimed_publication()
    session = Mock()
    service = PublicationService(session, Mock(), Mock(), Mock(side_effect=RuntimeError("bad configuration")))
    monkeypatch.setattr(service, "_claim", lambda _id: (publication, attempt, connection, assets))

    service.execute(1)

    assert publication.status is SocialPublicationStatus.FAILED
    assert attempt.status is SocialPublicationAttemptStatus.FAILED
    assert attempt.error_code == "publisher_initialization_failed"
    assert attempt.finished_at is not None


def test_enqueue_failures_use_new_attempt_numbers() -> None:
    publication = SimpleNamespace(id=1)
    session = Mock()
    session.scalar.side_effect = [3, 4]
    service = PublicationService(session, Mock(), Mock(), Mock())

    service._record_enqueue_failure(publication, RuntimeError())
    service._record_enqueue_failure(publication, RuntimeError())

    assert [call.args[0].attempt_no for call in session.add.call_args_list] == [4, 5]


def test_reconciliation_rejects_non_instagram_connection() -> None:
    publication = SimpleNamespace(id=1, status=SocialPublicationStatus.PUBLISHING, social_account_connection_id=2)
    session = Mock()
    session.get.side_effect = [publication, SimpleNamespace(platform=SocialPlatform.YOUTUBE)]
    factory = Mock()

    assert PublicationService(session, Mock(), Mock(), factory).reconcile(1) is False
    factory.assert_not_called()
