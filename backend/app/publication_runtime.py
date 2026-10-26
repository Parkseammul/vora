"""Composition root for publication workers; it does not involve WorkflowEngine."""

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.instagram_carousel import CarouselImagePreparer
from app.llm_provider import LLMProvider
from app.publication import (
    CeleryPublicationDispatcher,
    PublicationService,
    RedisPublicationEvents,
)
from app.publication_copy import PublicationCopyService
from app.social_providers import (
    InstagramCarouselProvider,
    PublishedPost,
    SocialPlatform,
    SocialProviderError,
    YouTubeProvider,
)
from app.storage import LocalStorageProvider, S3StorageProvider, StorageProvider
from app.youtube_tokens import YouTubeTokenService


def get_storage_provider() -> StorageProvider:
    if settings.storage_provider.lower() == "s3":
        return S3StorageProvider(settings.s3_bucket or "", settings.s3_region)
    return LocalStorageProvider(Path(settings.uploads_root))


class RuntimePublisher:
    def __init__(self, platform: SocialPlatform, storage: StorageProvider | None, session: Session) -> None:
        self._platform, self._storage, self._session = platform, storage, session

    def publish(self, connection, assets, publication, attempt, persist_progress) -> PublishedPost:  # type: ignore[no-untyped-def]
        # Kept only for the legacy video worker contract. V3 PublicationService
        # rejects YOUTUBE before this boundary and never calls a provider.
        if self._platform is SocialPlatform.YOUTUBE:
            provider = YouTubeProvider(
                settings.youtube_client_id,
                settings.youtube_client_secret,
                settings.youtube_redirect_uri,
            )
            access_token = YouTubeTokenService(self._session, provider).valid_access_token(
                connection.id
            )
            asset = assets[0] if isinstance(assets, list) else assets
            local_path = (Path(settings.uploads_root) / asset.storage_key).resolve()
            return provider.publish(
                access_token,
                str(local_path),
                publication.youtube_title or "VORA video",
                publication.youtube_description or "",
            )
        if self._platform is not SocialPlatform.INSTAGRAM or self._storage is None:
            raise SocialProviderError("Publication platform is not configured", code="configuration")
        if connection.token_expires_at is not None and connection.token_expires_at <= datetime.now(UTC):
            raise SocialProviderError("Instagram access token has expired; reconnect the account", code="authorization")
        urls = CarouselImagePreparer(self._storage).prepare(publication.id, assets)
        return InstagramCarouselProvider(
            settings.instagram_client_id,
            settings.instagram_client_secret,
            settings.instagram_redirect_uri,
            settings.instagram_graph_api_version,
            settings.instagram_container_poll_interval_seconds,
            settings.instagram_container_poll_max_attempts,
        ).publish_carousel(
            connection.access_token,
            connection.external_account_id,
            urls,
            publication.instagram_caption or "",
            attempt.metadata_,
            persist_progress,
        )

    def reconcile(self, connection, attempt, persist_progress) -> PublishedPost | None:  # type: ignore[no-untyped-def]
        if self._platform is not SocialPlatform.INSTAGRAM:
            return None
        return InstagramCarouselProvider(
            settings.instagram_client_id,
            settings.instagram_client_secret,
            settings.instagram_redirect_uri,
            settings.instagram_graph_api_version,
            settings.instagram_container_poll_interval_seconds,
            settings.instagram_container_poll_max_attempts,
        ).reconcile(
            connection.access_token,
            connection.external_account_id,
            attempt.metadata_,
            persist_progress,
        )


class DeterministicE2EInstagramPublisher:
    """Offline publication adapter; never constructs an Instagram HTTP client."""

    def publish(self, connection, assets, publication, attempt, persist_progress) -> PublishedPost:  # type: ignore[no-untyped-def]
        # Unlike the real non-idempotent provider, this deterministic adapter
        # has no intermediate remote state to persist.
        del connection, assets, attempt, persist_progress
        if publication.instagram_caption == "__VORA_E2E_FAKE_PUBLICATION_FAILURE__":
            raise SocialProviderError("Deterministic E2E publication failure", code="e2e_fake_failure")
        return PublishedPost(f"e2e-instagram-{publication.id}", f"https://e2e.invalid/{publication.id}")

    def reconcile(self, connection, attempt, persist_progress) -> PublishedPost | None:  # type: ignore[no-untyped-def]
        del connection, attempt, persist_progress
        return None


def get_publication_service(session: Session, llm_provider: LLMProvider | None = None) -> PublicationService:
    return PublicationService(
        session,
        CeleryPublicationDispatcher(celery_app, settings.publication_queue),
        RedisPublicationEvents(settings.redis_url),
        lambda platform: DeterministicE2EInstagramPublisher()
        if settings.e2e_fake_providers and platform is SocialPlatform.INSTAGRAM
        else RuntimePublisher(
            platform,
            get_storage_provider() if platform is SocialPlatform.INSTAGRAM else None,
            session,
        ),
        PublicationCopyService(llm_provider, settings.llm_provider, settings.publication_copy_model)
        if llm_provider is not None
        else None,
    )
