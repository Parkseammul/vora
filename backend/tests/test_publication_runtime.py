from unittest.mock import Mock

import pytest

from app import publication_runtime
from app.social_providers import SocialPlatform


def test_youtube_publisher_defers_storage_provider_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_factory = Mock()
    monkeypatch.setattr(publication_runtime, "get_storage_provider", storage_factory)

    service = publication_runtime.get_publication_service(Mock())
    service._publisher_factory(SocialPlatform.YOUTUBE)

    storage_factory.assert_not_called()
    service._publisher_factory(SocialPlatform.INSTAGRAM)
    storage_factory.assert_called_once_with()
