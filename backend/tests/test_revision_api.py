from collections.abc import Iterator, Sequence
from typing import TypeVar
from urllib.error import HTTPError, URLError
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
    AssetType,
    ExecutionInputSnapshot,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    NodeExecutionStatus,
    PromptVersion,
    Revision,
    RulesVersion,
    SocialAccountConnection,
    SocialConnectionStatus,
    SocialPlatform,
    SocialPublication,
    SocialPublicationAttempt,
    SocialPublicationAttemptStatus,
    SocialPublicationStatus,
    User,
    UserApproval,
    WorkflowExecution,
)
from app.node_executors import FakeNodeExecutor, NodeExecutorConfigurationError
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
            NodeExecution.node_type == "content_planning",
        )
    ).one()
    session.add(
        UserApproval(
            node_execution_id=planning.id,
            workflow_execution_id=execution.id,
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
    assert response.json()["restart_node"] == "script_generation"
    latest_script = session.scalars(
        select(NodeExecution)
        .where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "script_generation",
        )
        .order_by(NodeExecution.revision_no.desc())
    ).first()
    assert latest_script is not None
    assert latest_script.revision_no == 1
    assert latest_script.status is NodeExecutionStatus.WAITING


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
    client: TestClient, session: Session, revision_request: str
) -> None:
    user = User(email=f"{uuid4()}@example.com", name="blank revision tester")
    session.add(user)
    session.flush()
    execution = WorkflowExecution(
        user_id=user.id,
        workflow_key="vora_content_creation",
        workflow_version=1,
    )
    session.add(execution)
    session.commit()
    response = client.post(
        f"/workflow-executions/{execution.id}/revisions",
        json={"current_node": "script_generation", "revision_request": revision_request},
    )

    assert response.status_code == 422


def test_workflow_read_apis_and_approval_use_latest_node_state(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = prepare_script_approval(session)
    script = session.scalars(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "script_generation",
        )
    ).one()

    workflow_response = client.get(f"/workflow-executions/{execution.id}")
    script_response = client.get(f"/workflow-executions/{execution.id}/script")
    video_response = client.get(f"/workflow-executions/{execution.id}/video")

    assert workflow_response.status_code == 200
    assert workflow_response.json()["nodes"]["script_generation"]["id"] == script.id
    assert workflow_response.json()["nodes"]["script_generation"]["attempt"] == {
        "current": 1,
        "automatic_max": 1,
    }
    assert script_response.json()["node"]["status"] == "WAITING"
    assert video_response.json()["video"] is None

    monkeypatch.setattr(main, "get_llm_provider", lambda: ScriptImpactProvider())
    monkeypatch.setattr(
        main,
        "get_workflow_engine",
        lambda db_session, services: WorkflowEngine(db_session, workflow_registry, FakeNodeExecutor()),
    )
    approval_response = client.post(
        f"/workflow-executions/{execution.id}/approvals",
        json={"node_execution_id": script.id},
    )

    assert approval_response.status_code == 200
    assert approval_response.json()["status"] == "WAITING_APPROVAL"


def test_approval_returns_404_when_authenticated_user_does_not_own_workflow(
    client: TestClient, session: Session
) -> None:
    owner = User(email=f"{uuid4()}@example.com", name="approval owner")
    authenticated_user = User(email=f"{uuid4()}@example.com", name="other user")
    session.add_all([owner, authenticated_user])
    session.flush()
    execution = WorkflowExecution(
        user_id=owner.id,
        workflow_key="vora_content_creation",
        workflow_version=1,
    )
    session.add(execution)
    session.flush()
    waiting_node = NodeExecution(
        workflow_execution_id=execution.id,
        node_type="content_planning",
        status=NodeExecutionStatus.WAITING,
        input_data={},
    )
    session.add(waiting_node)
    session.commit()
    main.app.dependency_overrides[main.get_current_user] = lambda: authenticated_user

    response = client.post(
        f"/workflow-executions/{execution.id}/approvals",
        json={"node_execution_id": waiting_node.id},
    )

    assert response.status_code == 404


