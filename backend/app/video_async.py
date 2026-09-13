import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from celery import Celery  # type: ignore[import-untyped]
from redis import Redis

from app.models import NodeExecution
from app.node_executors import NodeExecutionResult, NodeExecutor

if TYPE_CHECKING:
    from app.workflow_engine import WorkflowEngine


VideoProgressCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class QueuedVideoTask:
    task_id: str
    queue: str


class VideoTaskDispatcher(Protocol):
    def enqueue(
        self, workflow_execution_id: int, node_execution_id: int, attempt_id: int
    ) -> QueuedVideoTask: ...

    def publish_progress(
        self, workflow_execution_id: int, stage: str, message: str
    ) -> None: ...


class VideoProgressEvents(Protocol):
    def publish(self, workflow_execution_id: int, stage: str, message: str) -> None: ...

    def subscribe(self, workflow_execution_id: int) -> Iterator[dict[str, object]]: ...


class RedisVideoProgressEvents:
    """Redis carries ephemeral UI progress only; workflow state remains in PostgreSQL."""

    def __init__(self, redis_url: str) -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=True)

    def publish(self, workflow_execution_id: int, stage: str, message: str) -> None:
        self._redis.publish(
            self._channel(workflow_execution_id),
            json.dumps(
                {
                    "workflow_execution_id": workflow_execution_id,
                    "node": "video_generation",
                    "stage": stage,
                    "message": message,
                }
            ),
        )

    def subscribe(self, workflow_execution_id: int) -> Iterator[dict[str, object]]:
        pubsub = self._redis.pubsub()
        pubsub.subscribe(self._channel(workflow_execution_id))
        try:
            for event in pubsub.listen():
                if event["type"] == "message":
                    payload = event["data"]
                    if isinstance(payload, str):
                        try:
                            decoded = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(decoded, dict):
                            yield decoded
        finally:
            pubsub.close()

    @staticmethod
    def _channel(workflow_execution_id: int) -> str:
        return f"workflow-execution:{workflow_execution_id}:video-progress"


class CeleryVideoTaskDispatcher:
    """Only video_generation is queued; this is deliberately not a general async framework."""

    def __init__(self, celery: Celery, events: VideoProgressEvents, queue: str) -> None:
        self._celery = celery
        self._events = events
        self._queue = queue

    def enqueue(
        self, workflow_execution_id: int, node_execution_id: int, attempt_id: int
    ) -> QueuedVideoTask:
        result = self._celery.send_task(
            "app.video_tasks.execute_video_generation",
            kwargs={
                "workflow_execution_id": workflow_execution_id,
                "node_execution_id": node_execution_id,
                "attempt_id": attempt_id,
            },
            queue=self._queue,
        )
        queued = QueuedVideoTask(str(result.id), self._queue)
        self._publish_progress_safely(
            workflow_execution_id, "QUEUED", "Video generation is queued"
        )
        return queued

    def publish_progress(
        self, workflow_execution_id: int, stage: str, message: str
    ) -> None:
        self._publish_progress_safely(workflow_execution_id, stage, message)

    def _publish_progress_safely(
        self, workflow_execution_id: int, stage: str, message: str
    ) -> None:
        # Redis progress is intentionally best-effort: it must not duplicate queued work.
        try:
            self._events.publish(workflow_execution_id, stage, message)
        except Exception:  # noqa: BLE001
            return


def sse_event(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


class VideoGenerationWorker:
    """Runs one queued attempt; the Engine remains the sole workflow-state writer."""

    def __init__(
        self,
        get_node_execution: Callable[[int], NodeExecution | None],
        executor: NodeExecutor,
        workflow_engine: "WorkflowEngine",
        dispatcher: VideoTaskDispatcher,
    ) -> None:
        self._get_node_execution = get_node_execution
        self._executor = executor
        self._workflow_engine = workflow_engine
        self._dispatcher = dispatcher

    def execute(self, workflow_execution_id: int, node_execution_id: int, attempt_id: int) -> None:
        node_execution = self._get_node_execution(node_execution_id)
        if node_execution is None or node_execution.workflow_execution_id != workflow_execution_id:
            raise ValueError("Video generation node execution does not exist")
        try:
            result = self._executor.execute(
                "video_generation",
                node_execution.input_data,
                workflow_execution_id=workflow_execution_id,
                node_execution_id=node_execution_id,
                video_progress_callback=lambda stage, message: self._dispatcher.publish_progress(
                    workflow_execution_id, stage, message
                ),
            )
            if not isinstance(result, NodeExecutionResult):
                raise TypeError("video_generation must return NodeExecutionResult")
        except Exception as exc:  # noqa: BLE001
            self._workflow_engine.complete_video_attempt_failure(node_execution_id, attempt_id, exc)
            return
        self._workflow_engine.complete_video_attempt_success(node_execution_id, attempt_id, result)
