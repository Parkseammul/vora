"""Real Redis/Celery fake-provider workflow E2E, opt-in for dedicated infrastructure."""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import main
from app.celery_app import celery_app
from app.database import engine
from app.e2e_providers import (
    DeterministicE2EImageProvider,
    DeterministicE2ELLMProvider,
    FFmpegE2ETTSProvider,
    FFmpegE2EVideoProvider,
)
from app.models import (
    ApprovalDecision,
    AssetType,
    ExecutionInputSnapshot,
    FileAsset,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionAttemptStatus,
    NodeExecutionStatus,
    PromptVersion,
    Revision,
    RulesVersion,
    SocialAccountConnection,
    SocialConnectionStatus,
    SocialPlatform,
    SocialPublication,
    SocialPublicationAttempt,
    SocialPublicationAttemptStatus,
    SocialPublicationStatus,
    User,
    UserApproval,
    WorkflowDispatchIntent,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.publication_runtime import (
    DeterministicE2EInstagramPublisher,
    get_publication_service,
)
from app.v3_runtime import V3NodeExecutor
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
    dev_user_id: int | None = None
    try:
        with Session(engine) as session:
            dev_user = session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
            if dev_user is None:
                session.add(User(email=DEV_USER_EMAIL, name="VORA Celery E2E"))
                session.commit()
                created_dev_user = True
                dev_user = session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
            assert dev_user is not None
            dev_user_id = dev_user.id
        assert dev_user_id is not None

        monkeypatch.setattr(main.settings, "uploads_root", uploads_root)
        monkeypatch.setattr("app.workflow_execution_service.UPLOADS_ROOT", uploads_root)
        with TestClient(main.app) as client:
            with Session(engine) as session:
                # The public creation API is V3.  This E2E deliberately covers
                # the retained V1 Celery video worker path, so bootstrap V1
                # state directly instead of sending an invalid V3 request.
                execution = WorkflowExecution(
                    user_id=dev_user_id,
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
                    {"request_text": "fake celery workflow", "source_asset_id": source_asset.id},
                ) is WorkflowExecutionStatus.WAITING_APPROVAL
                execution_id = execution.id

                execution = session.get(WorkflowExecution, execution_id)
                snapshot = session.scalar(
                    select(ExecutionInputSnapshot).where(
                        ExecutionInputSnapshot.workflow_execution_id == execution_id
                    )
                )
                assert execution is not None and snapshot is None
                planning = _node(session, execution_id, "content_planning")
                planning_id = planning.id
                assert execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
                assert planning.status is NodeExecutionStatus.WAITING

                bgm = FileAsset(
                    workflow_execution_id=execution_id,
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
                assert script.status is NodeExecutionStatus.WAITING

            enqueued = client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={"node_execution_id": script_id},
            )
            assert enqueued.status_code == 200

            execution, video, attempt = _wait_for_video_completion(execution_id)
            assert execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
            assert video.status is NodeExecutionStatus.WAITING
            assert attempt.status is NodeExecutionAttemptStatus.SUCCEEDED
            assert attempt.metadata_["queue"] == main.settings.video_generation_queue
            assert (uploads_root / "generated" / str(execution_id) / "final_video.mp4").is_file()

            final_approval = client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={"node_execution_id": video.id},
            )
            assert final_approval.status_code == 200
            with Session(engine) as session:
                final = session.get(WorkflowExecution, execution_id)
                assert final is not None and final.status is WorkflowExecutionStatus.SUCCEEDED
    finally:
        _stop_worker(worker)
        if execution_id is not None:
            _delete_execution(execution_id)
        if created_dev_user:
            with Session(engine) as session:
                session.execute(delete(User).where(User.email == DEV_USER_EMAIL))
                session.commit()


