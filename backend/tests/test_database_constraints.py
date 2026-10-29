from collections.abc import Iterator
from typing import cast
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
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    RulesVersion,
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


def create_node(session: Session, workflow: WorkflowExecution, version: int = 0) -> NodeExecution:
    node = NodeExecution(
        workflow_execution_id=workflow.id,
        node_type="SCRIPT_GENERATION",
        revision_no=version,
        input_data={"topic": "VORA"},
    )
    session.add(node)
    session.flush()
    return node


def create_snapshot_dependencies(
    session: Session, workflow: WorkflowExecution
) -> tuple[RulesVersion, FileAsset]:
    rules = RulesVersion(version=1, rules_data={})
    source = FileAsset(
        workflow_execution_id=workflow.id,
        asset_type=AssetType.IMAGE,
        storage_key=str(uuid4()),
        file_name="source.png",
        mime_type="image/png",
        file_size=1,
    )
    session.add_all([rules, source])
    session.flush()
    return rules, source


def create_snapshot(session: Session, workflow: WorkflowExecution) -> ExecutionInputSnapshot:
    rules, source = create_snapshot_dependencies(session, workflow)
    snapshot = ExecutionInputSnapshot(
        workflow_execution_id=workflow.id,
        request_text="request",
        marketing_info="product information",
        theme=None,
        source_asset_id=source.id,
        rules_version_id=rules.id,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def test_user_email_is_optional_and_not_an_identity_key(session: Session) -> None:
    user = create_user(session)
    workflow = create_workflow(session, user)
    original_user_id = user.id
    duplicate = User(email=user.email, name="duplicate")
    without_email = User(email=None, name="without email")
    session.add_all([duplicate, without_email])
    session.flush()
    assert duplicate.id != original_user_id
    assert without_email.id != original_user_id
    assert workflow.user_id == original_user_id


def test_workflow_has_only_one_snapshot_and_jsonb_round_trips(session: Session) -> None:
    workflow = create_workflow(session, create_user(session))
    snapshot = create_snapshot(session, workflow)
    assert snapshot.marketing_info == "product information"
    assert snapshot.source_asset_id is not None
    flush_fails(
        session,
        ExecutionInputSnapshot(
            workflow_execution_id=workflow.id,
            request_text="image request",
            marketing_info="other product",
            source_asset_id=snapshot.source_asset_id,
            rules_version_id=snapshot.rules_version_id,
        ),
    )


@pytest.mark.parametrize("request_text", [None, "", "   "])
def test_snapshot_request_text_must_not_be_null_or_blank(
    session: Session, request_text: str | None
) -> None:
    workflow = create_workflow(session, create_user(session))
    rules, source = create_snapshot_dependencies(session, workflow)
    flush_fails(
        session,
        ExecutionInputSnapshot(
            workflow_execution_id=workflow.id,
            request_text=cast(str, request_text),
            marketing_info="product information",
            source_asset_id=source.id,
            rules_version_id=rules.id,
        ),
    )


def test_node_versions_are_unique_but_distinct_versions_are_allowed(session: Session) -> None:
    workflow = create_workflow(session, create_user(session))
    create_node(session, workflow, version=1)
    flush_fails(
        session,
        NodeExecution(
            workflow_execution_id=workflow.id,
            node_type="SCRIPT_GENERATION",
            revision_no=1,
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
            status=NodeExecutionAttemptStatus.SUCCEEDED,
            metadata_={},
        )
    )
    session.flush()


def test_approval_is_unique_and_workflow_scoped(session: Session) -> None:
    user = create_user(session)
    node = create_node(session, create_workflow(session, user))
    session.add(
        UserApproval(
            workflow_execution_id=node.workflow_execution_id,
            node_execution_id=node.id,
            decision=ApprovalDecision.APPROVED,
        )
    )
    session.flush()
    flush_fails(
        session,
        UserApproval(
            workflow_execution_id=node.workflow_execution_id,
            node_execution_id=node.id,
            decision=ApprovalDecision.APPROVED,
        ),
    )

    second_node = create_node(session, node_workflow := create_workflow(session, user))
    assert second_node.workflow_execution_id == node_workflow.id
    second_approval = UserApproval(
        workflow_execution_id=second_node.workflow_execution_id,
        node_execution_id=second_node.id,
        decision=ApprovalDecision.REJECTED,
    )
    session.add(second_approval)
    session.flush()
    assert second_approval.id is not None


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


def test_file_asset_source_image_allows_null_producer(session: Session) -> None:
    workflow = create_workflow(session, create_user(session))
    source = FileAsset(
        workflow_execution_id=workflow.id,
        created_by_node_execution_id=None,
        asset_type=AssetType.IMAGE,
        storage_key=str(uuid4()),
        file_name="image.png",
        mime_type="image/png",
        file_size=1,
        slide_no=None,
    )
    session.add(source)
    session.flush()
    assert source.id is not None


def test_file_asset_accepts_each_valid_single_source(session: Session) -> None:
    workflow = create_workflow(session, create_user(session))
    node = create_node(session, workflow)
    output_asset = FileAsset(
        workflow_execution_id=workflow.id,
        created_by_node_execution_id=node.id,
        asset_type=AssetType.VIDEO,
        storage_key=str(uuid4()),
        file_name="output.mp4",
        mime_type="video/mp4",
        file_size=1,
    )
    session.add(output_asset)
    session.flush()
    assert output_asset.id is not None


def test_asset_slide_no_must_be_positive(session: Session) -> None:
    workflow = create_workflow(session, create_user(session))
    flush_fails(
        session,
        FileAsset(
            workflow_execution_id=workflow.id,
            asset_type=AssetType.IMAGE,
            storage_key=str(uuid4()),
            file_name="invalid.png",
            mime_type="image/png",
            file_size=1,
            slide_no=0,
        ),
    )
    first_asset = FileAsset(
        workflow_execution_id=workflow.id,
        asset_type=AssetType.IMAGE,
        storage_key=str(uuid4()),
        file_name="first.png",
        mime_type="image/png",
        file_size=1,
        slide_no=1,
    )
    second_asset = FileAsset(
        workflow_execution_id=workflow.id,
        asset_type=AssetType.IMAGE,
        storage_key=str(uuid4()),
        file_name="duplicate.png",
        mime_type="image/png",
        file_size=1,
        slide_no=1,
    )
    session.add_all([first_asset, second_asset])
    session.flush()
    assert first_asset.slide_no == second_asset.slide_no == 1


def test_publication_idempotency_key_is_unique(session: Session) -> None:
    user = create_user(session)
    workflow = create_workflow(session, user)
    node = create_node(session, workflow)
    asset = FileAsset(
        workflow_execution_id=workflow.id,
        created_by_node_execution_id=node.id,
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
            node_execution_id=node.id,
            social_account_connection_id=account.id,
            idempotency_key="publish-once",
        )
    )
    session.flush()
    flush_fails(
        session,
        SocialPublication(
            node_execution_id=node.id,
            social_account_connection_id=account.id,
            idempotency_key="publish-once",
        ),
    )


def test_publication_node_allows_multiple_platform_intents(session: Session) -> None:
    user = create_user(session)
    workflow = create_workflow(session, user)
    node = create_node(session, workflow)
    first_account = SocialAccountConnection(
        user_id=user.id,
        platform=SocialPlatform.INSTAGRAM,
        external_account_id="account-1",
        access_token="secret",
    )
    second_account = SocialAccountConnection(
        user_id=user.id,
        platform=SocialPlatform.YOUTUBE,
        external_account_id="account-2",
        access_token="secret",
    )
    session.add_all([first_account, second_account])
    session.flush()
    session.add_all(
        [
            SocialPublication(
                node_execution_id=node.id,
                social_account_connection_id=first_account.id,
                idempotency_key="instagram-intent",
            ),
            SocialPublication(
                node_execution_id=node.id,
                social_account_connection_id=second_account.id,
                idempotency_key="youtube-intent",
            ),
        ]
    )
    session.flush()
