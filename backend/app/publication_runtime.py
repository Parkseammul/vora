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
    YouTubeProvider,
)
from app.storage import LocalStorageProvider, S3StorageProvider, StorageProvider


def get_storage_provider() -> StorageProvider:
    if settings.storage_provider.lower() == "s3":
        return S3StorageProvider(settings.s3_bucket or "", settings.s3_region)
    return LocalStorageProvider(Path(settings.uploads_root))


class RuntimePublisher:
    def __init__(self, platform: SocialPlatform, storage: StorageProvider) -> None:
        self._platform, self._storage = platform, storage

    def publish(self, connection, asset, publication) -> PublishedPost:  # type: ignore[no-untyped-def]
        local_path = (Path(settings.uploads_root) / asset.storage_key).resolve()
        if self._platform is SocialPlatform.YOUTUBE:
            path = self._storage.local_path(asset.storage_key) or local_path
            return YouTubeProvider(settings.youtube_client_id, settings.youtube_client_secret, settings.youtube_redirect_uri).publish(connection.access_token, str(path), publication.youtube_title or "VORA video", publication.youtube_description or "")
        # Instagram receives only a short-lived S3 URL; source video remains private.
        self._storage.upload_private(asset.storage_key, local_path, asset.mime_type)
        url = self._storage.presigned_get_url(asset.storage_key)
        return InstagramProvider(settings.instagram_client_id, settings.instagram_client_secret, settings.instagram_redirect_uri).publish(connection.access_token, connection.external_account_id, url, publication.instagram_caption or "")


def get_publication_service(session: Session, llm_provider: LLMProvider | None = None) -> PublicationService:
    storage = get_storage_provider()
    return PublicationService(
        session,
        CeleryPublicationDispatcher(celery_app, settings.publication_queue),
        RedisPublicationEvents(settings.redis_url),
        lambda platform: RuntimePublisher(platform, storage),
        PublicationCopyService(llm_provider, settings.llm_provider, settings.publication_copy_model)
        if llm_provider is not None
        else None,
    )