def test_real_redis_celery_worker_reaches_v3_human_approval_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V3 generation uses a child Celery process and never needs vendor credentials."""
    assert main.settings.e2e_fake_providers is True
    assert main.settings.generation_queue.startswith("vora_test_")
    assert main.settings.generation_queue != main.settings.publication_queue
    worker: subprocess.Popen[bytes] | None = None
    execution_id: int | None = None
    version = int(uuid4().hex[:7], 16)
    root = (Path("uploads") / f"v3-celery-e2e-{uuid4()}").resolve()
    created_dev_user = False
    try:
        with Session(engine) as session:
            user = session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
            if user is None:
                session.add(User(email=DEV_USER_EMAIL, name="V3 Celery E2E"))
                created_dev_user = True
            rules = RulesVersion(version=version, rules_data={"e2e": True})
            session.add(rules)
            session.add_all(
                [
                    PromptVersion(
                        node_type="CONTENT_PLAN",
                        version=version,
                        prompt_template="Request: {request_text}\nRules: {rules_data}",
                    ),
                    PromptVersion(
                        node_type="TEXT_GENERATE",
                        version=version,
                        prompt_template="Message: {core_message}\nSlides: {slide_structure}",
                    ),
                    PromptVersion(
                        node_type="TEXT_CHECK",
                        version=version,
                        prompt_template="Slides: {slides}\nRules: {rules_data}",
                    ),
                    PromptVersion(
                        node_type="IMAGE_CHECK",
                        version=version,
                        prompt_template="Slides: {slides}\nAssets: {asset_ids}",
                    ),
                ]
            )
            session.commit()
            rules_id = rules.id
        # The API process and the child worker must use the same test-only
        # local storage root; no default uploads directory is involved.
        monkeypatch.setattr(main.settings, "uploads_root", root)
        monkeypatch.setattr("app.workflow_execution_service.UPLOADS_ROOT", root)
        worker = _start_v3_worker(root)
        png = DeterministicE2EImageProvider._PNG
        with TestClient(main.app) as client:
            response = client.post(
                "/workflow-executions",
                data={"request_text": "offline carousel", "marketing_info": "details", "theme": "clean", "rules_version_id": str(rules_id)},
                files={"source_image": ("source.png", png, "image/png")},
            )
        assert response.status_code == 202
        execution_id = response.json()["workflow_execution_id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with Session(engine) as session:
                execution = session.get(WorkflowExecution, execution_id)
                if execution is not None and execution.status is WorkflowExecutionStatus.WAITING_APPROVAL:
                    snapshot = session.scalar(
                        select(ExecutionInputSnapshot).where(
                            ExecutionInputSnapshot.workflow_execution_id == execution_id
                        )
                    )
                    intent = session.scalar(
                        select(WorkflowDispatchIntent).where(
                            WorkflowDispatchIntent.workflow_execution_id == execution_id
                        )
                    )
                    nodes = list(
                        session.scalars(
                            select(NodeExecution)
                            .where(NodeExecution.workflow_execution_id == execution_id)
                            .order_by(NodeExecution.id)
                        )
                    )
                    assets = list(
                        session.scalars(
                            select(FileAsset)
                            .where(
                                FileAsset.workflow_execution_id == execution_id,
                                FileAsset.slide_no.is_not(None),
                            )
                            .order_by(FileAsset.slide_no)
                        )
                    )
                    attempts = list(
                        session.scalars(
                            select(NodeExecutionAttempt)
                            .join(NodeExecution)
                            .where(NodeExecution.workflow_execution_id == execution_id)
                            .order_by(NodeExecutionAttempt.id)
                        )
                    )
                    assert snapshot is not None
                    assert intent is not None
                    assert intent.action == "START"
                    assert intent.status == "COMPLETED"
                    assert [node.node_type for node in nodes] == ["CONTENT_PLAN", "TEXT_GENERATE", "TEXT_CHECK", "IMAGE_GENERATE", "IMAGE_CHECK", "HUMAN_APPROVAL"]
                    assert len(assets) == 5
                    assert all(asset.mime_type == "image/png" for asset in assets)
                    assert len(attempts) == 5
                    assert all(attempt.status is NodeExecutionAttemptStatus.SUCCEEDED for attempt in attempts)
                    assert all(node.status is not NodeExecutionStatus.RUNNING for node in nodes)
                    return
            time.sleep(0.5)
        with Session(engine) as session:
            execution = session.get(WorkflowExecution, execution_id)
            nodes = list(
                session.scalars(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution_id
                    )
                )
            )
            states = [(node.node_type, node.status.value) for node in nodes]
            attempts = list(
                session.scalars(
                    select(NodeExecutionAttempt)
                    .join(NodeExecution)
                    .where(NodeExecution.workflow_execution_id == execution_id)
                    .order_by(NodeExecutionAttempt.id)
                )
            )
            attempt_errors = [
                (attempt.id, attempt.status.value, attempt.error_code, attempt.error_message)
                for attempt in attempts
            ]
            raise AssertionError(
                f"V3 Celery worker did not reach HUMAN_APPROVAL: "
                f"workflow={execution.status.value if execution else None}, nodes={states}, "
                f"attempts={attempt_errors}"
            )
    finally:
        _stop_worker(worker)
        if execution_id is not None:
            _delete_execution(execution_id)
        with Session(engine) as session:
            session.execute(delete(PromptVersion).where(PromptVersion.version == version))
            session.execute(delete(RulesVersion).where(RulesVersion.version == version))
            if created_dev_user:
                session.execute(delete(User).where(User.email == DEV_USER_EMAIL))
            session.commit()
        rmtree(root, ignore_errors=True)


def test_real_redis_celery_v3_approved_then_fake_instagram_publication() -> None:
    """Approval resumes only PUBLICATION; a separate worker publishes the carousel."""
    context = _V3E2EContext.create()
    generation_worker: subprocess.Popen[bytes] | None = None
    publication_worker: subprocess.Popen[bytes] | None = None
    connection_id: int | None = None
    created_connection = False
    try:
        generation_worker = _start_v3_worker(context.root)
        execution_id = context.create_generation()
        _wait_for_workflow(execution_id, WorkflowExecutionStatus.WAITING_APPROVAL)
        with Session(engine) as session:
            approval_node = _waiting_node(session, execution_id)

        with TestClient(main.app) as client:
            approved = client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={"node_execution_id": approval_node.id, "decision": "APPROVED"},
            )
        assert approved.status_code == 202
        with Session(engine) as session:
            approval = session.scalar(
                select(UserApproval).where(UserApproval.node_execution_id == approval_node.id)
            )
            resume = session.scalar(
                select(WorkflowDispatchIntent).where(
                    WorkflowDispatchIntent.workflow_execution_id == execution_id,
                    WorkflowDispatchIntent.action == "RESUME",
                )
            )
            assert approval is not None and approval.decision is ApprovalDecision.APPROVED
            assert resume is not None and resume.status in {"QUEUED", "CLAIMED", "COMPLETED"}

        completed = _wait_for_workflow(execution_id, WorkflowExecutionStatus.SUCCEEDED)
        with Session(engine) as session:
            publication_node = session.scalar(
                select(NodeExecution).where(
                    NodeExecution.workflow_execution_id == execution_id,
                    NodeExecution.node_type == "PUBLICATION",
                    NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
                )
            )
            assert publication_node is not None
            # Workflow completion only prepares the publication payload.
            assert session.scalar(
                select(SocialPublication).where(SocialPublication.node_execution_id == publication_node.id)
            ) is None
            connection = session.scalar(
                select(SocialAccountConnection).where(
                    SocialAccountConnection.user_id == completed.user_id,
                    SocialAccountConnection.platform == SocialPlatform.INSTAGRAM,
                )
            )
            if connection is None:
                connection = SocialAccountConnection(
                    user_id=completed.user_id,
                    platform=SocialPlatform.INSTAGRAM,
                    external_account_id=f"e2e-{uuid4()}",
                    account_name="E2E fake Instagram",
                    access_token="offline-fake-token",
                    status=SocialConnectionStatus.CONNECTED,
                )
                session.add(connection)
                session.commit()
                created_connection = True
            connection_id = connection.id

        publication_worker = _start_publication_worker(context.root)
        with TestClient(main.app) as client:
            published = client.post(
                f"/workflow-executions/{execution_id}/publications",
                json={"platforms": ["INSTAGRAM"], "instagram_caption": "offline publication"},
            )
        assert published.status_code == 200
        publication_id = published.json()["publications"][0]["id"]
        publication = _wait_for_publication(publication_id)
        assert publication.status is SocialPublicationStatus.PUBLISHED
        assert publication.external_post_id == f"e2e-instagram-{publication.id}"
        assert publication.external_post_url == f"https://e2e.invalid/{publication.id}"
        with Session(engine) as session:
            attempt = session.scalar(
                select(SocialPublicationAttempt).where(
                    SocialPublicationAttempt.social_publication_id == publication_id
                )
            )
            workflow = session.get(WorkflowExecution, execution_id)
            assert attempt is not None and attempt.status is SocialPublicationAttemptStatus.SUCCESS
            assert workflow is not None and workflow.status is WorkflowExecutionStatus.SUCCEEDED
    finally:
        _stop_worker(publication_worker)
        _stop_worker(generation_worker)
        if created_connection and connection_id is not None:
            _delete_social_connection(connection_id)
        context.cleanup()


def test_real_redis_celery_v3_rejected_revision_reenters_human_approval() -> None:
    """A real generation worker resumes the prepared TEXT revision without duplicating it."""
    context = _V3E2EContext.create()
    generation_worker: subprocess.Popen[bytes] | None = None
    try:
        generation_worker = _start_v3_worker(context.root)
        execution_id = context.create_generation()
        _wait_for_workflow(execution_id, WorkflowExecutionStatus.WAITING_APPROVAL)
        with Session(engine) as session:
            original_waiting = _waiting_node(session, execution_id)

        with TestClient(main.app) as client:
            rejected = client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={
                    "node_execution_id": original_waiting.id,
                    "decision": "REJECTED",
                    "revision_target": "TEXT",
                    "revision_request": "Make the carousel copy more direct.",
                },
            )
        assert rejected.status_code == 202
        with Session(engine) as session:
            revision = session.scalar(
                select(Revision).where(Revision.workflow_execution_id == execution_id)
            )
            revision_intent = session.scalar(
                select(WorkflowDispatchIntent).where(
                    WorkflowDispatchIntent.workflow_execution_id == execution_id,
                    WorkflowDispatchIntent.action == "REVISION",
                )
            )
            prepared = session.scalar(
                select(NodeExecution).where(
                    NodeExecution.workflow_execution_id == execution_id,
                    NodeExecution.node_type == "TEXT_GENERATE",
                    NodeExecution.revision_no == 1,
                )
            )
            assert revision is not None and revision.revision_no == 1
            assert revision.start_node_type == "TEXT_GENERATE"
            assert revision_intent is not None and revision_intent.status in {"QUEUED", "CLAIMED", "COMPLETED"}
            assert prepared is not None
            assert session.scalar(
                select(NodeExecutionAttempt).where(
                    NodeExecutionAttempt.node_execution_id == prepared.id,
                    NodeExecutionAttempt.attempt_no == 1,
                )
            ) is not None

        _wait_for_workflow(execution_id, WorkflowExecutionStatus.WAITING_APPROVAL, revision_no=1)
        with Session(engine) as session:
            revisions = list(
                session.scalars(select(Revision).where(Revision.workflow_execution_id == execution_id))
            )
            plans = list(
                session.scalars(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution_id,
                        NodeExecution.node_type == "CONTENT_PLAN",
                    )
                )
            )
            text_nodes = list(
                session.scalars(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution_id,
                        NodeExecution.node_type == "TEXT_GENERATE",
                    )
                )
            )
            revised_waiting = _waiting_node(session, execution_id)
            assert len(revisions) == 1
            assert len(plans) == 1 and plans[0].revision_no == 0
            assert sorted(node.revision_no for node in text_nodes) == [0, 1]
            assert revised_waiting.revision_no == 1
    finally:
        _stop_worker(generation_worker)
        context.cleanup()


def test_real_redis_celery_fake_publication_failure_does_not_republish() -> None:
    """The deterministic failure is terminal and a duplicate user request reuses its intent."""
    context = _V3E2EContext.create()
    generation_worker: subprocess.Popen[bytes] | None = None
    publication_worker: subprocess.Popen[bytes] | None = None
    connection_id: int | None = None
    created_connection = False
    try:
        generation_worker = _start_v3_worker(context.root)
        execution_id = context.create_generation()
        _wait_for_workflow(execution_id, WorkflowExecutionStatus.WAITING_APPROVAL)
        with Session(engine) as session:
            approval_node = _waiting_node(session, execution_id)
        with TestClient(main.app) as client:
            assert client.post(
                f"/workflow-executions/{execution_id}/approvals",
                json={"node_execution_id": approval_node.id, "decision": "APPROVED"},
            ).status_code == 202
        completed = _wait_for_workflow(execution_id, WorkflowExecutionStatus.SUCCEEDED)
        with Session(engine) as session:
            connection = session.scalar(
                select(SocialAccountConnection).where(
                    SocialAccountConnection.user_id == completed.user_id,
                    SocialAccountConnection.platform == SocialPlatform.INSTAGRAM,
                )
            )
            if connection is None:
                connection = SocialAccountConnection(
                    user_id=completed.user_id,
                    platform=SocialPlatform.INSTAGRAM,
                    external_account_id=f"e2e-{uuid4()}",
                    account_name="E2E fake Instagram",
                    access_token="offline-fake-token",
                    status=SocialConnectionStatus.CONNECTED,
                )
                session.add(connection)
                session.commit()
                created_connection = True
            connection_id = connection.id

        publication_worker = _start_publication_worker(context.root)
        body = {
            "platforms": ["INSTAGRAM"],
            "instagram_caption": "__VORA_E2E_FAKE_PUBLICATION_FAILURE__",
        }
        with TestClient(main.app) as client:
            first = client.post(f"/workflow-executions/{execution_id}/publications", json=body)
        assert first.status_code == 200
        publication_id = first.json()["publications"][0]["id"]
        publication = _wait_for_publication_failure(publication_id)
        assert publication.status is SocialPublicationStatus.FAILED
        with TestClient(main.app) as client:
            duplicate = client.post(f"/workflow-executions/{execution_id}/publications", json=body)
        assert duplicate.status_code == 200
        assert duplicate.json()["publications"][0]["id"] == publication_id
        with Session(engine) as session:
            attempts = list(
                session.scalars(
                    select(SocialPublicationAttempt).where(
                        SocialPublicationAttempt.social_publication_id == publication_id
                    )
                )
            )
            assert len(attempts) == 1
            assert attempts[0].status is SocialPublicationAttemptStatus.FAILED
            assert attempts[0].error_code == "e2e_fake_failure"
    finally:
        _stop_worker(publication_worker)
        _stop_worker(generation_worker)
        if created_connection and connection_id is not None:
            _delete_social_connection(connection_id)
        context.cleanup()


def test_real_worker_crash_relay_recovery_reuses_completed_v3_nodes() -> None:
    """A killed worker leaves durable state; Worker B recovers it through the real relay task."""
    context = _V3E2EContext.create()
    worker_a: subprocess.Popen[bytes] | None = None
    worker_b: subprocess.Popen[bytes] | None = None
    try:
        started = context.root / "crash-gate-started"
        worker_a = _start_v3_worker_with_env(
            context.root,
            {
                "VORA_E2E_BLOCK_NODE": "TEXT_GENERATE",
                "VORA_E2E_GATE_STARTED_PATH": str(started),
                "VORA_E2E_GATE_RELEASE_PATH": str(context.root / "never-release"),
                "VORA_E2E_LEASE_SECONDS": "2",
            },
        )
        execution_id = context.create_generation()
        _wait_for_path(started)
        first = _wait_for_claim(execution_id, expected_generation=1)
        assert worker_a.poll() is None
        with Session(engine) as session:
            plan = session.scalar(
                select(NodeExecution).where(
                    NodeExecution.workflow_execution_id == execution_id,
                    NodeExecution.node_type == "CONTENT_PLAN",
                )
            )
            text = session.scalar(
                select(NodeExecution).where(
                    NodeExecution.workflow_execution_id == execution_id,
                    NodeExecution.node_type == "TEXT_GENERATE",
                )
            )
            assert plan is not None and plan.status is NodeExecutionStatus.SUCCEEDED
            assert text is not None and text.status is NodeExecutionStatus.RUNNING

        _kill_worker_tree(worker_a)
        assert worker_a.poll() is not None
        _wait_for_lease_expiry(first.id)
        worker_b = _start_v3_worker_with_env(
            context.root, {"VORA_E2E_LEASE_SECONDS": "2"}
        )
        relay = celery_app.send_task(
            "app.generation_tasks.relay_generation_intents",
            queue=main.settings.generation_queue,
        )
        assert relay.id is not None
        _wait_for_workflow(execution_id, WorkflowExecutionStatus.WAITING_APPROVAL)
        with Session(engine) as session:
            intent = session.get(WorkflowDispatchIntent, first.id)
            plans = list(
                session.scalars(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution_id,
                        NodeExecution.node_type == "CONTENT_PLAN",
                    )
                )
            )
            text_nodes = list(
                session.scalars(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution_id,
                        NodeExecution.node_type == "TEXT_GENERATE",
                    )
                )
            )
            text_attempts = list(
                session.scalars(
                    select(NodeExecutionAttempt)
                    .join(NodeExecution)
                    .where(
                        NodeExecution.workflow_execution_id == execution_id,
                        NodeExecution.node_type == "TEXT_GENERATE",
                    )
                )
            )
            assert intent is not None and intent.status == "COMPLETED"
            assert intent.claim_generation == 2
            assert intent.claim_token != first.claim_token
            assert len(plans) == 1 and plans[0].status is NodeExecutionStatus.SUCCEEDED
            assert len(text_nodes) == 1 and text_nodes[0].status is NodeExecutionStatus.SUCCEEDED
            assert len(text_attempts) == 1
        duplicate = celery_app.send_task(
            "app.generation_tasks.execute_generation",
            kwargs={"intent_id": first.id},
            queue=main.settings.generation_queue,
        )
        _wait_for_task_status(str(duplicate.id), "SUCCESS")
        with Session(engine) as session:
            assert session.scalar(
                select(NodeExecution).where(
                    NodeExecution.workflow_execution_id == execution_id,
                    NodeExecution.node_type == "TEXT_GENERATE",
                )
            ) is not None
            assert len(
                list(
                    session.scalars(
                        select(NodeExecution).where(
                            NodeExecution.workflow_execution_id == execution_id
                        )
                    )
                )
            ) == 6
    finally:
        _stop_worker(worker_b)
        _stop_worker(worker_a)
        context.cleanup()


def test_real_worker_heartbeat_preserves_claim_until_gate_is_released() -> None:
    """An active heartbeat prevents Worker B's real relay task from stealing a live claim."""
    context = _V3E2EContext.create()
    worker_a: subprocess.Popen[bytes] | None = None
    worker_b: subprocess.Popen[bytes] | None = None
    try:
        started, release = context.root / "heartbeat-started", context.root / "heartbeat-release"
        worker_a = _start_v3_worker_with_env(
            context.root,
            {
                "VORA_E2E_BLOCK_NODE": "TEXT_GENERATE",
                "VORA_E2E_GATE_STARTED_PATH": str(started),
                "VORA_E2E_GATE_RELEASE_PATH": str(release),
                "VORA_E2E_LEASE_SECONDS": "2",
            },
        )
        execution_id = context.create_generation()
        _wait_for_path(started)
        first = _wait_for_claim(execution_id, expected_generation=1)
        worker_b = _start_v3_worker_with_env(
            context.root, {"VORA_E2E_LEASE_SECONDS": "2"}
        )
        _wait_for_lease_extension(first.id, first.lease_expires_at)
        celery_app.send_task(
            "app.generation_tasks.relay_generation_intents",
            queue=main.settings.generation_queue,
        )
        _wait_for_claim(execution_id, expected_generation=1)
        release.touch()
        _wait_for_workflow(execution_id, WorkflowExecutionStatus.WAITING_APPROVAL)
        with Session(engine) as session:
            intent = session.get(WorkflowDispatchIntent, first.id)
            assert intent is not None and intent.status == "COMPLETED"
            assert intent.claim_generation == 1
    finally:
        _stop_worker(worker_b)
        _stop_worker(worker_a)
        context.cleanup()


