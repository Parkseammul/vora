"""Publication aggregate: independent from WorkflowEngine and durable in PostgreSQL."""

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from celery import Celery  # type: ignore[import-untyped]
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import (
    FileAsset,
    NodeExecution,
    NodeExecutionStatus,
    SocialAccountConnection,
    SocialPlatform,
    SocialPublication,
    SocialPublicationAttempt,
    SocialPublicationAttemptStatus,
    SocialPublicationStatus,
    UserApproval,
    WorkflowExecution,
)
from app.publication_copy import PublicationCopyService
from app.social_providers import PublishedPost, SocialProviderError

MAX_PUBLICATION_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 3


class PublicationEvents(Protocol):
    def publish(self, workflow_execution_id: int, publication_id: int, platform: str, stage: str, message: str, attempt_no: int) -> None: ...

    def subscribe(self, workflow_execution_id: int) -> Iterator[dict[str, object]]: ...


class PublicationDispatcher(Protocol):
    def enqueue(self, publication_id: int) -> str: ...


class SocialPublisher(Protocol):
    def publish(
        self,
        connection: SocialAccountConnection,
        asset: FileAsset,
        publication: SocialPublication,
        attempt: SocialPublicationAttempt,
        persist_progress: Callable[[], None],
    ) -> PublishedPost: ...

    def reconcile(
        self,
        connection: SocialAccountConnection,
        attempt: SocialPublicationAttempt,
        persist_progress: Callable[[], None],
    ) -> PublishedPost | None: ...


class RedisPublicationEvents:
    """SSE events are transient; publication state and attempts remain in PostgreSQL."""

    def __init__(self, redis_url: str) -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=True)

    def publish(self, workflow_execution_id: int, publication_id: int, platform: str, stage: str, message: str, attempt_no: int) -> None:
        self._redis.publish(self._channel(workflow_execution_id), json.dumps({"workflow_execution_id": workflow_execution_id, "publication_id": publication_id, "platform": platform, "stage": stage, "message": message, "attempt": attempt_no}))

    def subscribe(self, workflow_execution_id: int) -> Iterator[dict[str, object]]:
        pubsub = self._redis.pubsub()
        pubsub.subscribe(self._channel(workflow_execution_id))
        try:
            for event in pubsub.listen():
                if event["type"] == "message" and isinstance(event["data"], str):
                    try:
                        payload = json.loads(event["data"])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        yield payload
        finally:
            pubsub.close()

    @staticmethod
    def _channel(workflow_execution_id: int) -> str:
        return f"workflow-execution:{workflow_execution_id}:publication-progress"


class CeleryPublicationDispatcher:
    def __init__(self, celery: Celery, queue: str) -> None:
        self._celery, self._queue = celery, queue

    def enqueue(self, publication_id: int) -> str:
        return str(self._celery.send_task("app.publication_tasks.execute_publication", kwargs={"publication_id": publication_id}, queue=self._queue).id)


@dataclass(frozen=True)
class PublicationDraft:
    youtube_title: str
    youtube_description: str
    instagram_caption: str


