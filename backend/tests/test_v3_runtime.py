from collections.abc import Iterator
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableLambda
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import engine
from app.models import (
    AssetType,
    ExecutionInputSnapshot,
    FileAsset,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    NodeExecutionStatus,
    PromptVersion,
    RulesVersion,
    User,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.v3_runtime import ContentPlanOutput, TextGenerateOutput, V3NodeExecutor
from app.workflow_definitions import workflow_registry
from app.workflow_engine import WorkflowEngine


@pytest.fixture
def session() -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _execution(session: Session) -> WorkflowExecution:
    user = User(email=f"{uuid4()}@example.com", name="V3 runtime tester")
    session.add(user)
    session.flush()
    execution = WorkflowExecution(
        user_id=user.id,
        workflow_key="vora_content_creation",
        workflow_version=3,
    )
    session.add(execution)
    session.commit()
    return execution


PROMPT_TEMPLATE = (
    "Request: {request_text}\nMarketing: {marketing_info}\nTheme: {theme}\n"
    "Rules: {rules_data}\nOriginal input: {input_data}"
)
TEXT_GENERATE_PROMPT_TEMPLATE = (
    "Message: {core_message}\nSlides: {slide_structure}\n"
    "Rules: {rules_data}\nOriginal input: {input_data}"
)


def _content_plan_model(_: object) -> ContentPlanOutput:
    return ContentPlanOutput(
        core_message="A focused message",
        slide_structure=["hook", "supporting point"],
    )


def _failing_content_plan_model(_: object) -> ContentPlanOutput:
    raise RuntimeError("CONTENT_PLAN model unavailable")


def _text_generate_model(_: object) -> TextGenerateOutput:
    return TextGenerateOutput(
        slides=[
            {"slide_no": 1, "text": "Headline"},
            {"slide_no": 2, "text": "Supporting copy"},
        ]
    )


def _failing_text_generate_model(_: object) -> TextGenerateOutput:
    raise RuntimeError("TEXT_GENERATE model unavailable")


def _v3_input_contract(session: Session, execution: WorkflowExecution) -> None:
    rules = RulesVersion(version=1, rules_data={"tone": "clear"})
    content_plan_prompt = PromptVersion(
        node_type="CONTENT_PLAN", version=1, prompt_template=PROMPT_TEMPLATE
    )
    text_generate_prompt = PromptVersion(
        node_type="TEXT_GENERATE", version=1, prompt_template=TEXT_GENERATE_PROMPT_TEMPLATE
    )
    session.add_all([rules, content_plan_prompt, text_generate_prompt])
    session.flush()
    source_asset = FileAsset(
        workflow_execution_id=execution.id,
        created_by_node_execution_id=None,
        asset_type=AssetType.IMAGE,
        storage_key=f"tests/{uuid4()}.png",
        file_name="input.png",
        mime_type="image/png",
        file_size=1,
        slide_no=None,
    )
    session.add(source_asset)
    session.flush()
    session.add(
        ExecutionInputSnapshot(
            workflow_execution_id=execution.id,
            request_text="Plan a launch",
            marketing_info="Product details",
            theme="Minimal",
            source_asset_id=source_asset.id,
            rules_version_id=rules.id,
        )
    )
    session.commit()


def test_v3_chain_reaches_human_approval_with_runnable_attempts(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            content_plan_model=RunnableLambda(_content_plan_model),
            content_plan_model_name="test-content-plan-model",
            text_generate_model=RunnableLambda(_text_generate_model),
            text_generate_model_name="test-text-generate-model",
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "fake"})

    assert status is WorkflowExecutionStatus.WAITING_APPROVAL
    session.refresh(execution)
    assert execution.current_node_type == "HUMAN_APPROVAL"
    nodes = session.scalars(
            select(NodeExecution)
            .where(NodeExecution.workflow_execution_id == execution.id)
            .order_by(NodeExecution.id)
    ).all()
    assert [(node.node_type, node.status, node.revision_no) for node in nodes] == [
            ("CONTENT_PLAN", NodeExecutionStatus.SUCCEEDED, 0),
            ("TEXT_GENERATE", NodeExecutionStatus.SUCCEEDED, 0),
            ("TEXT_CHECK", NodeExecutionStatus.SUCCEEDED, 0),
            ("IMAGE_GENERATE", NodeExecutionStatus.SUCCEEDED, 0),
            ("IMAGE_CHECK", NodeExecutionStatus.SUCCEEDED, 0),
            ("HUMAN_APPROVAL", NodeExecutionStatus.WAITING, 0),
        ]
    attempts = session.scalars(
            select(NodeExecutionAttempt)
            .join(NodeExecution)
            .where(NodeExecution.workflow_execution_id == execution.id)
            .order_by(NodeExecutionAttempt.id)
    ).all()
    assert len(attempts) == 5
    assert all(attempt.attempt_no == 1 for attempt in attempts)
    assert all(attempt.status is NodeExecutionAttemptStatus.SUCCEEDED for attempt in attempts)
    assert attempts[0].prompt_version_id is not None
    assert attempts[0].ai_company == "OPENAI"
    assert attempts[0].model == "test-content-plan-model"
    assert attempts[1].prompt_version_id is not None
    assert attempts[1].ai_company == "OPENAI"
    assert attempts[1].model == "test-text-generate-model"


