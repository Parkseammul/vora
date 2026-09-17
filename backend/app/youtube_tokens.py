"""Durable, pre-upload YouTube OAuth token handling."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SocialAccountConnection, SocialConnectionStatus, SocialPlatform
from app.social_providers import SocialProviderError, YouTubeProvider

TOKEN_REFRESH_SKEW = timedelta(minutes=5)


class YouTubeTokenService:
    def __init__(
        self,
        session: Session,
        provider: YouTubeProvider,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        # Token changes use their own transaction: committing the caller's shared
        # publication Session could persist unrelated work, and returning with its
        # row lock held would unnecessarily cover the upload HTTP request.
        self._session_factory = session_factory or (lambda: Session(bind=session.get_bind()))
        self._provider = provider

    def valid_access_token(self, connection_id: int) -> str:
        result: str | None = None
        failure: SocialProviderError | None = None
        # The transaction closes before this method returns, releasing FOR UPDATE
        # before RuntimePublisher can make the non-idempotent upload request.
        with self._session_factory() as token_session, token_session.begin():
            connection = token_session.scalar(
                select(SocialAccountConnection)
                .where(SocialAccountConnection.id == connection_id)
                .with_for_update()
            )
            if connection is None or connection.platform is not SocialPlatform.YOUTUBE:
                failure = SocialProviderError(
                    "YouTube account connection was not found", code="reauth_required"
                )
            else:
                now = datetime.now(UTC)
                if (
                    connection.token_expires_at
                    and connection.token_expires_at > now + TOKEN_REFRESH_SKEW
                ):
                    result = connection.access_token
                elif not connection.refresh_token:
                    connection.status = SocialConnectionStatus.EXPIRED
                    failure = SocialProviderError(
                        "YouTube reconnection is required", code="reauth_required"
                    )
                else:
                    try:
                        refreshed = self._provider.refresh_access_token(connection.refresh_token)
                    except SocialProviderError as exc:
                        if exc.code == "reauth_required":
                            connection.status = SocialConnectionStatus.EXPIRED
                        failure = exc
                    else:
                        connection.access_token = refreshed.access_token
                        connection.token_expires_at = now + timedelta(seconds=refreshed.expires_in)
                        if refreshed.refresh_token:
                            connection.refresh_token = refreshed.refresh_token
                        connection.status = SocialConnectionStatus.CONNECTED
                        result = connection.access_token
        if failure is not None:
            raise failure
        if result is None:  # Defensive: all transaction outcomes set a result or failure.
            raise SocialProviderError("YouTube token refresh did not produce a token")
        return result