class PublicationService:
    def __init__(self, session: Session, dispatcher: PublicationDispatcher, events: PublicationEvents, publisher_factory: Callable[[SocialPlatform], SocialPublisher], copy_service: PublicationCopyService | None = None) -> None:
        self._session, self._dispatcher, self._events, self._publisher_factory = session, dispatcher, events, publisher_factory
        self._copy_service = copy_service

    def draft(self, execution: WorkflowExecution) -> PublicationDraft:
        plan = self._session.scalar(select(NodeExecution).where(NodeExecution.workflow_execution_id == execution.id, NodeExecution.node_key == "content_planning").order_by(NodeExecution.user_requested_version.desc()))
        planning = plan.output_data if plan and isinstance(plan.output_data, dict) else {}
        script_node = self._session.scalar(select(NodeExecution).where(NodeExecution.workflow_execution_id == execution.id, NodeExecution.node_key == "script_generation").order_by(NodeExecution.user_requested_version.desc()))
        script = script_node.output_data if script_node and isinstance(script_node.output_data, dict) else {}
        copy = self._copy_service.generate(planning, script) if self._copy_service else PublicationCopyService.fallback(planning)
        return PublicationDraft(copy.youtube_title, copy.youtube_description, copy.instagram_caption)

    def request_publish(self, execution: WorkflowExecution, platforms: list[SocialPlatform], draft: PublicationDraft, force_republish: bool = False) -> list[SocialPublication]:
        asset = self._approved_video_asset(execution)
        publications: list[SocialPublication] = []
        for platform in platforms:
            connection = self._session.scalar(select(SocialAccountConnection).where(SocialAccountConnection.user_id == execution.user_id, SocialAccountConnection.platform == platform))
            if connection is None:
                raise ValueError(f"{platform.value} account is not connected")
            if platform is SocialPlatform.INSTAGRAM:
                self._reconcile_unresolved_instagram_publications(connection, asset)
            key = self._idempotency_key(asset.id, connection.id, platform, draft)
            if force_republish:
                key = f"{key}:{uuid4()}"
            existing = self._session.scalar(select(SocialPublication).where(SocialPublication.idempotency_key == key))
            if existing is not None:
                publications.append(existing)
                continue
            publication = SocialPublication(social_account_connection_id=connection.id, file_asset_id=asset.id, idempotency_key=key, youtube_title=draft.youtube_title, youtube_description=draft.youtube_description, instagram_caption=draft.instagram_caption)
            self._session.add(publication)
            self._session.flush()
            publications.append(publication)
        self._session.commit()
        for publication in publications:
            if publication.status is SocialPublicationStatus.PENDING:
                self._dispatcher.enqueue(publication.id)
        return publications

    def _reconcile_unresolved_instagram_publications(
        self, connection: SocialAccountConnection, asset: FileAsset
    ) -> None:
        """Block a new POST until an ambiguous predecessor is read-only reconciled."""
        records = self._session.execute(
            select(SocialPublication, SocialPublicationAttempt)
            .join(
                SocialPublicationAttempt,
                SocialPublicationAttempt.social_publication_id == SocialPublication.id,
            )
            .where(
                SocialPublication.social_account_connection_id == connection.id,
                SocialPublication.file_asset_id == asset.id,
                SocialPublication.status != SocialPublicationStatus.SUCCESS,
                or_(
                    SocialPublicationAttempt.error_code.in_(
                        ("delivery_unknown", "instagram_media_verification_pending")
                    ),
                    # A worker can have died after persisting publish intent but
                    # before recording an error. Treat every active predecessor
                    # for this account/asset as unresolved until reconciled.
                    SocialPublicationAttempt.status == SocialPublicationAttemptStatus.RUNNING,
                ),
            )
            .order_by(SocialPublication.id.desc(), SocialPublicationAttempt.attempt_no.desc())
        ).all()
        publisher = self._publisher_factory(SocialPlatform.INSTAGRAM)
        for previous_publication, previous_attempt in records:
            def persist_progress(
                attempt: SocialPublicationAttempt = previous_attempt,
            ) -> None:
                flag_modified(attempt, "metadata_")
                self._session.commit()

            post = publisher.reconcile(connection, previous_attempt, persist_progress)
            if post is None:
                raise ValueError(
                    "An earlier Instagram publication has an unresolved delivery result; "
                    "reconciliation is required before republishing"
                )
            previous_attempt.status = SocialPublicationAttemptStatus.SUCCESS
            previous_publication.status = SocialPublicationStatus.SUCCESS
            previous_publication.external_post_id = post.external_post_id
            previous_publication.external_post_url = post.external_post_url
            previous_publication.published_at = datetime.now(UTC)
            self._session.commit()

    def execute(self, publication_id: int) -> None:
        publication = self._session.get(SocialPublication, publication_id)
        if publication is None:
            return
        connection = self._session.get(SocialAccountConnection, publication.social_account_connection_id)
        asset = self._session.get(FileAsset, publication.file_asset_id)
        if connection is None or asset is None:
            self._fail(publication, "Publication dependencies were not found", "dependency")
            return
        resumed_attempt: SocialPublicationAttempt | None = None
        if publication.status is SocialPublicationStatus.PUBLISHING and connection.platform is SocialPlatform.INSTAGRAM:
            resumed_attempt = self._session.scalar(
                select(SocialPublicationAttempt)
                .where(
                    SocialPublicationAttempt.social_publication_id == publication.id,
                    SocialPublicationAttempt.status == SocialPublicationAttemptStatus.RUNNING,
                )
                .order_by(SocialPublicationAttempt.attempt_no.desc())
            )
            if resumed_attempt is None:
                return
        elif publication.status is not SocialPublicationStatus.PENDING:
            return
        else:
            publication.status = SocialPublicationStatus.PUBLISHING
            self._session.commit()

        start_attempt = resumed_attempt.attempt_no if resumed_attempt is not None else 1
        for attempt_no in range(start_attempt, MAX_PUBLICATION_ATTEMPTS + 1):
            if resumed_attempt is not None and attempt_no == start_attempt:
                attempt = resumed_attempt
            else:
                attempt = SocialPublicationAttempt(
                    social_publication_id=publication.id,
                    attempt_no=attempt_no,
                    status=SocialPublicationAttemptStatus.RUNNING,
                    started_at=datetime.now(UTC),
                )
                self._session.add(attempt)
            self._session.commit()
            self._emit(publication, connection.platform, "PUBLISHING", "Publishing started", attempt_no)
            try:
                def persist_progress(current_attempt: SocialPublicationAttempt = attempt) -> None:
                    # JSONB is not mutable-tracked; make each external boundary durable.
                    flag_modified(current_attempt, "metadata_")
                    self._session.commit()

                post = self._publisher_factory(connection.platform).publish(
                    connection, asset, publication, attempt, persist_progress
                )
            except SocialProviderError as exc:
                attempt.status, attempt.error_code, attempt.error_message, attempt.finished_at = SocialPublicationAttemptStatus.FAILED, exc.code, str(exc), datetime.now(UTC)
                self._session.commit()
                if exc.retryable and attempt_no < MAX_PUBLICATION_ATTEMPTS:
                    self._emit(publication, connection.platform, "RETRYING", "Temporary provider error; retrying", attempt_no)
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                self._fail(publication, str(exc), exc.code, attempt_no, connection.platform)
                return
            attempt.status, attempt.finished_at = SocialPublicationAttemptStatus.SUCCESS, datetime.now(UTC)
            publication.status, publication.external_post_id, publication.external_post_url, publication.published_at = SocialPublicationStatus.SUCCESS, post.external_post_id, post.external_post_url, datetime.now(UTC)
            self._session.commit()
            self._emit(publication, connection.platform, "SUCCESS", "Publishing completed", attempt_no)
            return

    def _approved_video_asset(self, execution: WorkflowExecution) -> FileAsset:
        node = self._session.scalar(select(NodeExecution).where(NodeExecution.workflow_execution_id == execution.id, NodeExecution.node_key == "video_generation").order_by(NodeExecution.user_requested_version.desc()))
        if execution.status.value != "SUCCESS" or node is None or node.status is not NodeExecutionStatus.SUCCESS:
            raise ValueError("Final video approval is required before publishing")
        if self._session.scalar(select(UserApproval).where(UserApproval.node_execution_id == node.id)) is None:
            raise ValueError("Final video approval is required before publishing")
        asset_id = node.output_data.get("video_asset_id") if isinstance(node.output_data, dict) else None
        asset = self._session.get(FileAsset, asset_id) if isinstance(asset_id, int) else None
        if asset is None:
            raise ValueError("Generated video was not found")
        return asset

    @staticmethod
    def _idempotency_key(asset_id: int, connection_id: int, platform: SocialPlatform, draft: PublicationDraft) -> str:
        raw = f"{asset_id}:{connection_id}:{platform.value}:{draft.youtube_title}:{draft.youtube_description}:{draft.instagram_caption}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _fail(self, publication: SocialPublication, message: str, code: str, attempt_no: int = 0, platform: SocialPlatform | None = None) -> None:
        publication.status, publication.error_message = SocialPublicationStatus.FAILED, message
        self._session.commit()
        connection = self._session.get(SocialAccountConnection, publication.social_account_connection_id)
        self._emit(publication, platform or (connection.platform if connection else SocialPlatform.YOUTUBE), "FAILED", message, attempt_no)

    def _emit(self, publication: SocialPublication, platform: SocialPlatform, stage: str, message: str, attempt_no: int) -> None:
        try:
            asset = self._session.get(FileAsset, publication.file_asset_id)
            if asset:
                self._events.publish(asset.workflow_execution_id, publication.id, platform.value, stage, message, attempt_no)
        except (OSError, RedisError):
            return