def test_v3_chain_failure_marks_attempt_node_and_workflow_failed(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
            session,
            workflow_registry,
            V3NodeExecutor(
                session=session,
                fail_node="TEXT_GENERATE",
                content_plan_model=RunnableLambda(_content_plan_model),
            ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "fake"})

    assert status is WorkflowExecutionStatus.FAILED
    session.refresh(execution)
    assert execution.current_node_type is None
    failed_node = session.scalar(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_type == "TEXT_GENERATE",
            )
    )
    assert failed_node is not None
    assert failed_node.status is NodeExecutionStatus.FAILED
    attempt = session.scalar(
            select(NodeExecutionAttempt).where(
                NodeExecutionAttempt.node_execution_id == failed_node.id
            )
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.FAILED


def test_v3_content_plan_uses_prompt_version_and_persists_structured_output(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            fail_node="TEXT_GENERATE",
            content_plan_model=RunnableLambda(_content_plan_model),
            content_plan_model_name="test-content-plan-model",
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    content_plan = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "CONTENT_PLAN",
        )
    )
    assert content_plan is not None
    assert content_plan.output_data == {
        "core_message": "A focused message",
        "slide_structure": ["hook", "supporting point"],
        "slides": [
            {"slide_no": 1, "purpose": "hook"},
            {"slide_no": 2, "purpose": "supporting point"},
        ],
    }
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == content_plan.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.SUCCEEDED
    assert attempt.prompt_version_id is not None
    assert attempt.metadata_ == {"runtime": "v3-langchain", "prompt_version": 1}


def test_v3_content_plan_model_failure_marks_attempt_node_and_workflow_failed(
    session: Session,
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(session=session, content_plan_model=RunnableLambda(_failing_content_plan_model)),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    content_plan = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "CONTENT_PLAN",
        )
    )
    assert content_plan is not None
    assert content_plan.status is NodeExecutionStatus.FAILED
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == content_plan.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.FAILED


def test_v3_text_generate_uses_prompt_version_and_persists_structured_output(
    session: Session,
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            fail_node="TEXT_CHECK",
            content_plan_model=RunnableLambda(_content_plan_model),
            text_generate_model=RunnableLambda(_text_generate_model),
            text_generate_model_name="test-text-generate-model",
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    text_generate = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "TEXT_GENERATE",
        )
    )
    assert text_generate is not None
    assert text_generate.output_data == {
        "slides": [
            {"slide_no": 1, "text": "Headline"},
            {"slide_no": 2, "text": "Supporting copy"},
        ]
    }
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == text_generate.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.SUCCEEDED
    assert attempt.prompt_version_id is not None
    assert attempt.ai_company == "OPENAI"
    assert attempt.model == "test-text-generate-model"
    assert attempt.metadata_ == {"runtime": "v3-langchain", "prompt_version": 1}


def test_v3_text_generate_model_failure_marks_attempt_node_and_workflow_failed(
    session: Session,
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            content_plan_model=RunnableLambda(_content_plan_model),
            text_generate_model=RunnableLambda(_failing_text_generate_model),
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    text_generate = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "TEXT_GENERATE",
        )
    )
    assert text_generate is not None
    assert text_generate.status is NodeExecutionStatus.FAILED
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == text_generate.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.FAILED