def test_real_worker_fencing_blocks_resumed_stale_claim_result() -> None:
    """Worker A resumes after Worker B reclaims, but its stale result cannot be persisted."""
    context = _V3E2EContext.create()
    worker_a: subprocess.Popen[bytes] | None = None
    worker_b: subprocess.Popen[bytes] | None = None
    try:
        started, release = context.root / "fence-started", context.root / "fence-release"
        worker_a = _start_v3_worker_with_env(
            context.root,
            {
                "VORA_E2E_BLOCK_NODE": "TEXT_GENERATE",
                "VORA_E2E_GATE_STARTED_PATH": str(started),
                "VORA_E2E_GATE_RELEASE_PATH": str(release),
                "VORA_E2E_DISABLE_HEARTBEAT": "true",
                "VORA_E2E_LEASE_SECONDS": "2",
            },
        )
        execution_id = context.create_generation()
        _wait_for_path(started)
        first = _wait_for_claim(execution_id, expected_generation=1)
        first_task_id = first.celery_task_id
        _wait_for_lease_expiry(first.id)
        worker_b = _start_v3_worker_with_env(
            context.root, {"VORA_E2E_LEASE_SECONDS": "2"}
        )
        celery_app.send_task(
            "app.generation_tasks.relay_generation_intents",
            queue=main.settings.generation_queue,
        )
        _wait_for_workflow(execution_id, WorkflowExecutionStatus.WAITING_APPROVAL)
        second = _wait_for_claim(execution_id, expected_generation=2, completed=True)
        assert second.claim_token != first.claim_token
        with Session(engine) as session:
            text = session.scalar(
                select(NodeExecution).where(
                    NodeExecution.workflow_execution_id == execution_id,
                    NodeExecution.node_type == "TEXT_GENERATE",
                )
            )
            assert text is not None
            output_before_release = text.output_data
        release.touch()
        _wait_for_stale_worker_result_to_be_rejected(first_task_id)
        with Session(engine) as session:
            intent = session.get(WorkflowDispatchIntent, first.id)
            text = session.scalar(
                select(NodeExecution).where(
                    NodeExecution.workflow_execution_id == execution_id,
                    NodeExecution.node_type == "TEXT_GENERATE",
                )
            )
            attempts = list(
                session.scalars(
                    select(NodeExecutionAttempt)
                    .join(NodeExecution)
                    .where(
                        NodeExecution.workflow_execution_id == execution_id,
                        NodeExecution.node_type == "TEXT_GENERATE",
                    )
                )
            )
            assert intent is not None and intent.status == "COMPLETED"
            assert intent.claim_generation == 2
            assert text is not None and text.output_data == output_before_release
            assert len(attempts) == 1
    finally:
        _stop_worker(worker_b)
        _stop_worker(worker_a)
        context.cleanup()


