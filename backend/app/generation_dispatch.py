"""Durable, at-least-once dispatch for V3 generation workflow segments."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

from celery import Celery  # type: ignore[import-untyped]
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.models import WorkflowDispatchIntent, WorkflowExecution
from app.workflow_engine import WorkflowEngine

ACTION_START = "START"
ACTION_RESUME = "RESUME"
ACTION_REVISION = "REVISION"
PENDING = "PENDING"
QUEUED = "QUEUED"
CLAIMED = "CLAIMED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"


class DispatchLeaseLostError(RuntimeError):
    """Raised when a worker may no longer turn an external result into DB state."""


class GenerationDispatchService:
    """Coordinates broker delivery without granting Redis ownership of state."""

    def __init__(self, session: Session, celery: Celery, queue: str) -> None:
        self._session = session
        self._celery = celery
        self._queue = queue

    def create(
        self,
        workflow_execution_id: int,
        action: str,
        revision_target: str | None = None,
        revision_request: str | None = None,
    ) -> WorkflowDispatchIntent:
        intent = WorkflowDispatchIntent(
            workflow_execution_id=workflow_execution_id,
            action=action,
            revision_target=revision_target,
            revision_request=revision_request,
        )
        self._session.add(intent)
        self._session.flush()
        return intent

    def enqueue(self, intent_id: int) -> bool:
        """Best effort only; a retained PENDING intent is recovered by the relay."""
        intent = self._session.get(WorkflowDispatchIntent, intent_id)
        if intent is None or intent.status not in {PENDING, QUEUED}:
            return False
        try:
            task = self._celery.send_task(
                "app.generation_tasks.execute_generation",
                kwargs={"intent_id": intent.id},
                queue=self._queue,
            )
        except Exception as exc:  # noqa: BLE001
            intent.status = PENDING
            intent.last_error = type(exc).__name__
            self._session.commit()
            return False
        intent.status = QUEUED
        intent.dispatch_count += 1
        intent.celery_task_id = str(task.id)
        intent.last_error = None
        self._session.commit()
        return True

    def relay_pending(self, limit: int = 100) -> int:
        now = datetime.now(UTC)
        intents = list(
            self._session.scalars(
                select(WorkflowDispatchIntent)
                .where(
                    or_(
                        WorkflowDispatchIntent.status == PENDING,
                        (WorkflowDispatchIntent.status == CLAIMED)
                        & (WorkflowDispatchIntent.lease_expires_at < now),
                    )
                )
                .order_by(WorkflowDispatchIntent.created_at)
                .limit(limit)
            )
        )
        for intent in intents:
            if intent.status == CLAIMED:
                intent.status = PENDING
                intent.lease_expires_at = None
        self._session.commit()
        return sum(1 for intent in intents if self.enqueue(intent.id))

    def claim(self, intent_id: int, lease_seconds: int = 900) -> WorkflowDispatchIntent | None:
        now = datetime.now(UTC)
        intent = self._session.scalar(
            select(WorkflowDispatchIntent)
            .where(WorkflowDispatchIntent.id == intent_id)
            .with_for_update()
        )
        if intent is None or intent.status == COMPLETED:
            self._session.rollback()
            return None
        if intent.status == CLAIMED and (intent.lease_expires_at is None or intent.lease_expires_at >= now):
            self._session.rollback()
            return None
        if intent.status not in {PENDING, QUEUED, CLAIMED}:
            self._session.rollback()
            return None
        intent.status = CLAIMED
        intent.claim_generation += 1
        intent.claim_token = str(uuid4())
        intent.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self._session.commit()
        return intent

    def heartbeat(self, intent_id: int, claim_token: str, claim_generation: int, lease_seconds: int) -> bool:
        """Extend only the currently fenced claim; a stale worker cannot revive itself."""
        intent = self._session.scalar(
            select(WorkflowDispatchIntent)
            .where(WorkflowDispatchIntent.id == intent_id)
            .with_for_update()
        )
        if (
            intent is None
            or intent.status != CLAIMED
            or intent.claim_token != claim_token
            or intent.claim_generation != claim_generation
        ):
            self._session.rollback()
            return False
        intent.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        self._session.commit()
        return True

    def owns(self, intent_id: int, claim_token: str, claim_generation: int) -> bool:
        """Fresh scalar query avoids a stale ORM identity-map result after a re-claim."""
        row = self._session.execute(
            select(
                WorkflowDispatchIntent.status,
                WorkflowDispatchIntent.claim_token,
                WorkflowDispatchIntent.claim_generation,
                WorkflowDispatchIntent.lease_expires_at,
            ).where(WorkflowDispatchIntent.id == intent_id)
        ).one_or_none()
        return bool(
            row
            and row.status == CLAIMED
            and row.claim_token == claim_token
            and row.claim_generation == claim_generation
            and row.lease_expires_at is not None
            and row.lease_expires_at > datetime.now(UTC)
        )

    def owns_for_update(self, intent_id: int, claim_token: str, claim_generation: int) -> bool:
        """Validate ownership while locking the intent in the caller's transaction."""
        row = self._session.execute(
            select(
                WorkflowDispatchIntent.status,
                WorkflowDispatchIntent.claim_token,
                WorkflowDispatchIntent.claim_generation,
                WorkflowDispatchIntent.lease_expires_at,
            )
            .where(WorkflowDispatchIntent.id == intent_id)
            .with_for_update()
        ).one_or_none()
        return bool(
            row
            and row.status == CLAIMED
            and row.claim_token == claim_token
            and row.claim_generation == claim_generation
            and row.lease_expires_at is not None
            and row.lease_expires_at > datetime.now(UTC)
        )

    def complete(
        self, intent_id: int, claim_token: str | None = None, claim_generation: int | None = None
    ) -> bool:
        if claim_token is None or claim_generation is None:
            self._session.rollback()
            return False
        result = self._session.execute(
            update(WorkflowDispatchIntent)
            .where(
                WorkflowDispatchIntent.id == intent_id,
                WorkflowDispatchIntent.status == CLAIMED,
                WorkflowDispatchIntent.claim_token == claim_token,
                WorkflowDispatchIntent.claim_generation == claim_generation,
            )
            .returning(WorkflowDispatchIntent.id)
            .values(status=COMPLETED, lease_expires_at=None)
        )
        if result.scalar_one_or_none() is None:
            self._session.rollback()
            return False
        self._session.commit()
        return True

    def fail(
        self,
        intent_id: int,
        error: Exception,
        claim_token: str | None = None,
        claim_generation: int | None = None,
    ) -> bool:
        if claim_token is None or claim_generation is None:
            self._session.rollback()
            return False
        # WorkflowEngine records domain failures. The intent only records dispatch outcome.
        result = self._session.execute(
            update(WorkflowDispatchIntent)
            .where(
                WorkflowDispatchIntent.id == intent_id,
                WorkflowDispatchIntent.status == CLAIMED,
                WorkflowDispatchIntent.claim_token == claim_token,
                WorkflowDispatchIntent.claim_generation == claim_generation,
            )
            .returning(WorkflowDispatchIntent.id)
            .values(
                status=FAILED,
                lease_expires_at=None,
                last_error=type(error).__name__,
            )
        )
        if result.scalar_one_or_none() is None:
            self._session.rollback()
            return False
        self._session.commit()
        return True


