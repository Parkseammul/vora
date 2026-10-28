from collections.abc import Iterator, Sequence
from pathlib import Path
from shutil import rmtree
from types import SimpleNamespace
from typing import TypeVar
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import main
from app.database import engine
from app.input_analysis import (
    AgeGroup,
    ContentGoal,
    InputAnalysisResult,
    TargetAudience,
    analyze_input,
)
from app.llm_provider import LLMMetadata, LLMProviderType, LLMResult
from app.models import (
    ExecutionInputSnapshot,
    FileAsset,
    NodeExecution,
    PromptVersion,
    RulesVersion,
    User,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.v3_runtime import ContentPlanOutput, TextCheckOutput, TextGenerateOutput
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
    if session.scalar(select(RulesVersion).where(RulesVersion.version == 1)) is None:
        session.add(RulesVersion(version=1, rules_data={}))
    if session.scalar(select(PromptVersion).where(PromptVersion.node_type == "CONTENT_PLAN")) is None:
        session.add(
            PromptVersion(
                node_type="CONTENT_PLAN",
                version=1,
                prompt_template="{request_text} {marketing_info} {theme} {rules_data} {input_data}",
            )
        )
    if session.scalar(select(PromptVersion).where(PromptVersion.node_type == "TEXT_GENERATE")) is None:
        session.add(
            PromptVersion(
                node_type="TEXT_GENERATE",
                version=1,
                prompt_template="{core_message} {slide_structure} {rules_data} {input_data}",
            )
        )
    if session.scalar(select(PromptVersion).where(PromptVersion.node_type == "TEXT_CHECK")) is None:
        session.add(
            PromptVersion(
                node_type="TEXT_CHECK",
                version=1,
                prompt_template="{slides} {rules_data} {input_data}",
            )
        )
    if session.scalar(select(PromptVersion).where(PromptVersion.node_type == "IMAGE_CHECK")) is None:
        session.add(
            PromptVersion(
                node_type="IMAGE_CHECK",
                version=1,
                prompt_template="{slides} {rules_data} {input_data} {asset_ids}",
            )
        )
    session.commit()

    def get_test_session() -> Iterator[Session]:
        yield session

    previous_llm = getattr(main.app.state, "llm_provider", None)
    previous_media = getattr(main.app.state, "media_providers", None)
    # V3 now includes image generation/checking, so the API fixture must own an
    # explicit offline provider configuration rather than falling through to a
    # production provider that is intentionally absent from test settings.
    monkeypatch.setattr(main.settings, "e2e_fake_providers", True)
    monkeypatch.setattr(main.settings, "uploads_root", uploads_root)
    main._configure_default_providers()
    monkeypatch.setattr("app.workflow_execution_service.UPLOADS_ROOT", uploads_root)
    monkeypatch.setattr(main, "get_llm_provider", lambda: DeterministicPlanningProvider())
    monkeypatch.setattr(
        main.celery_app,
        "send_task",
        lambda *_args, **_kwargs: SimpleNamespace(id="test-generation-task"),
    )
    monkeypatch.setattr(
        main,
        "get_content_plan_chat_model",
        lambda: RunnableLambda(
            lambda _: ContentPlanOutput(core_message="message", slide_structure=["hook"])
        ),
    )
    monkeypatch.setattr(
        main,
        "get_text_check_chat_model",
        lambda: RunnableLambda(
            lambda _: TextCheckOutput(passed=True, violations=[], feedback=None)
        ),
    )
    monkeypatch.setattr(
        main,
        "get_text_generate_chat_model",
        lambda: RunnableLambda(
            lambda _: TextGenerateOutput(slides=[{"slide_no": 1, "text": "copy"}])
        ),
    )
    main.app.dependency_overrides[main.get_session] = get_test_session
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()
        if previous_llm is not None:
            main.configure_llm_provider(previous_llm)
        if previous_media is not None:
            main.configure_media_providers(*previous_media)


def nodes_for(session: Session, workflow_execution_id: int) -> list[NodeExecution]:
    return list(
        session.scalars(
            select(NodeExecution)
            .where(NodeExecution.workflow_execution_id == workflow_execution_id)
            .order_by(NodeExecution.id)
        )
    )


def test_source_image_input_is_reassembled_from_db_and_stops_for_content_approval(
    client: TestClient, session: Session
) -> None:
    rules_version = session.scalar(select(RulesVersion).where(RulesVersion.version == 1))
    assert rules_version is not None
    response = client.post(
        "/workflow-executions",
        data={
            "request_text": "짧은 영상을 만들어줘",
            "marketing_info": "product information",
            "rules_version_id": str(rules_version.id),
        },
        files={"source_image": ("source.png", b"source", "image/png")},
    )

    assert response.status_code == 202
    body = response.json()
    execution = session.get(WorkflowExecution, body["workflow_execution_id"])
    assert execution is not None
    snapshot = session.scalar(
        select(ExecutionInputSnapshot).where(
            ExecutionInputSnapshot.workflow_execution_id == execution.id
        )
    )
    assert snapshot is not None
    assert snapshot.request_text == "짧은 영상을 만들어줘"
    assert snapshot.marketing_info == "product information"
    source = session.get(FileAsset, snapshot.source_asset_id)
    assert source is not None
    assert source.created_by_node_execution_id is None
    assert source.slide_no is None

    assert execution.status is WorkflowExecutionStatus.PENDING
    assert nodes_for(session, execution.id) == []
    assert body == {
        "workflow_execution_id": execution.id,
        "status": "PENDING",
        "dispatch_intent_id": body["dispatch_intent_id"],
    }


def test_source_image_is_persisted_and_reaches_input_analysis(
    client: TestClient, session: Session, uploads_root: Path
) -> None:
    rules_version = session.scalar(select(RulesVersion).where(RulesVersion.version == 1))
    assert rules_version is not None
    response = client.post(
        "/workflow-executions",
        data={
            "request_text": "request",
            "marketing_info": "product information",
            "rules_version_id": str(rules_version.id),
        },
        files={"source_image": ("source.jpg", b"source", "image/jpeg")},
    )

    assert response.status_code == 202
    execution_id = response.json()["workflow_execution_id"]
    snapshot = session.scalar(
        select(ExecutionInputSnapshot).where(
            ExecutionInputSnapshot.workflow_execution_id == execution_id
        )
    )
    assert snapshot is not None
    asset = session.get(FileAsset, snapshot.source_asset_id)
    assert asset is not None
    assert asset.file_name == "source.jpg"
    assert asset.created_by_node_execution_id is None
    assert asset.slide_no is None
    assert not asset.storage_key.startswith("C:")
    assert (uploads_root / asset.storage_key).read_bytes() == b"source"

    assert nodes_for(session, execution_id) == []


@pytest.mark.parametrize(
    ("data", "files"),
    [
        ({}, None),
        ({"request_text": ""}, None),
        ({"request_text": "   "}, None),
        (
            {"request_text": "request", "marketing_info": "info", "rules_version_id": "1"},
            {"source_image": ("file.pdf", b"pdf", "application/pdf")},
        ),
        (
            {"request_text": "request", "marketing_info": "info", "rules_version_id": "1"},
            {"source_image": ("file.gif", b"gif", "image/gif")},
        ),
        (
            {"request_text": "request", "marketing_info": "info", "rules_version_id": "1"},
            {"source_image": ("file.webp", b"webp", "image/webp")},
        ),
    ],
)
def test_api_rejects_invalid_input(
    client: TestClient,
    data: dict[str, str],
    files: dict[str, tuple[str, bytes, str]] | None,
) -> None:
    response = client.post("/workflow-executions", data=data, files=files)

    assert response.status_code == 422


def test_api_rejects_image_larger_than_10mb(client: TestClient) -> None:
    response = client.post(
        "/workflow-executions",
        data={"request_text": "request", "marketing_info": "info", "rules_version_id": "1"},
        files={"source_image": ("large.png", b"x" * (10 * 1024 * 1024 + 1), "image/png")},
    )

    assert response.status_code == 422


def test_database_failure_cleans_files_and_does_not_start_engine(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch, uploads_root: Path
) -> None:
    existing_workflow_ids = set(session.scalars(select(WorkflowExecution.id)))

    def fail_commit(self: Session) -> None:
        raise OperationalError("commit", {}, RuntimeError("database unavailable"))

    monkeypatch.setattr(Session, "commit", fail_commit)
    response = client.post(
        "/workflow-executions",
        data={"request_text": "request", "marketing_info": "info", "rules_version_id": "1"},
        files={"source_image": ("input.png", b"image", "image/png")},
    )

    assert response.status_code == 500
    assert [path for path in uploads_root.rglob("*") if path.is_file()] == []
    created_workflow_ids = set(session.scalars(select(WorkflowExecution.id))) - existing_workflow_ids
    assert created_workflow_ids == set()
    assert session.scalars(
        select(NodeExecution).where(NodeExecution.workflow_execution_id.in_(created_workflow_ids))
    ).all() == []


def test_input_analysis_result_contract_rejects_invalid_values() -> None:
    assert InputAnalysisResult().model_dump(mode="json") == {
        "request_text": "",
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


def test_input_analysis_applies_revision_request_to_target_audience() -> None:
    result = analyze_input(
        {
            "request_text": "운동 영상을 만들어줘",
            "revision_request": "타깃을 20대로 바꿔줘",
            "source_asset_id": 1,
        }
    )

    assert result.target_audience.age_group.value == "TWENTIES"


def test_input_analysis_revision_preserves_unmodified_baseline_fields() -> None:
    baseline = InputAnalysisResult(
        content_goal=ContentGoal.INFORMATIONAL,
        target_audience=TargetAudience(age_group=AgeGroup.THIRTIES, audience_group="러너"),
        duration_seconds=45,
        tone="차분하게",
        source_asset_ids=[11, 12],
    )

    result = analyze_input(
        {
            "request_text": "기존 요청",
            "revision_request": "타깃을 20대로 바꿔줘",
            "source_asset_id": 99,
            "previous_output": baseline.model_dump(mode="json"),
        }
    )

    assert result.target_audience.age_group is AgeGroup.TWENTIES
    assert result.content_goal is ContentGoal.INFORMATIONAL
    assert result.duration_seconds == 45
    assert result.tone == "차분하게"
    assert result.source_asset_ids == [11, 12]