def test_e2e_fake_v3_composition_blocks_vendor_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit E2E flag wires every V3 vendor boundary to an offline adapter."""
    assert main.settings.e2e_fake_providers is True

    def unexpected_vendor_construction(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("an offline E2E run must not construct a vendor provider")

    monkeypatch.setattr(main, "ChatOpenAI", unexpected_vendor_construction)
    monkeypatch.setattr(main, "OpenAIImageProvider", unexpected_vendor_construction)
    with Session(engine) as session:
        services = main.get_ai_workflow_services(session)
        workflow_engine = main.get_workflow_engine(session, services, workflow_version=3)
        executor = workflow_engine._executor
        assert isinstance(executor, V3NodeExecutor)
        assert isinstance(executor._image_provider, DeterministicE2EImageProvider)
        assert executor._content_plan_model_factory is None
        assert executor._text_generate_model_factory is None
        assert executor._text_check_model_factory is None
        assert executor._image_check_model_factory is None
        assert executor._content_plan_model is not None
        assert executor._text_generate_model is not None
        assert executor._text_check_model is not None
        assert executor._image_check_model is not None
        publication = get_publication_service(session)
        assert isinstance(
            publication._publisher_factory(SocialPlatform.INSTAGRAM),
            DeterministicE2EInstagramPublisher,
        )


@dataclass
class _V3E2EContext:
    root: Path
    rules_id: int
    version: int
    created_dev_user: bool
    original_uploads_root: Path
    execution_id: int | None = None

    @classmethod
    def create(cls) -> "_V3E2EContext":
        root = (Path("uploads") / f"v3-celery-e2e-{uuid4()}").resolve()
        version = int(uuid4().hex[:7], 16)
        created_dev_user = False
        with Session(engine) as session:
            user = session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
            if user is None:
                session.add(User(email=DEV_USER_EMAIL, name="V3 Celery E2E"))
                created_dev_user = True
            rules = RulesVersion(version=version, rules_data={"e2e": True})
            session.add(rules)
            session.add_all(
                [
                    PromptVersion(
                        node_type="CONTENT_PLAN",
                        version=version,
                        prompt_template="Request: {request_text}\nRules: {rules_data}",
                    ),
                    PromptVersion(
                        node_type="TEXT_GENERATE",
                        version=version,
                        prompt_template="Message: {core_message}\nSlides: {slide_structure}",
                    ),
                    PromptVersion(
                        node_type="TEXT_CHECK",
                        version=version,
                        prompt_template="Slides: {slides}\nRules: {rules_data}",
                    ),
                    PromptVersion(
                        node_type="IMAGE_CHECK",
                        version=version,
                        prompt_template="Slides: {slides}\nAssets: {asset_ids}",
                    ),
                ]
            )
            session.commit()
            rules_id = rules.id
        original_uploads_root = main.settings.uploads_root
        main.settings.uploads_root = root
        from app import workflow_execution_service

        workflow_execution_service.UPLOADS_ROOT = root
        return cls(root, rules_id, version, created_dev_user, original_uploads_root)

    def create_generation(self) -> int:
        with TestClient(main.app) as client:
            response = client.post(
                "/workflow-executions",
                data={
                    "request_text": "offline carousel",
                    "marketing_info": "details",
                    "theme": "clean",
                    "rules_version_id": str(self.rules_id),
                },
                files={"source_image": ("source.png", DeterministicE2EImageProvider._PNG, "image/png")},
            )
        assert response.status_code == 202
        self.execution_id = response.json()["workflow_execution_id"]
        return self.execution_id

    def cleanup(self) -> None:
        from app import workflow_execution_service

        if self.execution_id is not None:
            _delete_execution(self.execution_id)
        with Session(engine) as session:
            session.execute(delete(PromptVersion).where(PromptVersion.version == self.version))
            session.execute(delete(RulesVersion).where(RulesVersion.version == self.version))
            if self.created_dev_user:
                session.execute(delete(User).where(User.email == DEV_USER_EMAIL))
            session.commit()
        main.settings.uploads_root = self.original_uploads_root
        workflow_execution_service.UPLOADS_ROOT = self.original_uploads_root
        rmtree(self.root, ignore_errors=True)


def _wait_for_workflow(
    workflow_execution_id: int,
    status: WorkflowExecutionStatus,
    revision_no: int | None = None,
    timeout_seconds: float = 20,
) -> WorkflowExecution:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with Session(engine) as session:
            workflow = session.get(WorkflowExecution, workflow_execution_id)
            if workflow is not None and workflow.status is status:
                if revision_no is None:
                    session.expunge(workflow)
                    return workflow
                waiting = _waiting_node(session, workflow_execution_id)
                if waiting.revision_no == revision_no:
                    session.expunge(workflow)
                    return workflow
        time.sleep(0.25)
    raise AssertionError(f"workflow {workflow_execution_id} did not reach {status.value}")


def _wait_for_path(path: Path, timeout_seconds: float = 20) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"worker did not reach E2E gate {path}")


def _wait_for_claim(
    workflow_execution_id: int,
    *,
    expected_generation: int,
    completed: bool = False,
    timeout_seconds: float = 20,
) -> WorkflowDispatchIntent:
    deadline = time.monotonic() + timeout_seconds
    expected_status = "COMPLETED" if completed else "CLAIMED"
    while time.monotonic() < deadline:
        with Session(engine) as session:
            intent = session.scalar(
                select(WorkflowDispatchIntent)
                .where(WorkflowDispatchIntent.workflow_execution_id == workflow_execution_id)
                .order_by(WorkflowDispatchIntent.id.desc())
            )
            if (
                intent is not None
                and intent.status == expected_status
                and intent.claim_generation == expected_generation
            ):
                session.expunge(intent)
                return intent
        time.sleep(0.05)
    raise AssertionError(
        f"workflow {workflow_execution_id} did not reach claim generation {expected_generation}"
    )


def _wait_for_lease_expiry(intent_id: int, timeout_seconds: float = 10) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_expiry: datetime | None = None
    while time.monotonic() < deadline:
        with Session(engine) as session:
            intent = session.get(WorkflowDispatchIntent, intent_id)
            if (
                intent is not None
                and intent.lease_expires_at is not None
                and intent.lease_expires_at <= datetime.now(UTC)
            ):
                return
            if intent is not None:
                last_expiry = intent.lease_expires_at
        time.sleep(0.05)
    raise AssertionError(
        f"intent {intent_id} lease did not expire: expiry={last_expiry}, now={datetime.now(UTC)}"
    )


def _wait_for_lease_extension(
    intent_id: int, original_expiry: datetime | None, timeout_seconds: float = 10
) -> None:
    assert original_expiry is not None
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with Session(engine) as session:
            intent = session.get(WorkflowDispatchIntent, intent_id)
            if (
                intent is not None
                and intent.lease_expires_at is not None
                and intent.lease_expires_at > original_expiry
            ):
                return
        time.sleep(0.05)
    raise AssertionError(f"intent {intent_id} heartbeat did not extend its lease")


def _wait_for_stale_worker_result_to_be_rejected(
    task_id: str | None, timeout_seconds: float = 20
) -> None:
    assert task_id is not None
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if celery_app.AsyncResult(task_id).status == "FAILURE":
            return
        time.sleep(0.05)
    raise AssertionError(f"stale task {task_id} did not report fenced failure")


def _wait_for_task_status(task_id: str, expected_status: str, timeout_seconds: float = 20) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if celery_app.AsyncResult(task_id).status == expected_status:
            return
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach {expected_status}")


def _wait_for_publication(
    publication_id: int, timeout_seconds: float = 20
) -> SocialPublication:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with Session(engine) as session:
            publication = session.get(SocialPublication, publication_id)
            if publication is not None and publication.status is SocialPublicationStatus.PUBLISHED:
                session.expunge(publication)
                return publication
        time.sleep(0.25)
    with Session(engine) as session:
        publication = session.get(SocialPublication, publication_id)
        attempts = list(
            session.scalars(
                select(SocialPublicationAttempt).where(
                    SocialPublicationAttempt.social_publication_id == publication_id
                )
            )
        )
        raise AssertionError(
            f"publication {publication_id} did not reach PUBLISHED: "
            f"status={publication.status.value if publication else None}, "
            f"attempts={[(item.status.value, item.error_code, item.error_message) for item in attempts]}"
        )


def _wait_for_publication_failure(
    publication_id: int, timeout_seconds: float = 20
) -> SocialPublication:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with Session(engine) as session:
            publication = session.get(SocialPublication, publication_id)
            if publication is not None and publication.status is SocialPublicationStatus.FAILED:
                session.expunge(publication)
                return publication
        time.sleep(0.25)
    raise AssertionError(f"publication {publication_id} did not reach FAILED")


def _waiting_node(session: Session, workflow_execution_id: int) -> NodeExecution:
    node = session.scalar(
        select(NodeExecution)
        .where(
            NodeExecution.workflow_execution_id == workflow_execution_id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
            NodeExecution.status == NodeExecutionStatus.WAITING,
        )
        .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
    )
    assert node is not None
    return node


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
        start_new_session=os.name != "nt",
    )


def _start_v3_worker(uploads_root: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.update({"VORA_E2E_FAKE_PROVIDERS": "true", "UPLOADS_ROOT": str(uploads_root)})
    return _start_v3_worker_with_env(uploads_root, env)


def _start_v3_worker_with_env(
    uploads_root: Path, extra_env: dict[str, str]
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.update({"VORA_E2E_FAKE_PROVIDERS": "true", "UPLOADS_ROOT": str(uploads_root)})
    env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-m", "celery", "-A", "app.celery_app", "worker", "--pool=solo", "--loglevel=WARNING", "--queues", main.settings.generation_queue, "--hostname", f"vora-v3-test-{uuid4()}@%h"],
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )


def _start_publication_worker(uploads_root: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.update({"VORA_E2E_FAKE_PROVIDERS": "true", "UPLOADS_ROOT": str(uploads_root)})
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
            main.settings.publication_queue,
            "--hostname",
            f"vora-publication-test-{uuid4()}@%h",
        ],
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
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
            NodeExecution.node_type == key,
        )
    )
    assert node is not None
    return node


def _delete_execution(workflow_execution_id: int) -> None:
    """Remove only rows created by this E2E from the already validated test database."""
    with Session(engine) as session:
        node_ids = select(NodeExecution.id).where(NodeExecution.workflow_execution_id == workflow_execution_id)
        approval_ids = select(UserApproval.id).where(UserApproval.node_execution_id.in_(node_ids))
        publication_ids = select(SocialPublication.id).where(
            SocialPublication.node_execution_id.in_(node_ids)
        )
        session.execute(
            delete(SocialPublicationAttempt).where(
                SocialPublicationAttempt.social_publication_id.in_(publication_ids)
            )
        )
        session.execute(delete(SocialPublication).where(SocialPublication.id.in_(publication_ids)))
        session.execute(delete(Revision).where(Revision.user_approval_id.in_(approval_ids)))
        session.execute(delete(UserApproval).where(UserApproval.node_execution_id.in_(node_ids)))
        session.execute(delete(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id.in_(node_ids)))
        session.execute(
            delete(WorkflowDispatchIntent).where(
                WorkflowDispatchIntent.workflow_execution_id == workflow_execution_id
            )
        )
        # The input snapshot references its source FileAsset.
        session.execute(
            delete(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id == workflow_execution_id
            )
        )
        session.execute(delete(FileAsset).where(FileAsset.workflow_execution_id == workflow_execution_id))
        session.execute(delete(NodeExecution).where(NodeExecution.workflow_execution_id == workflow_execution_id))
        session.execute(delete(WorkflowExecution).where(WorkflowExecution.id == workflow_execution_id))
        session.commit()


def _delete_social_connection(connection_id: int) -> None:
    with Session(engine) as session:
        session.execute(delete(SocialAccountConnection).where(SocialAccountConnection.id == connection_id))
        session.commit()


def _stop_worker(worker: subprocess.Popen[bytes] | None) -> None:
    if worker is None or worker.poll() is not None:
        return
    if os.name == "nt":
        worker.terminate()
    else:
        os.killpg(os.getpgid(worker.pid), signal.SIGTERM)
    try:
        worker.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            worker.kill()
        else:
            os.killpg(os.getpgid(worker.pid), signal.SIGKILL)
        worker.wait(timeout=10)


def _kill_worker_tree(worker: subprocess.Popen[bytes] | None) -> None:
    """Terminate only the dedicated Celery worker process tree on each test OS."""
    assert worker is not None
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(worker.pid), "/T", "/F"],
            check=True,
            capture_output=True,
        )
    else:
        # Workers are started in a new session, so this cannot terminate pytest.
        os.killpg(os.getpgid(worker.pid), signal.SIGKILL)
    worker.wait(timeout=10)
