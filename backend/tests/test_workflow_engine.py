from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import engine
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
from app.node_executors import FakeNodeExecutor
from app.workflow_definitions import VORA_CONTENT_CREATION_V1, workflow_registry
from app.workflow_engine import WorkflowEngine


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


def create_execution(session: Session) -> WorkflowExecution:
    user = User(email=f"{uuid4()}@example.com", name="workflow tester")
    session.add(user)
    session.flush()
    execution = WorkflowExecution(
        user_id=user.id,
        workflow_key="vora_content_creation",
        workflow_version=1,
    )
    session.add(execution)
    session.commit()
    return execution


def approve_waiting_node(session: Session, execution: WorkflowExecution) -> NodeExecution:
    waiting_node = session.scalars(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.status == NodeExecutionStatus.WAITING_APPROVAL,
        )
    ).one()
    session.add(
        UserApproval(
            node_execution_id=waiting_node.id,
            user_id=execution.user_id,
            decision=ApprovalDecision.APPROVED,
        )
    )
    session.commit()
    return waiting_node


def test_registry_finds_vora_workflow_by_key_and_version() -> None:
    definition = workflow_registry.get("vora_content_creation", 1)

    assert definition is VORA_CONTENT_CREATION_V1
    assert [node.key for node in definition.nodes] == [
        "input_analysis",
        "content_planning",
        "script_generation",
        "video_generation",
    ]


def test_workflow_runs_stops_and_resumes_through_final_approval(session: Session) -> None:
    execution = create_execution(session)
    executor = FakeNodeExecutor()
    workflow_engine = WorkflowEngine(session, workflow_registry, executor)

    status = workflow_engine.start_execution(execution.id, {"topic": "VORA"})

    assert status is WorkflowExecutionStatus.WAITING_APPROVAL
    assert executor.executed_node_keys == ["input_analysis", "content_planning"]
    nodes = session.scalars(
        select(NodeExecution)
        .where(NodeExecution.workflow_execution_id == execution.id)
        .order_by(NodeExecution.id)
    ).all()
    assert [node.status for node in nodes] == [
        NodeExecutionStatus.SUCCESS,
        NodeExecutionStatus.WAITING_APPROVAL,
    ]
    content_planning = nodes[1]
    content_planning_attempt = session.scalars(
        select(NodeExecutionAttempt).where(
            NodeExecutionAttempt.node_execution_id == content_planning.id
        )
    ).one()
    assert content_planning.node_key == "content_planning"
    assert content_planning.status is NodeExecutionStatus.WAITING_APPROVAL
    assert content_planning_attempt.status is NodeExecutionAttemptStatus.SUCCESS
    assert content_planning_attempt.attempt_no == 1
    assert content_planning.output_data == {
        "result": "fake content_planning result",
        "input": {"result": "fake input_analysis result", "input": {"topic": "VORA"}},
    }

    approve_waiting_node(session, execution)
    assert workflow_engine.resume_after_approval(execution.id) is WorkflowExecutionStatus.WAITING_APPROVAL
    assert executor.executed_node_keys[-1] == "script_generation"

    approve_waiting_node(session, execution)
    assert workflow_engine.resume_after_approval(execution.id) is WorkflowExecutionStatus.WAITING_APPROVAL
    assert executor.executed_node_keys[-1] == "video_generation"

    approve_waiting_node(session, execution)
    assert workflow_engine.resume_after_approval(execution.id) is WorkflowExecutionStatus.SUCCESS
    session.refresh(execution)
    assert execution.status is WorkflowExecutionStatus.SUCCESS
    assert session.scalars(
        select(NodeExecution).where(NodeExecution.workflow_execution_id == execution.id)
    ).all()
    assert all(
        node.status is NodeExecutionStatus.SUCCESS
        for node in session.scalars(
            select(NodeExecution).where(NodeExecution.workflow_execution_id == execution.id)
        )
    )

    attempts = session.scalars(
        select(NodeExecutionAttempt)
        .join(NodeExecution)
        .where(NodeExecution.workflow_execution_id == execution.id)
    ).all()
    assert len(attempts) == 4
    assert all(attempt.attempt_no == 1 for attempt in attempts)
    assert all(attempt.status is NodeExecutionAttemptStatus.SUCCESS for attempt in attempts)


def test_executor_error_marks_node_attempt_and_workflow_failed(session: Session) -> None:
    execution = create_execution(session)
    executor = FakeNodeExecutor(failing_node_key="input_analysis")
    workflow_engine = WorkflowEngine(session, workflow_registry, executor)

    assert workflow_engine.start_execution(execution.id) is WorkflowExecutionStatus.FAILED

    node = session.scalars(
        select(NodeExecution).where(NodeExecution.workflow_execution_id == execution.id)
    ).one()
    attempt = session.scalars(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == node.id)
    ).one()
    session.refresh(execution)
    assert node.status is NodeExecutionStatus.FAILED
    assert attempt.status is NodeExecutionAttemptStatus.FAILED
    assert attempt.attempt_no == 1
    assert attempt.error_message == "Fake executor failure for input_analysis"
    assert execution.status is WorkflowExecutionStatus.FAILED


def test_resume_requires_persisted_approval(session: Session) -> None:
    execution = create_execution(session)
    workflow_engine = WorkflowEngine(session, workflow_registry, FakeNodeExecutor())
    workflow_engine.start_execution(execution.id)

    with pytest.raises(ValueError, match="has not been approved"):
        workflow_engine.resume_after_approval(execution.id)


def test_revision_reruns_target_with_new_version_and_reuses_upstream_success(
    session: Session,
) -> None:
    execution = create_execution(session)
    executor = FakeNodeExecutor()
    workflow_engine = WorkflowEngine(session, workflow_registry, executor)

    workflow_engine.start_execution(execution.id, {"topic": "VORA"})
    approve_waiting_node(session, execution)
    workflow_engine.resume_after_approval(execution.id)

    assert workflow_engine.start_revision(
        execution.id,
        "script_generation",
        "첫 문장을 더 강하게 바꿔줘",
    ) is WorkflowExecutionStatus.WAITING_APPROVAL

    nodes = session.scalars(
        select(NodeExecution)
        .where(NodeExecution.workflow_execution_id == execution.id)
        .order_by(NodeExecution.id)
    ).all()
    assert [node.node_key for node in nodes] == [
        "input_analysis",
        "content_planning",
        "script_generation",
        "script_generation",
    ]
    assert [node.user_requested_version for node in nodes] == [1, 1, 1, 2]
    assert nodes[0].status is NodeExecutionStatus.SUCCESS
    assert nodes[1].status is NodeExecutionStatus.SUCCESS
    assert nodes[2].status is NodeExecutionStatus.WAITING_APPROVAL
    assert nodes[3].status is NodeExecutionStatus.WAITING_APPROVAL
    assert nodes[3].input_data["revision_request"] == "첫 문장을 더 강하게 바꿔줘"
    assert nodes[3].input_data["previous_output"] == nodes[2].output_data
    assert executor.executed_node_keys == [
        "input_analysis",
        "content_planning",
        "script_generation",
        "script_generation",
    ]

    # The latest version is the only current approval target; v1 remains immutable history.
    session.add(
        UserApproval(
            node_execution_id=nodes[3].id,
            user_id=execution.user_id,
            decision=ApprovalDecision.APPROVED,
        )
    )
    session.commit()
    workflow_engine.resume_after_approval(execution.id)
    video = session.scalars(
        select(NodeExecution)
        .where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_key == "video_generation",
        )
    ).one()
    assert video.user_requested_version == 2
