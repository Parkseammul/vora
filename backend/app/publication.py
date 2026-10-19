"""Publication aggregate: independent from WorkflowEngine and durable in PostgreSQL."""

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from celery import Celery  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    FileAsset,
    NodeExecution,
    NodeExecutionStatus,
    SocialAccountConnection,
    SocialPlatform,
    SocialPublication,
    SocialPublicationAttempt,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.publication_copy import PublicationCopyService
from app.social_providers import PublishedPost


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
    """Creates durable Instagram Carousel intents; this MVP never calls Instagram."""

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
        )
        self._session.add(publication)
        self._session.commit()
        # Carousel provider work is intentionally deferred. PENDING means ready,
        # not published; no dispatcher/provider call occurs in this MVP.
        return [publication]

    def execute(self, publication_id: int) -> None:
        """Do not dispatch external publishing before the Carousel provider WBS."""
        del publication_id

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
