"""Publication aggregate: independent from WorkflowEngine and durable in PostgreSQL."""

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from celery import Celery  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy import func, select
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
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.publication_copy import PublicationCopyService
from app.social_providers import PublishedPost, SocialProviderError


class PublicationEvents(Protocol):
    def publish(self, workflow_execution_id: int, publication_id: int, platform: str, stage: str, message: str, attempt_no: int) -> None: ...

    def subscribe(self, workflow_execution_id: int) -> Iterator[dict[str, object]]: ...


class PublicationDispatcher(Protocol):
    def enqueue(self, publication_id: int) -> str: ...


class SocialPublisher(Protocol):
    def publish(
        self,
        connection: SocialAccountConnection,
        assets: list[FileAsset],
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
    """Creates and executes durable Instagram Carousel publication intents."""

    def __init__(
        self,
        session: Session,
        dispatcher: PublicationDispatcher,
        events: PublicationEvents,
        publisher_factory: Callable[[SocialPlatform], SocialPublisher],
        copy_service: PublicationCopyService | None = None,
    ) -> None:
        self._session = session
        self._dispatcher = dispatcher
        self._events = events
        self._publisher_factory = publisher_factory
        self._copy_service = copy_service

    def draft(self, execution: WorkflowExecution) -> PublicationDraft:
        plan = self._latest_node(execution.id, "CONTENT_PLAN")
        planning = plan.output_data if plan and isinstance(plan.output_data, dict) else {}
        copy = self._copy_service.generate(planning, {}) if self._copy_service else PublicationCopyService.fallback(planning)
        return PublicationDraft(copy.youtube_title, copy.youtube_description, copy.instagram_caption)

    def request_publish(
        self,
        execution: WorkflowExecution,
        platforms: list[SocialPlatform],
        draft: PublicationDraft,
        force_republish: bool = False,
    ) -> list[SocialPublication]:
        if platforms != [SocialPlatform.INSTAGRAM]:
            raise ValueError("V3 card-news publication currently supports INSTAGRAM only")
        publication_node, asset_ids = self._approved_publication_node(execution)
        connection = self._session.scalar(
            select(SocialAccountConnection).where(
                SocialAccountConnection.user_id == execution.user_id,
                SocialAccountConnection.platform == SocialPlatform.INSTAGRAM,
            )
        )
        if connection is None:
            raise ValueError("INSTAGRAM account is not connected")
        key = self._idempotency_key(publication_node.id, asset_ids, connection.id, draft)
        if force_republish:
            key = f"{key}:{uuid4()}"
        existing = self._session.scalar(
            select(SocialPublication).where(SocialPublication.idempotency_key == key)
        )
        if existing is not None:
            return [existing]
        publication = SocialPublication(
            node_execution_id=publication_node.id,
            social_account_connection_id=connection.id,
            idempotency_key=key,
            instagram_caption=draft.instagram_caption,
        )
        self._session.add(publication)
        self._session.commit()
        try:
            self._dispatcher.enqueue(publication.id)
        except Exception as exc:  # noqa: BLE001 - broker client implementations vary
            # The intent is durable even if the broker is unavailable. It can be
            # re-enqueued later without changing its idempotency key.
            self._record_enqueue_failure(publication, exc)
        return [publication]

    def execute(self, publication_id: int) -> None:
        """Claim briefly, call providers outside a DB transaction, then finalize."""
        claimed = self._claim(publication_id)
        if claimed is None:
            return
        publication, attempt, connection, assets = claimed

        def persist_progress() -> None:
            flag_modified(attempt, "metadata_")
            self._session.commit()

        try:
            publisher = self._publisher_factory(connection.platform)
        except Exception as exc:  # noqa: BLE001 - plugin/provider construction varies
            attempt.error_code, attempt.error_message = "publisher_initialization_failed", type(exc).__name__
            attempt.status, attempt.finished_at = SocialPublicationAttemptStatus.FAILED, datetime.now(UTC)
            publication.status = SocialPublicationStatus.FAILED
            self._session.commit()
            return
        try:
            result = publisher.publish(connection, assets, publication, attempt, persist_progress)
        except SocialProviderError as exc:
            attempt.error_code, attempt.error_message = exc.code, str(exc)
            if exc.code == "delivery_unknown":
                self._preserve_for_reconciliation(attempt)
            else:
                attempt.status, attempt.finished_at = SocialPublicationAttemptStatus.FAILED, datetime.now(UTC)
                publication.status = SocialPublicationStatus.FAILED
            self._session.commit()
            return
        except Exception as exc:  # noqa: BLE001 - a provider may have POSTed before raising
            attempt.error_code, attempt.error_message = "publication_internal_error", type(exc).__name__
            # Publishing may have crossed a non-idempotent Meta boundary. Never
            # convert that uncertainty into an automatic retryable FAILED state.
            self._preserve_for_reconciliation(attempt)
            self._session.commit()
            return
        attempt.status, attempt.finished_at = SocialPublicationAttemptStatus.SUCCESS, datetime.now(UTC)
        publication.status = SocialPublicationStatus.PUBLISHED
        publication.external_post_id, publication.external_post_url = result.external_post_id, result.external_post_url
        publication.published_at = datetime.now(UTC)
        self._session.commit()

    def reconcile(self, publication_id: int) -> bool:
        """Read-only provider recovery. This deliberately never calls publish()."""
        publication = self._session.get(SocialPublication, publication_id)
        if publication is None or publication.status is not SocialPublicationStatus.PUBLISHING:
            return False
        connection = self._session.get(SocialAccountConnection, publication.social_account_connection_id)
        attempt = self._session.scalar(select(SocialPublicationAttempt).where(SocialPublicationAttempt.social_publication_id == publication.id).order_by(SocialPublicationAttempt.attempt_no.desc()))
        if connection is None or connection.platform is not SocialPlatform.INSTAGRAM or attempt is None:
            return False
        result = self._publisher_factory(connection.platform).reconcile(connection, attempt, lambda: self._session.commit())
        if result is None:
            return False
        publication.status, publication.external_post_id, publication.external_post_url = SocialPublicationStatus.PUBLISHED, result.external_post_id, result.external_post_url
        publication.published_at = datetime.now(UTC)
        attempt.status, attempt.finished_at = SocialPublicationAttemptStatus.SUCCESS, datetime.now(UTC)
        self._session.commit()
        return True

    def _claim(self, publication_id: int) -> tuple[SocialPublication, SocialPublicationAttempt, SocialAccountConnection, list[FileAsset]] | None:
        publication = self._session.scalar(select(SocialPublication).where(SocialPublication.id == publication_id).with_for_update())
        if publication is None or publication.status is not SocialPublicationStatus.PENDING:
            self._session.rollback()
            return None
        connection = self._session.get(SocialAccountConnection, publication.social_account_connection_id)
        node = self._session.get(NodeExecution, publication.node_execution_id)
        if connection is None or node is None:
            publication.status = SocialPublicationStatus.FAILED
            self._session.commit()
            return None
        # V3 card-news publication is Instagram-only. Legacy YouTube rows and
        # providers remain isolated from this worker boundary.
        if connection.platform is not SocialPlatform.INSTAGRAM:
            publication.status = SocialPublicationStatus.FAILED
            self._session.commit()
            return None
        asset_ids = node.output_data.get("asset_ids") if isinstance(node.output_data, dict) else None
        assets = list(self._session.scalars(select(FileAsset).where(FileAsset.id.in_(asset_ids or []))))
        by_id = {asset.id: asset for asset in assets}
        try:
            ordered = self._validated_assets(node, asset_ids, by_id)
        except ValueError:
            publication.status = SocialPublicationStatus.FAILED
            self._session.commit()
            return None
        last_attempt = self._session.scalar(select(func.max(SocialPublicationAttempt.attempt_no)).where(SocialPublicationAttempt.social_publication_id == publication.id)) or 0
        attempt = SocialPublicationAttempt(social_publication_id=publication.id, attempt_no=last_attempt + 1, status=SocialPublicationAttemptStatus.RUNNING, started_at=datetime.now(UTC), metadata_={"publication_intent_recorded_at": datetime.now(UTC).isoformat()})
        publication.status = SocialPublicationStatus.PUBLISHING
        self._session.add(attempt)
        self._session.commit()
        return publication, attempt, connection, ordered

    def _validated_assets(self, publication_node: NodeExecution, asset_ids: object, by_id: dict[int, FileAsset]) -> list[FileAsset]:
        if not isinstance(asset_ids, list) or len(asset_ids) != 5 or not all(isinstance(item, int) for item in asset_ids):
            raise ValueError("Publication assets are invalid")
        image_node = self._session.scalar(select(NodeExecution).where(
            NodeExecution.workflow_execution_id == publication_node.workflow_execution_id,
            NodeExecution.node_type == "IMAGE_GENERATE",
            NodeExecution.revision_no == publication_node.revision_no,
            NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
        ))
        if image_node is None or image_node.output_data is None or image_node.output_data.get("asset_ids") != asset_ids:
            raise ValueError("Publication images do not match the approved IMAGE_GENERATE result")
        successful_creators = set(self._session.scalars(select(NodeExecution.id).where(
            NodeExecution.workflow_execution_id == publication_node.workflow_execution_id,
            NodeExecution.node_type == "IMAGE_GENERATE",
            NodeExecution.revision_no <= publication_node.revision_no,
            NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
        )))
        ordered = sorted(by_id.values(), key=lambda asset: asset.slide_no or 0)
        if len(ordered) != 5 or [asset.slide_no for asset in ordered] != [1, 2, 3, 4, 5] or any(asset.asset_type.value != "IMAGE" or asset.workflow_execution_id != publication_node.workflow_execution_id or asset.created_by_node_execution_id not in successful_creators for asset in ordered):
            raise ValueError("Publication images are not the approved five slides")
        return ordered

    def _record_enqueue_failure(self, publication: SocialPublication, exc: Exception) -> None:
        last_attempt = self._session.scalar(
            select(func.max(SocialPublicationAttempt.attempt_no)).where(
                SocialPublicationAttempt.social_publication_id == publication.id
            )
        ) or 0
        attempt = SocialPublicationAttempt(social_publication_id=publication.id, attempt_no=last_attempt + 1, status=SocialPublicationAttemptStatus.FAILED, error_code="enqueue_failed", error_message=type(exc).__name__, finished_at=datetime.now(UTC), metadata_={"reenqueue_required": True})
        self._session.add(attempt)
        self._session.commit()

    def _preserve_for_reconciliation(self, attempt: SocialPublicationAttempt) -> None:
        """A running attempt has no terminal result while Meta delivery is unknown."""
        attempt.status, attempt.finished_at = SocialPublicationAttemptStatus.RUNNING, None
        attempt.metadata_["reconciliation_required"] = True
        flag_modified(attempt, "metadata_")

    def _approved_publication_node(
        self, execution: WorkflowExecution
    ) -> tuple[NodeExecution, list[int]]:
        node = self._latest_node(execution.id, "PUBLICATION")
        if (
            execution.status is not WorkflowExecutionStatus.SUCCEEDED
            or node is None
            or node.status is not NodeExecutionStatus.SUCCEEDED
            or not isinstance(node.output_data, dict)
        ):
            raise ValueError("Approved image publication is required before publishing")
        asset_ids = node.output_data.get("asset_ids")
        if not isinstance(asset_ids, list) or len(asset_ids) != 5 or not all(
            isinstance(asset_id, int) for asset_id in asset_ids
        ):
            raise ValueError("Publication node does not contain five approved image assets")
        return node, asset_ids

    def _latest_node(self, workflow_execution_id: int, node_type: str) -> NodeExecution | None:
        return self._session.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == workflow_execution_id,
                NodeExecution.node_type == node_type,
            )
            .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
        )

    @staticmethod
    def _idempotency_key(
        node_execution_id: int,
        asset_ids: list[int],
        connection_id: int,
        draft: PublicationDraft,
    ) -> str:
        raw = f"{node_execution_id}:{asset_ids}:{connection_id}:INSTAGRAM:{draft.instagram_caption}"
        return hashlib.sha256(raw.encode()).hexdigest()
