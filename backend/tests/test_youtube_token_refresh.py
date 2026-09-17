from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app import publication_runtime
from app.main import _upsert_social_connection
from app.models import SocialConnectionStatus, SocialPlatform
from app.social_providers import OAuthIdentity, SocialProviderError, YouTubeProvider
from app.youtube_tokens import YouTubeTokenService


def _connection(*, expires_at: datetime | None, refresh_token: str | None = "stored-refresh"):
    return SimpleNamespace(
        id=10,
        platform=SocialPlatform.YOUTUBE,
        access_token="stored-access",
        refresh_token=refresh_token,
        token_expires_at=expires_at,
        status=SocialConnectionStatus.CONNECTED,
    )


def _token_service(token_session: MagicMock, provider: Mock) -> YouTubeTokenService:
    token_session.__enter__.return_value = token_session
    return YouTubeTokenService(Mock(), provider, session_factory=lambda: token_session)


def test_valid_token_skips_refresh_and_uses_for_update() -> None:
    session = MagicMock()
    connection = _connection(expires_at=datetime.now(UTC) + timedelta(minutes=6))
    session.scalar.return_value = connection
    provider = Mock()

    token = _token_service(session, provider).valid_access_token(10)

    assert token == "stored-access"
    provider.refresh_access_token.assert_not_called()
    statement = session.scalar.call_args.args[0]
    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
    session.begin.return_value.__exit__.assert_called_once()
    session.__exit__.assert_called_once()


@pytest.mark.parametrize("expires_at", [None, datetime.now(UTC) + timedelta(minutes=1)])
def test_expiring_or_unknown_token_refreshes_and_preserves_refresh_token(expires_at) -> None:
    session = MagicMock()
    connection = _connection(expires_at=expires_at)
    session.scalar.return_value = connection
    provider = Mock()
    provider.refresh_access_token.return_value = SimpleNamespace(
        access_token="new-access", expires_in=3600, refresh_token=None
    )

    token = _token_service(session, provider).valid_access_token(10)

    assert token == "new-access"
    provider.refresh_access_token.assert_called_once_with("stored-refresh")
    assert connection.refresh_token == "stored-refresh"
    assert connection.token_expires_at is not None
    session.begin.return_value.__exit__.assert_called_once()
    session.__exit__.assert_called_once()


def test_missing_refresh_token_requires_reconnect_before_upload() -> None:
    session = MagicMock()
    connection = _connection(expires_at=None, refresh_token=None)
    session.scalar.return_value = connection
    provider = Mock()

    with pytest.raises(SocialProviderError) as error:
        _token_service(session, provider).valid_access_token(10)

    assert error.value.code == "reauth_required"
    assert connection.status is SocialConnectionStatus.EXPIRED
    provider.refresh_access_token.assert_not_called()
    session.begin.return_value.__exit__.assert_called_once()
    session.__exit__.assert_called_once()


def test_second_locked_check_uses_first_refresh_result_without_another_refresh() -> None:
    session = MagicMock()
    connection = _connection(expires_at=datetime.now(UTC))
    session.scalar.side_effect = [connection, connection]
    provider = Mock()
    provider.refresh_access_token.return_value = SimpleNamespace(
        access_token="new-access", expires_in=3600, refresh_token="rotated-refresh"
    )
    service = _token_service(session, provider)

    assert service.valid_access_token(10) == "new-access"
    assert service.valid_access_token(10) == "new-access"

    provider.refresh_access_token.assert_called_once_with("stored-refresh")
    assert connection.refresh_token == "rotated-refresh"


def test_non_reauth_refresh_failure_preserves_connection_and_closes_transaction() -> None:
    session = MagicMock()
    connection = _connection(expires_at=None)
    session.scalar.return_value = connection
    provider = Mock()
    provider.refresh_access_token.side_effect = SocialProviderError(
        "YouTube token refresh failed", code="token_refresh_failed"
    )

    with pytest.raises(SocialProviderError, match="token refresh failed") as error:
        _token_service(session, provider).valid_access_token(10)

    assert error.value.code == "token_refresh_failed"
    assert connection.status is SocialConnectionStatus.CONNECTED
    session.begin.return_value.__exit__.assert_called_once()
    session.__exit__.assert_called_once()


