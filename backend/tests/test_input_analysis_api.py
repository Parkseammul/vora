from collections.abc import Iterator, Sequence
from pathlib import Path
from shutil import rmtree
from typing import TypeVar
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import main
from app.content_planning import ContentPlanningService
from app.database import engine
from app.input_analysis import InputAnalysisResult
from app.llm_provider import LLMMetadata, LLMProviderType, LLMResult
from app.models import (
    AssetType,
    ExecutionInputSnapshot,
    FileAsset,
    InputType,
    NodeExecution,
    NodeExecutionStatus,
    User,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.node_executors import RuleBasedNodeExecutor
from app.workflow_execution_service import DEV_USER_EMAIL

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class DeterministicPlanningProvider:
    def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        provider: LLMProviderType,
        model: str,
        images: Sequence[str] = (),
    ) -> LLMResult:
        asset_ids = [int(image) for image in images]
        scene_count = len(asset_ids) or 1
        duration = 30 / scene_count
        scenes: list[dict[str, object]] = [
            {
                "purpose": "scene",
                "main_objects": [],
                "description": "visual",
                "duration_seconds": duration,
                "source_asset_id": asset_id,
                "visual_direction": "clean",
                "transition_to_next": "CUT" if index < scene_count - 1 else None,
            }
            for index, asset_id in enumerate(asset_ids)
        ]
        if not scenes:
            scenes = [
                {
                    "purpose": "scene",
                    "main_objects": [],
                    "description": "visual",
                    "duration_seconds": duration,
                    "source_asset_id": None,
                    "visual_direction": "clean",
                    "transition_to_next": None,
                }
            ]
        return LLMResult(
            data=response_model.model_validate(
                {
                    "concept": "concept",
                    "hook": "hook",
                    "key_message": "message",
                    "cta": "cta",
                    "visual_style": "clean",
                    "bgm_direction": "upbeat",
                    "scenes": scenes,
                }
            ),
            metadata=LLMMetadata(provider=provider, model=model),
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
def uploads_root() -> Iterator[Path]:
    root = Path("uploads") / f"test-input-analysis-{uuid4()}"
    try:
        yield root
    finally:
        rmtree(root, ignore_errors=True)


@pytest.fixture
def client(
    session: Session, monkeypatch: pytest.MonkeyPatch, uploads_root: Path
) -> Iterator[TestClient]:
    if session.scalar(select(User).where(User.email == DEV_USER_EMAIL)) is None:
        session.add(User(email=DEV_USER_EMAIL, name="VORA Dev"))
        session.commit()

    def get_test_session() -> Iterator[Session]:
        yield session

    monkeypatch.setattr("app.workflow_execution_service.UPLOADS_ROOT", uploads_root)
    monkeypatch.setattr(
        main,
        "RuleBasedNodeExecutor",
        lambda: RuleBasedNodeExecutor(
            content_planning_service=ContentPlanningService(
                DeterministicPlanningProvider(),
                LLMProviderType.OPENAI,
                "test-model",
                session,
            )
        ),
    )
    main.app.dependency_overrides[main.get_session] = get_test_session
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


def nodes_for(session: Session, workflow_execution_id: int) -> list[NodeExecution]:
    return list(
        session.scalars(
            select(NodeExecution)
            .where(NodeExecution.workflow_execution_id == workflow_execution_id)
            .order_by(NodeExecution.id)
        )
    )


def test_text_input_is_reassembled_from_db_and_stops_for_content_approval(
    client: TestClient, session: Session
) -> None:
    response = client.post("/workflow-executions", data={"request_text": "짧은 영상을 만들어줘"})

    assert response.status_code == 200
    body = response.json()
    execution = session.get(WorkflowExecution, body["workflow_execution_id"])
    assert execution is not None
    snapshot = session.scalar(
        select(ExecutionInputSnapshot).where(
            ExecutionInputSnapshot.workflow_execution_id == execution.id
        )
    )
    assert snapshot is not None
    assert snapshot.input_type is InputType.TEXT
    assert snapshot.request_text == "짧은 영상을 만들어줘"
    assert session.scalars(select(FileAsset).where(FileAsset.execution_input_snapshot_id == snapshot.id)).all() == []

    input_analysis, content_planning = nodes_for(session, execution.id)
    assert input_analysis.output_data == content_planning.input_data
    assert input_analysis.input_data == {
        "request_text": "짧은 영상을 만들어줘",
        "input_type": "TEXT",
        "source_asset_ids": [],
    }
    assert execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
    assert content_planning.status is NodeExecutionStatus.WAITING_APPROVAL
    assert body == {
        "workflow_execution_id": execution.id,
        "status": "WAITING_APPROVAL",
        "current_node": "content_planning",
        "current_node_execution_id": content_planning.id,
        "result": content_planning.output_data,
    }


def test_images_preserve_db_sort_order_and_reach_input_analysis(
    client: TestClient, session: Session, uploads_root: Path
) -> None:
    response = client.post(
        "/workflow-executions",
        data={"request_text": "이 화장품 사진으로 30초 광고 만들어줘"},
        files=[
            ("images", ("first.jpg", b"first", "image/jpeg")),
            ("images", ("second.png", b"second", "image/png")),
            ("images", ("third.jpg", b"third", "image/jpeg")),
        ],
    )

    assert response.status_code == 200
    execution_id = response.json()["workflow_execution_id"]
    snapshot = session.scalar(
        select(ExecutionInputSnapshot).where(
            ExecutionInputSnapshot.workflow_execution_id == execution_id
        )
    )
    assert snapshot is not None
    assets = session.scalars(
        select(FileAsset)
        .where(
            FileAsset.execution_input_snapshot_id == snapshot.id,
            FileAsset.asset_type == AssetType.IMAGE,
        )
        .order_by(FileAsset.sort_order)
    ).all()
    assert snapshot.input_type is InputType.TEXT_IMAGE
    assert [asset.sort_order for asset in assets] == [1, 2, 3]
    assert [asset.file_name for asset in assets] == ["first.jpg", "second.png", "third.jpg"]
    assert all(not asset.storage_key.startswith("C:") for asset in assets)
    assert [(uploads_root / asset.storage_key).read_bytes() for asset in assets] == [
        b"first",
        b"second",
        b"third",
    ]

    input_analysis, content_planning = nodes_for(session, execution_id)
    source_asset_ids = [asset.id for asset in assets]
    assert input_analysis.input_data["source_asset_ids"] == source_asset_ids
    assert input_analysis.output_data == content_planning.input_data
    assert input_analysis.output_data["content_goal"] == "PRODUCT_AD"
    assert input_analysis.output_data["source_asset_ids"] == source_asset_ids


@pytest.mark.parametrize(
    ("data", "files"),
    [
        ({}, None),
        ({"request_text": ""}, None),
        ({"request_text": "   "}, None),
        (
            {"request_text": "request"},
            [("images", ("file.pdf", b"pdf", "application/pdf"))],
        ),
        (
            {"request_text": "request"},
            [("images", ("file.gif", b"gif", "image/gif"))],
        ),
        (
            {"request_text": "request"},
            [("images", ("file.webp", b"webp", "image/webp"))],
        ),
        (
            {"request_text": "request"},
            [("images", (f"{index}.png", b"x", "image/png")) for index in range(7)],
        ),
    ],
)
def test_api_rejects_invalid_input(
    client: TestClient, data: dict[str, str], files: list[tuple[str, tuple[str, bytes, str]]] | None
) -> None:
    response = client.post("/workflow-executions", data=data, files=files)

    assert response.status_code == 422


def test_api_rejects_image_larger_than_10mb(client: TestClient) -> None:
    response = client.post(
        "/workflow-executions",
        data={"request_text": "request"},
        files={"images": ("large.png", b"x" * (10 * 1024 * 1024 + 1), "image/png")},
    )

    assert response.status_code == 422


def test_database_failure_cleans_files_and_does_not_start_engine(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch, uploads_root: Path
) -> None:
    def fail_commit(self: Session) -> None:
        raise OperationalError("commit", {}, RuntimeError("database unavailable"))

    monkeypatch.setattr(Session, "commit", fail_commit)
    response = client.post(
        "/workflow-executions",
        data={"request_text": "request"},
        files={"images": ("input.png", b"image", "image/png")},
    )

    assert response.status_code == 500
    assert [path for path in uploads_root.rglob("*") if path.is_file()] == []
    assert session.scalars(select(NodeExecution)).all() == []


def test_input_analysis_result_contract_rejects_invalid_values() -> None:
    assert InputAnalysisResult().model_dump(mode="json") == {
        "content_goal": "GENERAL_SHORTFORM",
        "target_audience": {
            "age_group": "ALL",
            "gender": "ALL",
            "audience_group": "일반인",
        },
        "duration_seconds": 30,
        "tone": "밝고 자연스럽게",
        "source_asset_ids": [],
    }
    with pytest.raises(ValueError):
        InputAnalysisResult(duration_seconds=61)
    with pytest.raises(ValueError):
        InputAnalysisResult(source_asset_ids=[1, 1])
