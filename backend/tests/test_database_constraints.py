from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import engine
from app.models import (
    ApprovalDecision,
    AssetType,
    ExecutionInputSnapshot,
    FileAsset,
    InputType,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    SocialAccountConnection,
    SocialPlatform,
    SocialPublication,
    User,
    UserApproval,
    WorkflowExecution,
)


@pytest.fixture
def session() -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    db_session = Session(bind=connection)
    try:
        yield db_session
    finally:
        db_session.close()
        transaction.rollback()
        connection.close()


def flush_fails(session: Session, value: object) -> None:
    savepoint = session.begin_nested()
    session.add(value)
    with pytest.raises(IntegrityError):
        session.flush()
    savepoint.rollback()


def create_user(session: Session) -> User:
    user = User(email=f"{uuid4()}@example.com", name="tester")
    session.add(user)
    session.flush()
    return user


def create_workflow(session: Session, user: User) -> WorkflowExecution:
    workflow = WorkflowExecution(user_id=user.id, workflow_key="video", workflow_version=1)
    session.add(workflow)
    session.flush()
    return workflow


def create_node(session: Session, workflow: WorkflowExecution, version: int = 1) -> NodeExecution:
    node = NodeExecution(
        workflow_execution_id=workflow.id,
        node_key="SCRIPT_GENERATION",
        user_requested_version=version,
        input_data={"topic": "VORA"},
    )
    session.add(node)
    session.flush()
    return node


def test_user_email_is_unique(session: Session) -> None:
    user = create_user(session)
    flush_fails(session, User(email=user.email, name="duplicate"))


def test_workflow_has_only_one_snapshot_and_jsonb_round_trips(session: Session) -> None:
    workflow = create_workflow(session, create_user(session))
    snapshot = ExecutionInputSnapshot(
        workflow_execution_id=workflow.id,
        input_type=InputType.TEXT,
        request_text="request",
        input_data={"nested": {"value": 1}},
    )
    session.add(snapshot)
    session.flush()
    session.expire(snapshot)
    assert snapshot.input_data == {"nested": {"value": 1}}
    flush_fails(
        session,
        ExecutionInputSnapshot(
            workflow_execution_id=workflow.id,
            input_type=InputType.IMAGE,
            input_data={},
        ),
    )


def test_node_versions_are_unique_but_distinct_versions_are_allowed(session: Session) -> None:
    workflow = create_workflow(session, create_user(session))
    create_node(session, workflow, version=1)
    flush_fails(
        session,
        NodeExecution(
            workflow_execution_id=workflow.id,
            node_key="SCRIPT_GENERATION",
            user_requested_version=1,
            input_data={},
        ),
    )
    create_node(session, workflow, version=2)


def test_attempt_numbers_are_unique_but_distinct_attempts_are_allowed(session: Session) -> None:
    node = create_node(session, create_workflow(session, create_user(session)))
    session.add(
        NodeExecutionAttempt(
            node_execution_id=node.id,
            attempt_no=1,
            status=NodeExecutionAttemptStatus.FAILED,
            metadata_={},
        )
    )
    session.flush()
    flush_fails(
        session,
        NodeExecutionAttempt(
            node_execution_id=node.id,
            attempt_no=1,
            status=NodeExecutionAttemptStatus.RUNNING,
            metadata_={},
        ),
    )
    session.add(
        NodeExecutionAttempt(
            node_execution_id=node.id,
            attempt_no=2,
            status=NodeExecutionAttemptStatus.SUCCESS,
            metadata_={},
        )
    )
    session.flush()


def test_approval_is_unique_and_revision_text_is_required(session: Session) -> None:
    user = create_user(session)
    node = create_node(session, create_workflow(session, user))
    session.add(
        UserApproval(node_execution_id=node.id, user_id=user.id, decision=ApprovalDecision.APPROVED)
    )
    session.flush()
    flush_fails(
        session,
        UserApproval(node_execution_id=node.id, user_id=user.id, decision=ApprovalDecision.APPROVED),
    )

    second_node = create_node(session, node_workflow := create_workflow(session, user))
    assert second_node.workflow_execution_id == node_workflow.id
    flush_fails(
        session,
        UserApproval(
            node_execution_id=second_node.id,
            user_id=user.id,
            decision=ApprovalDecision.REVISION_REQUESTED,
        ),
    )
    revision_node = create_node(session, create_workflow(session, user))
    revision_approval = UserApproval(
        node_execution_id=revision_node.id,
        user_id=user.id,
        decision=ApprovalDecision.REVISION_REQUESTED,
        revision_request="Make the ending more concise.",
    )
    session.add(revision_approval)
    session.flush()
    assert revision_approval.id is not None


