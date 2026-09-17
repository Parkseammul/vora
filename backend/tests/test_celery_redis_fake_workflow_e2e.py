"""Real Redis/Celery fake-provider workflow E2E, opt-in for dedicated infrastructure."""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
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
    UserApproval,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.workflow_execution_service import DEV_USER_EMAIL

pytestmark = pytest.mark.skipif(
    os.environ.get("VORA_RUN_CELERY_REDIS_E2E", "").lower() != "true",
    reason="set VORA_RUN_CELERY_REDIS_E2E=true with dedicated Redis settings",
)


@pytest.fixture
def uploads_root() -> Iterator[Path]:
    root = (Path("uploads") / f"test-celery-redis-{uuid4()}").resolve()
    try:
        yield root
    finally:
        rmtree(root, ignore_errors=True)


def test_real_redis_celery_worker_completes_fake_video_workflow(
    monkeypatch: pytest.MonkeyPatch, uploads_root: Path
) -> None:
    """The producer and child worker share only explicit fake/test environment settings."""
    assert main.settings.e2e_fake_providers is True
    assert isinstance(main.get_llm_provider(), DeterministicE2ELLMProvider)
    assert isinstance(main.app.state.media_providers[0], FFmpegE2EVideoProvider)
    assert isinstance(main.app.state.media_providers[1], FFmpegE2ETTSProvider)
    assert main.settings.video_generation_queue.startswith("vora_test_")
    assert main.settings.publication_queue != main.settings.video_generation_queue

    worker: subprocess.Popen[bytes] | None = None
    execution_id: int | None = None
    created_dev_user = False
    try:
        with Session(engine) as session:
            dev_user = session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
            if dev_user is None:
                session.add(User(email=DEV_USER_EMAIL, name="VORA Celery E2E"))
                session.commit()
                created_dev_user = True

        monkeypatch.setattr(main.settings, "uploads_root", uploads_root)
        monkeypatch.setattr("app.workflow_execution_service.UPLOADS_ROOT", uploads_root)
        with TestClient(main.app) as client:
            created = client.post("/workflow-executions", data={"request_text": "fake celery workflow"})
            assert created.status_code == 200
            execution_id = int(created.json()["workflow_execution_id"])

            with Session(engine) as session:
                execution = session.get(WorkflowExecution, execution_id)
                snapshot = session.scalar(
                    select(ExecutionInputSnapshot).where(
                        ExecutionInputSnapshot.workflow_execution_id == execution_id
                    )
                )
                assert execution is not None and snapshot is not None
                planning = _node(session, execution_id, "content_planning")
                planning_id = planning.id
                assert execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
                assert planning.status is NodeExecutionStatus.WAITING_APPROVAL

                bgm = FileAsset(
                    workflow_execution_id=execution_id,
                    execution_input_snapshot_id=snapshot.id,
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
                FFmpegE2ETTSProvider().synthesize("local test background audio", bgm_path)
                bgm.file_size = bgm_path.stat().st_size
                session.commit()
                bgm_id = bgm.id

            monkeypatch.setattr(main.settings, "fixed_bgm_asset_id", bgm_id)
            worker = _start_worker(uploads_root, bgm_id)

            approved_planning = client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={"node_execution_id": planning_id},
            )
            assert approved_planning.status_code == 200
            with Session(engine) as session:
                script = _node(session, execution_id, "script_generation")
                script_id = script.id
                assert script.status is NodeExecutionStatus.WAITING_APPROVAL

            enqueued = client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={"node_execution_id": script_id},
            )
            assert enqueued.status_code == 200

            execution, video, attempt = _wait_for_video_completion(execution_id)
            assert execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
            assert video.status is NodeExecutionStatus.WAITING_APPROVAL
            assert attempt.status is NodeExecutionAttemptStatus.SUCCESS
            assert attempt.metadata_["queue"] == main.settings.video_generation_queue
            assert (uploads_root / "generated" / str(execution_id) / "final_video.mp4").is_file()

            final_approval = client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={"node_execution_id": video.id},
            )
            assert final_approval.status_code == 200
            with Session(engine) as session:
                final = session.get(WorkflowExecution, execution_id)
                assert final is not None and final.status is WorkflowExecutionStatus.SUCCESS
    finally:
        _stop_worker(worker)
        if execution_id is not None:
            _delete_execution(execution_id)
        if created_dev_user:
            with Session(engine) as session:
                session.execute(delete(User).where(User.email == DEV_USER_EMAIL))
                session.commit()


def _start_worker(uploads_root: Path, bgm_id: int) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.update(
        {
            "VORA_E2E_FAKE_PROVIDERS": "true",
            "FIXED_BGM_ASSET_ID": str(bgm_id),
            "UPLOADS_ROOT": str(uploads_root),
        }
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.celery_app",
            "worker",
            "--pool=solo",
            "--loglevel=WARNING",
            "--queues",
            main.settings.video_generation_queue,
            "--hostname",
            f"vora-test-{uuid4()}@%h",
        ],
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_video_completion(
    workflow_execution_id: int, timeout_seconds: float = 90.0
) -> tuple[WorkflowExecution, NodeExecution, NodeExecutionAttempt]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with Session(engine) as session:
            execution = session.get(WorkflowExecution, workflow_execution_id)
            video = _node(session, workflow_execution_id, "video_generation")
            attempt = session.scalar(
                select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == video.id)
            )
            assert execution is not None and attempt is not None
            if attempt.status is not NodeExecutionAttemptStatus.RUNNING:
                session.expunge_all()
                return execution, video, attempt
        time.sleep(1)
    raise AssertionError("Celery worker did not finish the queued fake video within 90 seconds")


def _node(session: Session, workflow_execution_id: int, key: str) -> NodeExecution:
    node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == workflow_execution_id,
            NodeExecution.node_key == key,
        )
    )
    assert node is not None
    return node


def _delete_execution(workflow_execution_id: int) -> None:
    """Remove only rows created by this E2E from the already validated test database."""
    with Session(engine) as session:
        node_ids = select(NodeExecution.id).where(NodeExecution.workflow_execution_id == workflow_execution_id)
        session.execute(delete(UserApproval).where(UserApproval.node_execution_id.in_(node_ids)))
        session.execute(delete(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id.in_(node_ids)))
        session.execute(delete(FileAsset).where(FileAsset.workflow_execution_id == workflow_execution_id))
        session.execute(delete(NodeExecution).where(NodeExecution.workflow_execution_id == workflow_execution_id))
        session.execute(
            delete(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id == workflow_execution_id
            )
        )
        session.execute(delete(WorkflowExecution).where(WorkflowExecution.id == workflow_execution_id))
        session.commit()


def _stop_worker(worker: subprocess.Popen[bytes] | None) -> None:
    if worker is None or worker.poll() is not None:
        return
    worker.terminate()
    try:
        worker.wait(timeout=10)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=10)
