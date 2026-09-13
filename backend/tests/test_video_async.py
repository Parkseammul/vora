from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import main
from app.database import engine as database_engine
from app.models import (
    ApprovalDecision,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    NodeExecutionStatus,
    User,
    UserApproval,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.node_executors import NodeExecutionResult
from app.video_async import (
    CeleryVideoTaskDispatcher,
    QueuedVideoTask,
    RedisVideoProgressEvents,
    VideoGenerationWorker,
    sse_event,
)
from app.video_generation import TransientVideoProviderError
from app.workflow_definitions import workflow_registry
from app.workflow_engine import WorkflowEngine


class RecordingDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[tuple[int, int, int]] = []
        self.events: list[dict[str, object]] = []

    def enqueue(
        self, workflow_execution_id: int, node_execution_id: int, attempt_id: int
    ) -> QueuedVideoTask:
        self.enqueued.append((workflow_execution_id, node_execution_id, attempt_id))
        self.publish_progress(workflow_execution_id, "QUEUED", "Video generation is queued")
        return QueuedVideoTask(f"task-{attempt_id}", "video_generation")

    def publish_progress(self, workflow_execution_id: int, stage: str, message: str) -> None:
        self.events.append(
            {
                "workflow_execution_id": workflow_execution_id,
                "node": "video_generation",
                "stage": stage,
                "message": message,
            }
        )


class CompletingDuringEnqueueDispatcher(RecordingDispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.workflow_engine: WorkflowEngine | None = None

    def enqueue(
        self, workflow_execution_id: int, node_execution_id: int, attempt_id: int
    ) -> QueuedVideoTask:
        queued = super().enqueue(workflow_execution_id, node_execution_id, attempt_id)
        assert self.workflow_engine is not None
        self.workflow_engine.complete_video_attempt_success(
            node_execution_id,
            attempt_id,
            NodeExecutionResult({"video_asset_id": 99}, {"provider": "fake"}),
        )
        return queued


class VideoExecutor:
    def __init__(self, outcomes: list[NodeExecutionResult | Exception]) -> None:
        self._outcomes = outcomes

    def execute(self, node_key: str, input_data: object, **kwargs: object) -> NodeExecutionResult:
        assert node_key == "video_generation"
        progress = kwargs["video_progress_callback"]
        assert callable(progress)
        progress("SCENE_GENERATION", "Generating video for scene 1")
        progress("TTS_GENERATION", "Generating narration for scene 1")
        progress("COMPOSING", "Composing final video")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class CeleryResult:
    id: str


class CeleryStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_task(self, name: str, **kwargs: object) -> CeleryResult:
        self.calls.append({"name": name, **kwargs})
        return CeleryResult("celery-task-1")


class EventStub:
    def __init__(self) -> None:
        self.events: list[tuple[int, str, str]] = []

    def publish(self, workflow_execution_id: int, stage: str, message: str) -> None:
        self.events.append((workflow_execution_id, stage, message))

    def subscribe(self, workflow_execution_id: int) -> Iterator[dict[str, object]]:
        yield {
            "workflow_execution_id": workflow_execution_id,
            "node": "video_generation",
            "stage": "QUEUED",
            "message": "Video generation is queued",
        }


class PubSubStub:
    def __init__(self) -> None:
        self.closed = False

    def subscribe(self, channel: str) -> None:
        assert channel == "workflow-execution:42:video-progress"

    def listen(self) -> Iterator[dict[str, object]]:
        yield {"type": "message", "data": "{malformed"}
        yield {
            "type": "message",
            "data": '{"workflow_execution_id": 42, "stage": "COMPLETED"}',
        }

    def close(self) -> None:
        self.closed = True


class RedisStub:
    def __init__(self, pubsub: PubSubStub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> PubSubStub:
        return self._pubsub


@pytest.fixture
def session() -> Iterator[Session]:
    connection = database_engine.connect()
    transaction = connection.begin()
    db_session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield db_session
    finally:
        db_session.close()
        transaction.rollback()
        connection.close()


def test_celery_dispatcher_enqueues_video_task_and_publishes_queued_event() -> None:
    celery, events = CeleryStub(), EventStub()
    dispatcher = CeleryVideoTaskDispatcher(celery, events, "video_generation")  # type: ignore[arg-type]

    queued = dispatcher.enqueue(10, 20, 30)

    assert queued == QueuedVideoTask("celery-task-1", "video_generation")
    assert celery.calls == [
        {
            "name": "app.video_tasks.execute_video_generation",
            "kwargs": {
                "workflow_execution_id": 10,
                "node_execution_id": 20,
                "attempt_id": 30,
            },
            "queue": "video_generation",
        }
    ]
    assert events.events == [(10, "QUEUED", "Video generation is queued")]


def test_async_video_worker_completes_with_waiting_approval_and_sse_stages(session: Session) -> None:
    execution, _ = _approved_script(session)
    dispatcher = RecordingDispatcher()
    executor = VideoExecutor([NodeExecutionResult({"video_asset_id": 99}, {"provider": "fake"})])
    workflow_engine = WorkflowEngine(session, workflow_registry, executor, dispatcher)  # type: ignore[arg-type]

    assert workflow_engine.resume_after_approval(execution.id) is WorkflowExecutionStatus.RUNNING
    node, attempt = _video_node_and_attempt(session, execution.id)
    assert attempt.metadata_["celery_task_id"] == f"task-{attempt.id}"
    assert attempt.metadata_["queue_state"] == "QUEUED"

    VideoGenerationWorker(
        lambda node_id: session.get(NodeExecution, node_id), executor, workflow_engine, dispatcher
    ).execute(execution.id, node.id, attempt.id)

    session.refresh(execution)
    session.refresh(node)
    session.refresh(attempt)
    assert execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
    assert node.status is NodeExecutionStatus.WAITING_APPROVAL
    assert attempt.status is NodeExecutionAttemptStatus.SUCCESS
    assert attempt.metadata_["queue_state"] == "COMPLETED"
    assert [event["stage"] for event in dispatcher.events] == [
        "QUEUED",
        "SCENE_GENERATION",
        "TTS_GENERATION",
        "COMPOSING",
        "COMPLETED",
    ]


def test_enqueue_race_preserves_worker_completed_state(session: Session) -> None:
    execution, _ = _approved_script(session)
    dispatcher = CompletingDuringEnqueueDispatcher()
    executor = VideoExecutor([])
    workflow_engine = WorkflowEngine(session, workflow_registry, executor, dispatcher)  # type: ignore[arg-type]
    dispatcher.workflow_engine = workflow_engine

    assert workflow_engine.resume_after_approval(execution.id) is WorkflowExecutionStatus.WAITING_APPROVAL
    node, attempt = _video_node_and_attempt(session, execution.id)
    session.refresh(execution)
    session.refresh(node)
    session.refresh(attempt)
    assert execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
    assert node.status is NodeExecutionStatus.WAITING_APPROVAL
    assert attempt.status is NodeExecutionAttemptStatus.SUCCESS
    assert attempt.metadata_["queue_state"] == "COMPLETED"
    assert attempt.metadata_["celery_task_id"] == f"task-{attempt.id}"
    assert attempt.metadata_["queue"] == "video_generation"


def test_retryable_video_error_creates_one_new_attempt_then_succeeds(session: Session) -> None:
    execution, _ = _approved_script(session)
    dispatcher = RecordingDispatcher()
    executor = VideoExecutor(
        [TimeoutError("provider timed out"), NodeExecutionResult({"video_asset_id": 99}, {})]
    )
    workflow_engine = WorkflowEngine(session, workflow_registry, executor, dispatcher)  # type: ignore[arg-type]
    workflow_engine.resume_after_approval(execution.id)
    node, first_attempt = _video_node_and_attempt(session, execution.id)
    worker = VideoGenerationWorker(
        lambda node_id: session.get(NodeExecution, node_id), executor, workflow_engine, dispatcher
    )

    worker.execute(execution.id, node.id, first_attempt.id)
    attempts = _attempts(session, node.id)
    assert [attempt.status for attempt in attempts] == [
        NodeExecutionAttemptStatus.FAILED,
        NodeExecutionAttemptStatus.RUNNING,
    ]
    assert node.status is NodeExecutionStatus.RUNNING
    assert len(dispatcher.enqueued) == 2

    worker.execute(execution.id, node.id, attempts[1].id)
    assert [attempt.status for attempt in _attempts(session, node.id)] == [
        NodeExecutionAttemptStatus.FAILED,
        NodeExecutionAttemptStatus.SUCCESS,
    ]
    assert node.status is NodeExecutionStatus.WAITING_APPROVAL


def test_temporary_provider_error_is_retryable(session: Session) -> None:
    execution, _ = _approved_script(session)
    dispatcher = RecordingDispatcher()
    executor = VideoExecutor([TransientVideoProviderError("provider overloaded")])
    workflow_engine = WorkflowEngine(session, workflow_registry, executor, dispatcher)  # type: ignore[arg-type]
    workflow_engine.resume_after_approval(execution.id)
    node, attempt = _video_node_and_attempt(session, execution.id)

    VideoGenerationWorker(
        lambda node_id: session.get(NodeExecution, node_id), executor, workflow_engine, dispatcher
    ).execute(execution.id, node.id, attempt.id)

    assert [attempt.status for attempt in _attempts(session, node.id)] == [
        NodeExecutionAttemptStatus.FAILED,
        NodeExecutionAttemptStatus.RUNNING,
    ]


def test_retryable_video_error_stops_after_three_attempts(session: Session) -> None:
    execution, _ = _approved_script(session)
    dispatcher = RecordingDispatcher()
    executor = VideoExecutor([TimeoutError("timeout")] * 3)
    workflow_engine = WorkflowEngine(session, workflow_registry, executor, dispatcher)  # type: ignore[arg-type]
    workflow_engine.resume_after_approval(execution.id)
    node, attempt = _video_node_and_attempt(session, execution.id)
    worker = VideoGenerationWorker(
        lambda node_id: session.get(NodeExecution, node_id), executor, workflow_engine, dispatcher
    )

    worker.execute(execution.id, node.id, attempt.id)
    worker.execute(execution.id, node.id, _attempts(session, node.id)[1].id)
    worker.execute(execution.id, node.id, _attempts(session, node.id)[2].id)

    session.refresh(execution)
    assert len(_attempts(session, node.id)) == 3
    assert node.status is NodeExecutionStatus.FAILED
    assert execution.status is WorkflowExecutionStatus.FAILED
    assert len(dispatcher.enqueued) == 3
    assert dispatcher.events[-1]["stage"] == "FAILED"


def test_permanent_video_error_does_not_retry(session: Session) -> None:
    execution, _ = _approved_script(session)
    dispatcher = RecordingDispatcher()
    executor = VideoExecutor([ValueError("Configured fixed BGM FileAsset is invalid")])
    workflow_engine = WorkflowEngine(session, workflow_registry, executor, dispatcher)  # type: ignore[arg-type]
    workflow_engine.resume_after_approval(execution.id)
    node, attempt = _video_node_and_attempt(session, execution.id)

    VideoGenerationWorker(
        lambda node_id: session.get(NodeExecution, node_id), executor, workflow_engine, dispatcher
    ).execute(execution.id, node.id, attempt.id)

    session.refresh(execution)
    assert len(_attempts(session, node.id)) == 1
    assert node.status is NodeExecutionStatus.FAILED
    assert execution.status is WorkflowExecutionStatus.FAILED


def test_video_progress_sse_streams_redis_event_shape() -> None:
    events = EventStub()
    main.app.dependency_overrides[main.get_video_progress_events] = lambda: events
    try:
        with TestClient(main.app) as client:
            response = client.get("/workflow-executions/42/video-generation/events")
    finally:
        main.app.dependency_overrides.clear()

    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"workflow_execution_id": 42' in response.text
    assert '"stage": "QUEUED"' in response.text


def test_redis_progress_subscription_skips_malformed_json_and_keeps_streaming() -> None:
    pubsub = PubSubStub()
    events = RedisVideoProgressEvents.__new__(RedisVideoProgressEvents)
    events._redis = RedisStub(pubsub)  # type: ignore[assignment]

    received = list(events.subscribe(42))
    sse_payload = "".join(sse_event(payload) for payload in received)

    assert received == [{"workflow_execution_id": 42, "stage": "COMPLETED"}]
    assert 'data: {"workflow_execution_id": 42, "stage": "COMPLETED"}' in sse_payload
    assert pubsub.closed is True


def _approved_script(session: Session) -> tuple[WorkflowExecution, NodeExecution]:
    user = User(email=f"{uuid4()}@example.com", name="async video tester")
    session.add(user)
    session.flush()
    execution = WorkflowExecution(
        user_id=user.id,
        workflow_key="vora_content_creation",
        workflow_version=1,
        status=WorkflowExecutionStatus.WAITING_APPROVAL,
    )
    session.add(execution)
    session.flush()
    planning = NodeExecution(
        workflow_execution_id=execution.id,
        node_key="content_planning",
        status=NodeExecutionStatus.SUCCESS,
        output_data={"planning": "output"},
    )
    script = NodeExecution(
        workflow_execution_id=execution.id,
        node_key="script_generation",
        status=NodeExecutionStatus.WAITING_APPROVAL,
        output_data={"script": "output"},
    )
    session.add_all([planning, script])
    session.flush()
    session.add(
        UserApproval(
            node_execution_id=script.id,
            user_id=user.id,
            decision=ApprovalDecision.APPROVED,
        )
    )
    session.commit()
    return execution, script


def _video_node_and_attempt(
    session: Session, workflow_execution_id: int
) -> tuple[NodeExecution, NodeExecutionAttempt]:
    node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == workflow_execution_id,
            NodeExecution.node_key == "video_generation",
        )
    )
    assert node is not None
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == node.id)
    )
    assert attempt is not None
    return node, attempt


def _attempts(session: Session, node_execution_id: int) -> list[NodeExecutionAttempt]:
    return list(
        session.scalars(
            select(NodeExecutionAttempt)
            .where(NodeExecutionAttempt.node_execution_id == node_execution_id)
            .order_by(NodeExecutionAttempt.attempt_no)
        )
    )
