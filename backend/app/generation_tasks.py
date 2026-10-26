import os

from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.database import engine as database_engine
from app.generation_dispatch import (
    GenerationDispatchService,
    GenerationLeaseHeartbeat,
    execute_claimed_intent,
)
from app.models import WorkflowExecution


@celery_app.task(name="app.generation_tasks.execute_generation")
def execute_generation(intent_id: int) -> None:
    """A worker owns a V3 segment only after atomically claiming its durable intent."""
    from app.main import get_ai_workflow_services, get_workflow_engine
    from app.workflow_execution_service import WorkflowExecutionService

    with Session(database_engine) as session:
        dispatch = GenerationDispatchService(session, celery_app, settings.generation_queue)
        disable_heartbeat = (
            settings.e2e_fake_providers
            and os.environ.get("VORA_E2E_DISABLE_HEARTBEAT", "").lower() == "true"
        )
        lease_seconds = settings.generation_dispatch_lease_seconds
        if settings.e2e_fake_providers and os.environ.get("VORA_E2E_LEASE_SECONDS"):
            lease_seconds = int(os.environ["VORA_E2E_LEASE_SECONDS"])
        intent = dispatch.claim(intent_id, lease_seconds=lease_seconds)
        if intent is None:
            return
        claim_token = intent.claim_token
        if claim_token is None:
            return
        heartbeat = GenerationLeaseHeartbeat(
            lambda: Session(database_engine),
            celery_app,
            settings.generation_queue,
            intent.id,
            claim_token,
            intent.claim_generation,
            lease_seconds,
        )
        if not disable_heartbeat:
            heartbeat.start()
        try:
            workflow = session.get(WorkflowExecution, intent.workflow_execution_id)
            if workflow is None:
                raise ValueError("Workflow execution does not exist")
            engine = get_workflow_engine(session, get_ai_workflow_services(session), workflow.workflow_version)
            engine.set_execution_guard(lambda: not heartbeat.lost and dispatch.owns(
                intent.id, claim_token, intent.claim_generation
            ))
            engine.set_execution_fence(
                lambda: not heartbeat.lost
                and dispatch.owns_for_update(intent.id, claim_token, intent.claim_generation)
            )
            execute_claimed_intent(
                session,
                intent,
                engine,
                WorkflowExecutionService(session).input_data,
            )
        except Exception as exc:
            dispatch.fail(intent_id, exc, claim_token, intent.claim_generation)
            raise
        finally:
            if not disable_heartbeat:
                heartbeat.stop()
        dispatch.complete(intent_id, claim_token, intent.claim_generation)


@celery_app.task(name="app.generation_tasks.relay_generation_intents")
def relay_generation_intents() -> int:
    with Session(database_engine) as session:
        return GenerationDispatchService(session, celery_app, settings.generation_queue).relay_pending()
