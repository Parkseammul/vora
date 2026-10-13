from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import engine
from app.models import (
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    NodeExecutionStatus,
    User,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.v3_runtime import V3NodeExecutor
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


def test_v3_chain_reaches_human_approval_with_runnable_attempts(session: Session) -> None:
    execution = _execution(session)
    engine_instance = WorkflowEngine(
        session, workflow_registry, V3NodeExecutor(session=session)
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


def test_v3_chain_failure_marks_attempt_node_and_workflow_failed(session: Session) -> None:
    execution = _execution(session)
    engine_instance = WorkflowEngine(
            session,
            workflow_registry,
            V3NodeExecutor(fail_node="TEXT_GENERATE"),
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
