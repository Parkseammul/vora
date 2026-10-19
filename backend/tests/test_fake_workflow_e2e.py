"""Offline approval-to-video workflow integration coverage.

This test deliberately uses the fake provider bootstrap and blocks common HTTP
entry points so it cannot consume vendor credits when local .env has real keys.
"""

import urllib.request
from collections.abc import Iterator
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import main
from app.database import engine
from app.e2e_providers import (
    DeterministicE2ELLMProvider,
    FFmpegE2ETTSProvider,
    FFmpegE2EVideoProvider,
)
from app.models import (
    AssetType,
    ExecutionInputSnapshot,
    FileAsset,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    NodeExecutionStatus,
    User,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.node_executors import RuleBasedNodeExecutor
from app.video_async import QueuedVideoTask, VideoGenerationWorker
from app.workflow_execution_service import DEV_USER_EMAIL


class RecordingDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[tuple[int, int, int]] = []
        self.stages: list[str] = []

    def enqueue(
        self, workflow_execution_id: int, node_execution_id: int, attempt_id: int
    ) -> QueuedVideoTask:
        self.enqueued.append((workflow_execution_id, node_execution_id, attempt_id))
        self.publish_progress(workflow_execution_id, "QUEUED", "Video generation is queued")
        return QueuedVideoTask(f"fake-video-{attempt_id}", "video_generation")

    def publish_progress(self, workflow_execution_id: int, stage: str, message: str) -> None:
        self.stages.append(stage)


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
    root = Path("uploads") / f"test-fake-workflow-{uuid4()}"
    try:
        yield root
    finally:
        rmtree(root, ignore_errors=True)


def test_fake_provider_workflow_reaches_final_video_approval_without_http(
    session: Session, monkeypatch: pytest.MonkeyPatch, uploads_root: Path
) -> None:
    # This test owns its offline bootstrap instead of depending on a shell
    # variable left behind by a developer or another test process.
    previous_llm = getattr(main.app.state, "llm_provider", None)
    previous_media = getattr(main.app.state, "media_providers", None)
    monkeypatch.setattr(main.settings, "e2e_fake_providers", True)
    main._configure_default_providers()
    assert isinstance(main.get_llm_provider(), DeterministicE2ELLMProvider)
    media = main.app.state.media_providers
    assert isinstance(media[0], FFmpegE2EVideoProvider)
    assert isinstance(media[1], FFmpegE2ETTSProvider)

    def block_external_http(*args: object, **kwargs: object) -> None:
        raise AssertionError("External HTTP is forbidden in fake workflow E2E")

    monkeypatch.setattr(httpx, "post", block_external_http)
    monkeypatch.setattr(urllib.request, "urlopen", block_external_http)
    dispatcher = RecordingDispatcher()
    monkeypatch.setattr(main, "get_video_task_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(main.settings, "uploads_root", uploads_root)
    monkeypatch.setattr("app.workflow_execution_service.UPLOADS_ROOT", uploads_root)

    user = session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
    if user is None:
        session.add(User(email=DEV_USER_EMAIL, name="VORA Dev"))
        session.commit()
        user = session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
    assert user is not None

    def get_test_session() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_session] = get_test_session
    try:
        with TestClient(main.app) as client:
            # The public creation API is V3 and requires a source image. This
            # test covers the retained V1 approval-to-video workflow directly.
            execution = WorkflowExecution(
                user_id=user.id,
                workflow_key="vora_content_creation",
                workflow_version=1,
            )
            session.add(execution)
            session.flush()
            source_asset = FileAsset(
                workflow_execution_id=execution.id,
                created_by_node_execution_id=None,
                asset_type=AssetType.IMAGE,
                storage_key=f"inputs/{execution.id}/source.png",
                file_name="source.png",
                mime_type="image/png",
                file_size=1,
            )
            session.add(source_asset)
            session.commit()
            services = main.get_ai_workflow_services(session)
            assert main.get_workflow_engine(session, services).start_execution(
                execution.id,
                {"request_text": "offline fake request", "source_asset_id": source_asset.id},
            ) is WorkflowExecutionStatus.WAITING_APPROVAL
            execution_id = execution.id
            snapshot = session.scalar(
                select(ExecutionInputSnapshot).where(
                    ExecutionInputSnapshot.workflow_execution_id == execution_id
                )
            )
            assert snapshot is None
            content_plan = _node(session, execution_id, "content_planning")
            assert execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
            assert content_plan.status is NodeExecutionStatus.WAITING

            bgm = FileAsset(
                workflow_execution_id=execution_id,
                created_by_node_execution_id=None,
                asset_type=AssetType.AUDIO,
                storage_key=f"bgm/{uuid4()}.mp3",
                file_name="fake-bgm.mp3",
                mime_type="audio/mpeg",
                file_size=0,
            )
            session.add(bgm)
            session.flush()
            bgm_path = uploads_root / bgm.storage_key
            bgm_path.parent.mkdir(parents=True)
            FFmpegE2ETTSProvider().synthesize("bgm", bgm_path)
            bgm.file_size = bgm_path.stat().st_size
            session.commit()
            monkeypatch.setattr(main.settings, "fixed_bgm_asset_id", bgm.id)

            script_approval = client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={"node_execution_id": content_plan.id},
            )
            assert script_approval.status_code == 200
            script = _node(session, execution_id, "script_generation")
            assert script.status is NodeExecutionStatus.WAITING

            video_enqueue = client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={"node_execution_id": script.id},
            )
            assert video_enqueue.status_code == 200
            video = _node(session, execution_id, "video_generation")
            attempt = session.scalar(
                select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == video.id)
            )
            assert attempt is not None
            assert video.status is NodeExecutionStatus.RUNNING
            assert execution.status is WorkflowExecutionStatus.RUNNING
            assert dispatcher.enqueued == [(execution_id, video.id, attempt.id)]

            services = main.get_ai_workflow_services(session)
            worker = VideoGenerationWorker(
                lambda node_id: session.get(NodeExecution, node_id),
                RuleBasedNodeExecutor(
                    content_planning_service=services.content_planning,
                    script_generation_service=services.script_generation,
                    video_generation_service=services.video_generation,
                    session=session,
                ),
                main.get_workflow_engine(session, services),
                dispatcher,
            )
            worker.execute(execution_id, video.id, attempt.id)

            session.refresh(execution)
            session.refresh(video)
            session.refresh(attempt)
            assert execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
            assert video.status is NodeExecutionStatus.WAITING
            assert attempt.status is NodeExecutionAttemptStatus.SUCCEEDED
            assert (uploads_root / "generated" / str(execution_id) / "final_video.mp4").is_file()
            assert dispatcher.stages == [
                "QUEUED",
                "SCENE_GENERATION",
                "TTS_GENERATION",
                "COMPOSING",
                "COMPLETED",
            ]

            final_approval = client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={"node_execution_id": video.id},
            )
            assert final_approval.status_code == 200
            session.refresh(execution)
            assert execution.status is WorkflowExecutionStatus.SUCCEEDED
            assert [node.status for node in _nodes(session, execution_id)] == [
                NodeExecutionStatus.SUCCEEDED,
                NodeExecutionStatus.SUCCEEDED,
                NodeExecutionStatus.SUCCEEDED,
                NodeExecutionStatus.SUCCEEDED,
            ]
    finally:
        main.app.dependency_overrides.clear()
        if previous_llm is not None:
            main.configure_llm_provider(previous_llm)
        if previous_media is not None:
            main.configure_media_providers(*previous_media)


def _node(session: Session, execution_id: int, key: str) -> NodeExecution:
    node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution_id,
            NodeExecution.node_type == key,
        )
    )
    assert node is not None
    return node


def _nodes(session: Session, execution_id: int) -> list[NodeExecution]:
    return list(
        session.scalars(
            select(NodeExecution)
            .where(NodeExecution.workflow_execution_id == execution_id)
            .order_by(NodeExecution.id)
        )
    )
