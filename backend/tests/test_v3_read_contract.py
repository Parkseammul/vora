from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
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


def test_workflow_list_is_owned_sorted_limited_and_cursor_paginated(
    session: Session, client: TestClient
) -> None:
    other = User(email=f"{uuid4()}@example.com", name="other-list-user")
    owner = User(email=f"{uuid4()}@example.com", name="owner-list-user")
    session.add_all([other, owner])
    session.flush()
    base = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    own_items = [
        WorkflowExecution(user_id=owner.id, workflow_key="vora_content_creation", workflow_version=3, status=WorkflowExecutionStatus.SUCCEEDED, created_at=base - timedelta(days=3), updated_at=base - timedelta(minutes=1), completed_at=base - timedelta(minutes=1)),
        WorkflowExecution(user_id=owner.id, workflow_key="vora_content_creation", workflow_version=3, status=WorkflowExecutionStatus.RUNNING, created_at=base - timedelta(days=2), updated_at=base - timedelta(minutes=2)),
        WorkflowExecution(user_id=owner.id, workflow_key="short_video", workflow_version=1, status=WorkflowExecutionStatus.FAILED, created_at=base - timedelta(days=1), updated_at=base - timedelta(minutes=3)),
    ]
    foreign = WorkflowExecution(user_id=other.id, workflow_key="vora_content_creation", workflow_version=3, status=WorkflowExecutionStatus.SUCCEEDED, created_at=base, updated_at=base)
    session.add_all([*own_items, foreign])
    session.flush()
    image_node = NodeExecution(workflow_execution_id=own_items[0].id, node_type="IMAGE_GENERATE", revision_no=2, status=NodeExecutionStatus.SUCCEEDED, output_data={"asset_ids": []})
    session.add(image_node)
    session.flush()
    asset = FileAsset(workflow_execution_id=own_items[0].id, created_by_node_execution_id=image_node.id, asset_type=AssetType.IMAGE, storage_key="recent/slide-1.png", file_name="slide-1.png", mime_type="image/png", file_size=1, slide_no=1)
    session.add(asset)
    session.flush()
    image_node.output_data = {"asset_ids": [asset.id]}
    session.flush()

    first = client.get("/workflow-executions?limit=2")
    assert first.status_code == 200
    payload = first.json()
    assert [item["workflow_execution_id"] for item in payload["items"]] == [own_items[0].id, own_items[1].id]
    assert {item["status"] for item in payload["items"]} == {"SUCCEEDED", "RUNNING"}
    assert payload["items"][0]["thumbnail_url"].endswith("revision_no=2")
    assert payload["next_cursor"]

    second = client.get(f"/workflow-executions?limit=2&cursor={payload['next_cursor']}")
    assert second.status_code == 200
    assert [item["workflow_execution_id"] for item in second.json()["items"]] == [own_items[2].id]
    assert all(item["workflow_execution_id"] != foreign.id for item in [*payload["items"], *second.json()["items"]])


def test_workflow_list_requires_authentication(client: TestClient) -> None:
    override = main.app.dependency_overrides.pop(main.get_current_user)
    try:
        response = client.get("/workflow-executions")
    finally:
        main.app.dependency_overrides[main.get_current_user] = override
    assert response.status_code == 401


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


def test_v3_selective_revision_reads_reused_assets_from_current_output(
    session: Session, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution, _, current_assets = _seed_v3(session)
    image_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_GENERATE",
            NodeExecution.revision_no == 1,
        )
    )
    assert image_node is not None
    prior_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_GENERATE",
            NodeExecution.revision_no == 0,
        )
    )
    assert prior_node is not None
    reused_assets: list[FileAsset] = []
    for slide_no in (1, 2, 3, 5):
        asset = FileAsset(
            workflow_execution_id=execution.id,
            created_by_node_execution_id=prior_node.id,
            asset_type=AssetType.IMAGE,
            storage_key=f"reused/{uuid4()}.png",
            file_name=f"reused-{slide_no}.png",
            mime_type="image/png",
            file_size=3,
            slide_no=slide_no,
        )
        reused_assets.append(asset)
        session.add(asset)
    session.flush()
    current_output_ids = [
        reused_assets[0].id,
        reused_assets[1].id,
        reused_assets[2].id,
        current_assets[3].id,
        reused_assets[3].id,
    ]
    image_node.output_data = {"asset_ids": current_output_ids}
    monkeypatch.setattr(main.settings, "uploads_root", tmp_path)
    for asset in [*reused_assets, current_assets[3]]:
        path = tmp_path / asset.storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")

    response = main._v3_workflow_response(session, execution)
    assert [image["file_name"] for image in response["images"]] == [
        asset.file_name for asset in [*reused_assets[:3], current_assets[3], reused_assets[3]]
    ]
    for slide_no in range(1, 6):
        image_response = client.get(f"/workflow-executions/{execution.id}/slides/{slide_no}/image")
        assert image_response.status_code == 200


def test_v3_image_read_rejects_asset_not_in_current_output(
    session: Session, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution, _, assets = _seed_v3(session)
    image_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_GENERATE",
            NodeExecution.revision_no == 1,
        )
    )
    assert image_node is not None
    image_node.output_data = {"asset_ids": [asset.id for asset in assets]}
    monkeypatch.setattr(main.settings, "uploads_root", tmp_path)
    path = tmp_path / assets[0].storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")
    response = client.get(f"/workflow-executions/{execution.id}/slides/1/image")
    assert response.status_code == 200

    image_node.output_data = {"asset_ids": [assets[1].id] * 5}
    assert client.get(f"/workflow-executions/{execution.id}/slides/1/image").status_code == 404


def test_v3_image_read_rejects_asset_from_another_workflow(
    session: Session, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution, _, assets = _seed_v3(session)
    image_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_GENERATE",
            NodeExecution.revision_no == 1,
        )
    )
    assert image_node is not None
    other_execution = WorkflowExecution(
        user_id=execution.user_id,
        workflow_key="vora_content_creation",
        workflow_version=3,
    )
    session.add(other_execution)
    session.flush()
    other_asset = FileAsset(
        workflow_execution_id=other_execution.id,
        asset_type=AssetType.IMAGE,
        storage_key=f"other/{uuid4()}.png",
        file_name="other.png",
        mime_type="image/png",
        file_size=3,
        slide_no=1,
    )
    session.add(other_asset)
    session.flush()
    image_node.output_data = {"asset_ids": [other_asset.id, *(asset.id for asset in assets[1:])]}
    monkeypatch.setattr(main.settings, "uploads_root", tmp_path)
    response = main._v3_workflow_response(session, execution)
    assert response["images"] == []
    assert client.get(f"/workflow-executions/{execution.id}/slides/1/image").status_code == 404