class GenerationLeaseHeartbeat:
    """Refresh a claim while provider code runs without holding the worker Session."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        celery: Celery,
        queue: str,
        intent_id: int,
        claim_token: str,
        claim_generation: int,
        lease_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._celery = celery
        self._queue = queue
        self._intent_id = intent_id
        self._claim_token = claim_token
        self._claim_generation = claim_generation
        self._lease_seconds = lease_seconds
        self._stopped = Event()
        self._lost = Event()
        self._thread = Thread(target=self._run, daemon=True, name=f"generation-lease-{intent_id}")

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=1)

    def _run(self) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while not self._stopped.wait(interval):
            with self._session_factory() as session:
                if not GenerationDispatchService(session, self._celery, self._queue).heartbeat(
                    self._intent_id,
                    self._claim_token,
                    self._claim_generation,
                    self._lease_seconds,
                ):
                    self._lost.set()
                    return


def execute_claimed_intent(
    session: Session,
    intent: WorkflowDispatchIntent,
    engine: WorkflowEngine,
    input_factory: Callable[[int], dict[str, object]],
) -> None:
    execution = session.get(WorkflowExecution, intent.workflow_execution_id)
    if execution is None:
        raise ValueError("Workflow execution does not exist")
    if intent.action == ACTION_START:
        if execution.status.value == "PENDING":
            engine.start_execution(execution.id, input_factory(execution.id))
        else:
            engine.recover_interrupted_execution(execution.id)
    elif intent.action == ACTION_RESUME:
        if execution.status.value == "WAITING_APPROVAL":
            engine.resume_after_approval(execution.id)
        else:
            engine.recover_interrupted_execution(execution.id)
    elif intent.action == ACTION_REVISION:
        # API already atomically persisted Revision, NodeExecution and Attempt.
        # Never recreate them in a worker delivery; execute the prepared attempt.
        engine.recover_prepared_revision(execution.id)
    else:
        raise ValueError("Unknown generation dispatch action")
