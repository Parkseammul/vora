from collections.abc import Iterator, Sequence
from typing import TypeVar
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import main
from app.database import engine
from app.llm_provider import LLMMetadata, LLMProviderType, LLMResult
from app.models import (
    ApprovalDecision,
    NodeExecution,
    NodeExecutionStatus,
    User,
    UserApproval,
    WorkflowExecution,
)
from app.node_executors import FakeNodeExecutor
from app.workflow_definitions import workflow_registry
from app.workflow_engine import WorkflowEngine

ResponseT = TypeVar("ResponseT", bound=BaseModel)


@pytest.fixture
def session() -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    db_session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield db_session
    finally:
        db_session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    def get_test_session() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_session] = get_test_session
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


class ScriptImpactProvider:
    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        provider: LLMProviderType,
        model: str,
        images: Sequence[str] = (),
    ) -> LLMResult:
        return LLMResult(
            data=response_model.model_validate({"target_node": "script_generation"}),
            metadata=LLMMetadata(provider=provider, model=model),
        )


def prepare_script_approval(session: Session) -> WorkflowExecution:
    user = User(email=f"{uuid4()}@example.com", name="revision api tester")
    session.add(user)
    session.flush()
    execution = WorkflowExecution(
        user_id=user.id,
        workflow_key="vora_content_creation",
        workflow_version=1,
    )
    session.add(execution)
    session.commit()
    engine_instance = WorkflowEngine(session, workflow_registry, FakeNodeExecutor())
    engine_instance.start_execution(execution.id, {"topic": "VORA"})
    planning = session.scalars(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_key == "content_planning",
        )
    ).one()
    session.add(
        UserApproval(
            node_execution_id=planning.id,
            user_id=user.id,
            decision=ApprovalDecision.APPROVED,
        )
    )
    session.commit()
    engine_instance.resume_after_approval(execution.id)
    return execution


def test_revision_api_uses_ai_target_not_current_node_hint(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = prepare_script_approval(session)
    monkeypatch.setattr(main, "get_llm_provider", lambda: ScriptImpactProvider())
    monkeypatch.setattr(
        main,
        "get_workflow_engine",
        lambda db_session, services: WorkflowEngine(db_session, workflow_registry, FakeNodeExecutor()),
    )

    response = client.post(
        f"/workflow-executions/{execution.id}/revisions",
        json={
            "current_node": "content_planning",
            "revision_request": "첫 문장을 더 강하게 바꿔줘",
        },
    )

    assert response.status_code == 200
    assert response.json()["current_node"] == "script_generation"
    latest_script = session.scalars(
        select(NodeExecution)
        .where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_key == "script_generation",
        )
        .order_by(NodeExecution.user_requested_version.desc())
    ).first()
    assert latest_script is not None
    assert latest_script.user_requested_version == 2
    assert latest_script.status is NodeExecutionStatus.WAITING_APPROVAL


def test_application_wiring_reuses_one_injected_provider_for_all_ai_services(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptImpactProvider()
    monkeypatch.setattr(main, "get_llm_provider", lambda: provider)

    services = main.get_ai_workflow_services(session)
    workflow_engine = main.get_workflow_engine(session, services)

    assert services.content_planning._llm_provider is provider
    assert services.script_generation._llm_provider is provider
    assert services.revision_impact._llm_provider is provider
    assert workflow_engine._executor._content_planning_service is services.content_planning
    assert workflow_engine._executor._script_generation_service is services.script_generation


@pytest.mark.parametrize("revision_request", ["", "   "])
def test_revision_api_rejects_blank_request(
    client: TestClient, revision_request: str
) -> None:
    response = client.post(
        "/workflow-executions/1/revisions",
        json={"current_node": "script_generation", "revision_request": revision_request},
    )

    assert response.status_code == 422
