from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.database import engine as database_engine
from app.main import get_video_generation_service, get_video_task_dispatcher
from app.models import NodeExecution
from app.node_executors import RuleBasedNodeExecutor
from app.video_async import VideoGenerationWorker
from app.workflow_definitions import workflow_registry
from app.workflow_engine import WorkflowEngine


@celery_app.task(name="app.video_tasks.execute_video_generation")
def execute_video_generation(
    workflow_execution_id: int, node_execution_id: int, attempt_id: int
) -> None:
    """Celery task delegates all durable state transitions to WorkflowEngine methods."""
    with Session(database_engine) as session:
        video_service = get_video_generation_service(session)
        executor = RuleBasedNodeExecutor(video_generation_service=video_service, session=session)
        dispatcher = get_video_task_dispatcher()
        workflow_engine = WorkflowEngine(session, workflow_registry, executor, dispatcher)
        worker = VideoGenerationWorker(
            lambda node_id: session.get(NodeExecution, node_id),
            executor,
            workflow_engine,
            dispatcher,
        )
        worker.execute(workflow_execution_id, node_execution_id, attempt_id)