def test_trace_returns_full_history_without_payloads_and_does_not_mutate(
    client: TestClient, session: Session
) -> None:
    user = User(email=f"{uuid4()}@example.com", name="trace tester")
    rules = RulesVersion(version=999, rules_data={"private": "not returned"})
    prompt = PromptVersion(node_type="CONTENT_PLAN", version=999, prompt_template="secret prompt")
    session.add_all([user, rules, prompt])
    session.flush()
    execution = WorkflowExecution(user_id=user.id, workflow_key="vora_content_creation", workflow_version=3)
    session.add(execution)
    session.flush()
    source = main.FileAsset(
        workflow_execution_id=execution.id, asset_type=AssetType.IMAGE,
        storage_key=f"trace/{uuid4()}.png", file_name="source.png", mime_type="image/png", file_size=1,
    )
    session.add(source)
    session.flush()
    session.add(ExecutionInputSnapshot(
        workflow_execution_id=execution.id, request_text="private input", marketing_info="private marketing",
        source_asset_id=source.id, rules_version_id=rules.id,
    ))
    node = NodeExecution(workflow_execution_id=execution.id, node_type="CONTENT_PLAN", revision_no=0,
        status=NodeExecutionStatus.FAILED, input_data={"secret": "no"}, output_data={"secret": "no"})
    session.add(node)
    session.flush()
    session.add_all([
        NodeExecutionAttempt(node_execution_id=node.id, attempt_no=1, status=NodeExecutionAttemptStatus.FAILED,
            ai_company="OPENAI", model="test", prompt_version_id=prompt.id,
            error_code="PROVIDER_5XX", error_message="model unavailable oauth-secret"),
        NodeExecutionAttempt(node_execution_id=node.id, attempt_no=2, status=NodeExecutionAttemptStatus.SUCCEEDED),
    ])
    approval = UserApproval(
        workflow_execution_id=execution.id,
        node_execution_id=node.id,
        decision=ApprovalDecision.REJECTED,
    )
    session.add(approval)
    session.flush()
    session.add(Revision(
        workflow_execution_id=execution.id,
        user_approval_id=approval.id,
        revision_no=1,
        start_node_type="CONTENT_PLAN",
        request_text="private revision oauth-secret",
    ))
    connection = SocialAccountConnection(
        user_id=user.id,
        platform=SocialPlatform.INSTAGRAM,
        external_account_id="account",
        status=SocialConnectionStatus.CONNECTED,
        access_token="oauth-secret",
    )
    session.add(connection)
    session.flush()
    publication = SocialPublication(
        node_execution_id=node.id,
        social_account_connection_id=connection.id,
        status=SocialPublicationStatus.FAILED,
        idempotency_key=f"trace-{uuid4()}",
        instagram_caption="private caption",
    )
    session.add(publication)
    session.flush()
    session.add(SocialPublicationAttempt(
        social_publication_id=publication.id,
        attempt_no=1,
        status=SocialPublicationAttemptStatus.FAILED,
        error_code="PROVIDER_5XX",
        error_message="provider rejected oauth-secret",
        metadata_={"access_token": "oauth-secret"},
    ))
    session.commit()

    # The production trace dependency is fail-closed without a bearer token.
    main.app.dependency_overrides.pop(main.get_current_user, None)
    assert client.get(f"/workflow-executions/{execution.id}/trace").status_code == 401
    main.app.dependency_overrides[main.get_current_user] = lambda: user
    main.app.dependency_overrides[main.get_trace_authenticated_user_id] = lambda: user.id
    response = client.get(f"/workflow-executions/{execution.id}/trace")
    assert response.status_code == 200
    body = response.json()
    assert body["workflow"]["workflow_version"] == 3
    assert body["rules_version"] == {"id": rules.id, "version": 999}
    assert [item["attempt_no"] for item in body["nodes"][0]["attempts"]] == [1, 2]
    assert body["nodes"][0]["attempts"][0]["prompt_version"] == {
        "id": prompt.id, "node_type": "CONTENT_PLAN", "version": 999
    }
    assert body["nodes"][0]["attempts"][1]["input_tokens"] is None
    assert "private input" not in response.text and "secret prompt" not in response.text
    assert "model unavailable" not in response.text
    assert "private revision" not in response.text
    assert "private caption" not in response.text
    assert "oauth-secret" not in response.text
    assert body["revisions"][0]["start_node_type"] == "CONTENT_PLAN"
    assert "request_text" not in body["revisions"][0]
    publication_attempt = body["publications"][0]["attempts"][0]
    assert publication_attempt["error_code"] == "PROVIDER_5XX"
    assert publication_attempt["error_message"] == "Publication failed; see error_code."
    assert client.get("/workflow-executions/999999999/trace").status_code == 404
    other = User(email=f"{uuid4()}@example.com", name="other trace user")
    session.add(other)
    session.flush()
    other_execution = WorkflowExecution(user_id=other.id, workflow_key="vora_content_creation", workflow_version=3)
    session.add(other_execution)
    session.commit()
    assert client.get(f"/workflow-executions/{other_execution.id}/trace").status_code == 404
    assert session.get(NodeExecution, node.id).status is NodeExecutionStatus.FAILED


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HTTPError("https://provider.test", 429, "rate", None, None), "RATE_LIMIT"),
        (HTTPError("https://provider.test", 400, "bad request", None, None), "PROVIDER_4XX"),
        (HTTPError("https://provider.test", 503, "down", None, None), "PROVIDER_5XX"),
        (URLError("offline"), "NETWORK_ERROR"),
        (ConnectionError("offline"), "NETWORK_ERROR"),
        (TimeoutError("slow"), "TIMEOUT"),
        (NodeExecutorConfigurationError("missing"), "CONFIGURATION_ERROR"),
        (ValueError("invalid"), "VALIDATION_ERROR"),
    ],
)
def test_error_code_classification(error: Exception, expected: str) -> None:
    assert WorkflowEngine._error_code(error) == expected
