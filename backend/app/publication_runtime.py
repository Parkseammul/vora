"""Composition root for publication workers; it does not involve WorkflowEngine."""

from pathlib import Path

from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.llm_provider import LLMProvider
from app.publication import (
    CeleryPublicationDispatcher,
    PublicationService,
    RedisPublicationEvents,
)
from app.publication_copy import PublicationCopyService
from app.social_providers import (
    InstagramProvider,
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

    def publish(self, connection, asset, publication, attempt, persist_progress) -> PublishedPost:  # type: ignore[no-untyped-def]
        del attempt, persist_progress
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
            local_path = (Path(settings.uploads_root) / asset.storage_key).resolve()
            return provider.publish(
                access_token,
                str(local_path),
                publication.youtube_title or "VORA video",
                publication.youtube_description or "",
            )
        raise SocialProviderError(
            "Instagram Carousel provider is not implemented; publication remains PENDING",
            code="not_implemented",
        )

    def reconcile(self, connection, attempt, persist_progress) -> PublishedPost | None:  # type: ignore[no-untyped-def]
        if self._platform is not SocialPlatform.INSTAGRAM:
            return None
        return InstagramProvider(
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


def get_publication_service(session: Session, llm_provider: LLMProvider | None = None) -> PublicationService:
    return PublicationService(
        session,
        CeleryPublicationDispatcher(celery_app, settings.publication_queue),
        RedisPublicationEvents(settings.redis_url),
        lambda platform: RuntimePublisher(
            platform,
            get_storage_provider() if platform is SocialPlatform.INSTAGRAM else None,
            session,
        ),
        PublicationCopyService(llm_provider, settings.llm_provider, settings.publication_copy_model)
        if llm_provider is not None
        else None,
    )
