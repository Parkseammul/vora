"""Composition root for publication workers; it does not involve WorkflowEngine."""

from pathlib import Path
from urllib.parse import urlparse

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


def get_storage_provider() -> StorageProvider:
    if settings.storage_provider.lower() == "s3":
        return S3StorageProvider(settings.s3_bucket or "", settings.s3_region)
    return LocalStorageProvider(Path(settings.uploads_root))


class RuntimePublisher:
    def __init__(self, platform: SocialPlatform, storage: StorageProvider | None) -> None:
        self._platform, self._storage = platform, storage

    def publish(self, connection, asset, publication, attempt, persist_progress) -> PublishedPost:  # type: ignore[no-untyped-def]
        local_path = (Path(settings.uploads_root) / asset.storage_key).resolve()
        if self._platform is SocialPlatform.YOUTUBE:
            return YouTubeProvider(settings.youtube_client_id, settings.youtube_client_secret, settings.youtube_redirect_uri).publish(connection.access_token, str(local_path), publication.youtube_title or "VORA video", publication.youtube_description or "")
        # Instagram receives only a short-lived S3 URL; source video remains private.
        if self._storage is None:
            raise SocialProviderError("Instagram publishing requires S3 storage", code="configuration")
        if asset.mime_type != "video/mp4":
            raise SocialProviderError("Instagram Reels publishing requires video/mp4", code="invalid_video")
        if not local_path.is_file() or local_path.stat().st_size > 1_000_000_000:
            raise SocialProviderError(
                "Instagram Reels video file is unavailable or exceeds 1 GB", code="invalid_video"
            )
        if settings.instagram_presigned_url_ttl_seconds <= 0:
            raise SocialProviderError("Instagram Reels presigned URL TTL must be positive", code="configuration")
        self._storage.upload_private(asset.storage_key, local_path, asset.mime_type)
        url = self._storage.presigned_get_url(
            asset.storage_key, settings.instagram_presigned_url_ttl_seconds
        )
        if urlparse(url).scheme != "https":
            raise SocialProviderError("Instagram Reels requires an HTTPS video URL", code="configuration")
        return InstagramProvider(
            settings.instagram_client_id,
            settings.instagram_client_secret,
            settings.instagram_redirect_uri,
            settings.instagram_graph_api_version,
            settings.instagram_container_poll_interval_seconds,
            settings.instagram_container_poll_max_attempts,
        ).publish(
            connection.access_token,
            connection.external_account_id,
            url,
            publication.instagram_caption or "",
            attempt.metadata_,
            persist_progress,
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
        ),
        PublicationCopyService(llm_provider, settings.llm_provider, settings.publication_copy_model)
        if llm_provider is not None
        else None,
    )