def test_invalid_grant_marks_expired_after_isolated_transaction_closes() -> None:
    session = MagicMock()
    connection = _connection(expires_at=None)
    session.scalar.return_value = connection
    provider = Mock()
    provider.refresh_access_token.side_effect = SocialProviderError(
        "YouTube reconnection is required", code="reauth_required"
    )

    with pytest.raises(SocialProviderError) as error:
        _token_service(session, provider).valid_access_token(10)

    assert error.value.code == "reauth_required"
    assert connection.status is SocialConnectionStatus.EXPIRED
    session.begin.return_value.__exit__.assert_called_once()
    session.__exit__.assert_called_once()


def test_refresh_network_failure_does_not_create_upload_request(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "https://oauth2.googleapis.com/token")
    monkeypatch.setattr(httpx, "post", Mock(side_effect=httpx.ConnectError("offline", request=request)))
    provider = YouTubeProvider("client", "secret", None)

    with pytest.raises(SocialProviderError) as error:
        provider.refresh_access_token("refresh")

    assert error.value.code == "token_refresh_network"


def test_invalid_grant_requires_reconnect_without_exposing_response(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(
        400,
        json={"error": "invalid_grant", "error_description": "sensitive server response"},
        request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
    )
    monkeypatch.setattr(httpx, "post", Mock(return_value=response))

    with pytest.raises(SocialProviderError) as error:
        YouTubeProvider("client", "secret", None).refresh_access_token("refresh")

    assert error.value.code == "reauth_required"
    assert "sensitive" not in str(error.value)


def test_other_google_400_preserves_connection_status(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(
        400,
        json={"error": "invalid_request", "error_description": "sensitive server response"},
        request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
    )
    monkeypatch.setattr(httpx, "post", Mock(return_value=response))
    provider = YouTubeProvider("client", "secret", None)
    token_session = MagicMock()
    connection = _connection(expires_at=None)
    token_session.scalar.return_value = connection

    with pytest.raises(SocialProviderError) as error:
        _token_service(token_session, provider).valid_access_token(10)

    assert error.value.code == "token_refresh_failed"
    assert connection.status is SocialConnectionStatus.CONNECTED
    assert "sensitive" not in str(error.value)
    token_session.begin.return_value.__exit__.assert_called_once()
    token_session.__exit__.assert_called_once()


def test_oauth_reconnect_preserves_refresh_token_only_for_same_youtube_account() -> None:
    user = SimpleNamespace(id=1)
    connection = SimpleNamespace(
        external_account_id="channel-a",
        account_name="old",
        access_token="old-access",
        refresh_token="old-refresh",
        token_expires_at=None,
        status=SocialConnectionStatus.EXPIRED,
    )
    session = Mock()
    session.scalar.return_value = connection
    same_account = OAuthIdentity("channel-a", "new", "new-access", None, datetime.now(UTC))

    _upsert_social_connection(session, user, SocialPlatform.YOUTUBE, same_account)

    assert connection.refresh_token == "old-refresh"
    other_account = OAuthIdentity("channel-b", "other", "other-access", None, datetime.now(UTC))
    _upsert_social_connection(session, user, SocialPlatform.YOUTUBE, other_account)
    assert connection.refresh_token is None


def test_refreshed_token_is_used_for_single_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Mock()
    provider.publish.return_value = SimpleNamespace(external_post_id="video", external_post_url="url")
    token_service = Mock()
    token_service.valid_access_token.return_value = "fresh-access"
    monkeypatch.setattr(publication_runtime, "YouTubeProvider", Mock(return_value=provider))
    monkeypatch.setattr(publication_runtime, "YouTubeTokenService", Mock(return_value=token_service))
    runtime = publication_runtime.RuntimePublisher(SocialPlatform.YOUTUBE, None, Mock())
    connection = SimpleNamespace(id=10, access_token="old")
    asset = SimpleNamespace(storage_key="video.mp4")
    publication = SimpleNamespace(youtube_title="title", youtube_description="description")

    runtime.publish(connection, asset, publication, Mock(), Mock())

    token_service.valid_access_token.assert_called_once_with(10)
    provider.publish.assert_called_once()
    assert provider.publish.call_args.args[0] == "fresh-access"