@pytest.mark.parametrize("revision_request", ["", "   "])
def test_revision_request_must_not_be_blank(session: Session, revision_request: str) -> None:
    user = create_user(session)
    node = create_node(session, create_workflow(session, user))
    flush_fails(
        session,
        UserApproval(
            node_execution_id=node.id,
            user_id=user.id,
            decision=ApprovalDecision.REVISION_REQUESTED,
            revision_request=revision_request,
        ),
    )


def test_social_connection_identity_is_unique(session: Session) -> None:
    user = create_user(session)
    connection = SocialAccountConnection(
        user_id=user.id,
        platform=SocialPlatform.YOUTUBE,
        external_account_id="channel-1",
        access_token="secret",
    )
    session.add(connection)
    session.flush()
    flush_fails(
        session,
        SocialAccountConnection(
            user_id=user.id,
            platform=SocialPlatform.YOUTUBE,
            external_account_id="channel-1",
            access_token="another-secret",
        ),
    )


@pytest.mark.parametrize("with_snapshot,with_node", [(False, False), (True, True)])
def test_file_asset_requires_exactly_one_source(
    session: Session, with_snapshot: bool, with_node: bool
) -> None:
    workflow = create_workflow(session, create_user(session))
    snapshot = ExecutionInputSnapshot(
        workflow_execution_id=workflow.id, input_type=InputType.IMAGE, input_data={}
    )
    node = create_node(session, workflow)
    session.add(snapshot)
    session.flush()
    flush_fails(
        session,
        FileAsset(
            workflow_execution_id=workflow.id,
            execution_input_snapshot_id=snapshot.id if with_snapshot else None,
            node_execution_id=node.id if with_node else None,
            asset_type=AssetType.IMAGE,
            storage_key=str(uuid4()),
            file_name="image.png",
            mime_type="image/png",
            file_size=1,
        ),
    )


def test_file_asset_accepts_each_valid_single_source(session: Session) -> None:
    workflow = create_workflow(session, create_user(session))
    snapshot = ExecutionInputSnapshot(
        workflow_execution_id=workflow.id, input_type=InputType.IMAGE, input_data={}
    )
    node = create_node(session, workflow)
    session.add(snapshot)
    session.flush()
    input_asset = FileAsset(
        workflow_execution_id=workflow.id,
        execution_input_snapshot_id=snapshot.id,
        asset_type=AssetType.IMAGE,
        storage_key=str(uuid4()),
        file_name="input.png",
        mime_type="image/png",
        file_size=1,
    )
    output_asset = FileAsset(
        workflow_execution_id=workflow.id,
        node_execution_id=node.id,
        asset_type=AssetType.VIDEO,
        storage_key=str(uuid4()),
        file_name="output.mp4",
        mime_type="video/mp4",
        file_size=1,
    )
    session.add_all([input_asset, output_asset])
    session.flush()
    assert input_asset.id is not None
    assert output_asset.id is not None


def test_publication_idempotency_key_is_unique(session: Session) -> None:
    user = create_user(session)
    workflow = create_workflow(session, user)
    node = create_node(session, workflow)
    asset = FileAsset(
        workflow_execution_id=workflow.id,
        node_execution_id=node.id,
        asset_type=AssetType.VIDEO,
        storage_key=str(uuid4()),
        file_name="video.mp4",
        mime_type="video/mp4",
        file_size=1,
    )
    account = SocialAccountConnection(
        user_id=user.id,
        platform=SocialPlatform.INSTAGRAM,
        external_account_id="account-1",
        access_token="secret",
    )
    session.add_all([asset, account])
    session.flush()
    session.add(
        SocialPublication(
            social_account_connection_id=account.id,
            file_asset_id=asset.id,
            idempotency_key="publish-once",
        )
    )
    session.flush()
    flush_fails(
        session,
        SocialPublication(
            social_account_connection_id=account.id,
            file_asset_id=asset.id,
            idempotency_key="publish-once",
        ),
    )
