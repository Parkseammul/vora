from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import main
from app.database import engine
from app.models import (
    AssetType,
    FileAsset,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    NodeExecutionStatus,
    User,
    WorkflowExecution,
    WorkflowExecutionStatus,
)


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

    previous = main.app.dependency_overrides.get(main.get_session)
    main.app.dependency_overrides[main.get_session] = get_test_session
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        if previous is None:
            main.app.dependency_overrides.pop(main.get_session, None)
        else:
            main.app.dependency_overrides[main.get_session] = previous


def _seed_v3(session: Session) -> tuple[WorkflowExecution, User, list[FileAsset]]:
    # The owner is inserted last because conftest's authenticated-user override
    # deliberately selects the newest test user.
    other_user = User(email=f"{uuid4()}@example.com", name="other")
    owner = User(email=f"{uuid4()}@example.com", name="owner")
    session.add_all([other_user, owner])
    session.flush()
    execution = WorkflowExecution(
        user_id=owner.id,
        workflow_key="vora_content_creation",
        workflow_version=3,
        status=WorkflowExecutionStatus.WAITING_APPROVAL,
        current_node_type="HUMAN_APPROVAL",
    )
    session.add(execution)
    session.flush()

    # A complete old revision is intentionally retained to prove that reads do
    # not mix its images or approval target into the current revision.
    old_image = NodeExecution(
        workflow_execution_id=execution.id,
        node_type="IMAGE_GENERATE",
        revision_no=0,
        status=NodeExecutionStatus.SUCCEEDED,
        output_data={"asset_ids": []},
    )
    session.add(old_image)
    session.flush()
    session.add(
        FileAsset(
            workflow_execution_id=execution.id,
            created_by_node_execution_id=old_image.id,
            asset_type=AssetType.IMAGE,
            storage_key=f"old/{uuid4()}.png",
            file_name="old-slide.png",
            mime_type="image/png",
            file_size=3,
            slide_no=1,
        )
    )

    nodes: dict[str, NodeExecution] = {}
    for node_type in main.V3_NODE_KEYS:
        node = NodeExecution(
            workflow_execution_id=execution.id,
            node_type=node_type,
            revision_no=1,
            status=(
                NodeExecutionStatus.WAITING
                if node_type == "HUMAN_APPROVAL"
                else NodeExecutionStatus.SUCCEEDED
            ),
            output_data=(
                {"asset_ids": []}
                if node_type == "IMAGE_GENERATE"
                else {"passed": True, "prompt": "private"}
            ),
        )
        nodes[node_type] = node
        session.add(node)
    session.flush()
    image_node = nodes["IMAGE_GENERATE"]
    session.add_all(
        [
            NodeExecutionAttempt(
                node_execution_id=image_node.id,
                attempt_no=1,
                status=NodeExecutionAttemptStatus.FAILED,
            ),
            NodeExecutionAttempt(
                node_execution_id=image_node.id,
                attempt_no=2,
                status=NodeExecutionAttemptStatus.SUCCEEDED,
            ),
        ]
    )
    session.flush()
    assets: list[FileAsset] = []
    for slide_no in range(1, 6):
        asset = FileAsset(
            workflow_execution_id=execution.id,
            created_by_node_execution_id=image_node.id,
            asset_type=AssetType.IMAGE,
            storage_key=f"v3/{uuid4()}.png",
            file_name=f"slide-{slide_no}.png",
            mime_type="image/png",
            file_size=3,
            slide_no=slide_no,
        )
        assets.append(asset)
        session.add(asset)
    session.flush()
    image_node.output_data = {"asset_ids": [asset.id for asset in assets], "storage_key": "private"}
    return execution, owner, assets


def test_v3_detail_selects_current_revision_and_exposes_seven_nodes(session: Session) -> None:
    execution, _, assets = _seed_v3(session)
    response = main._v3_workflow_response(session, execution)

    assert response["workflow_version"] == 3
    assert list(response["nodes"]) == list(main.V3_NODE_KEYS)
    assert response["current_revision"] == 1
    assert response["human_approval"] == {
        "waiting": True,
        "node_execution_id": response["nodes"]["HUMAN_APPROVAL"]["id"],
        "status": "WAITING",
    }
    assert [image["slide_no"] for image in response["images"]] == [1, 2, 3, 4, 5]
    assert response["images"][0]["file_name"] == assets[0].file_name
    assert response["nodes"]["IMAGE_GENERATE"]["attempt"]["current"] == 2
    assert "storage_key" not in response["nodes"]["IMAGE_GENERATE"]["result"]


def test_v3_detail_get_enforces_ownership_and_does_not_mutate(session: Session, client: TestClient) -> None:
    execution, _, _ = _seed_v3(session)
    before = (execution.status, execution.current_node_type)
    response = client.get(f"/workflow-executions/{execution.id}")
    assert response.status_code == 200
    assert (execution.status, execution.current_node_type) == before

    other = WorkflowExecution(
        user_id=session.query(User).order_by(User.id.asc()).first().id,
        workflow_key="vora_content_creation",
        workflow_version=3,
    )
    session.add(other)
    session.flush()
    assert client.get(f"/workflow-executions/{other.id}").status_code == 404


def test_v3_slide_image_local_storage_and_validation(
    session: Session, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution, _, assets = _seed_v3(session)
    monkeypatch.setattr(main.settings, "uploads_root", tmp_path)
    (tmp_path / assets[0].storage_key).parent.mkdir(parents=True)
    (tmp_path / assets[0].storage_key).write_bytes(b"png")

    response = client.get(f"/workflow-executions/{execution.id}/slides/1/image")
    assert response.status_code == 200
    assert response.content == b"png"
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get(f"/workflow-executions/{execution.id}/slides/6/image").status_code == 422
    assert client.get(f"/workflow-executions/{execution.id}/slides/2/image").status_code == 404
