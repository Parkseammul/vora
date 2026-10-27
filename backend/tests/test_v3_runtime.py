from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableLambda
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import main
from app.database import engine
from app.generation_dispatch import (
    ACTION_RESUME,
    ACTION_REVISION,
    ACTION_START,
    CLAIMED,
    COMPLETED,
    PENDING,
    QUEUED,
    GenerationDispatchService,
    execute_claimed_intent,
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
    SocialPublication,
    User,
    UserApproval,
    WorkflowDispatchIntent,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.node_executors import NodeExecutionResult
from app.v3_runtime import (
    ContentPlanOutput,
    ImageCheckOutput,
    TextCheckOutput,
    TextGenerateOutput,
    V3NodeExecutor,
)
from app.workflow_definitions import workflow_registry
from app.workflow_engine import WorkflowEngine


class FakeImageProvider:
    def __init__(self, image: bytes = b"generated-image") -> None:
        self.image = image
        self.calls: list[tuple[str, Path]] = []

    def generate(self, *, prompt: str, source_image: Path) -> bytes:
        self.calls.append((prompt, source_image))
        return self.image


class FailingImageProvider:
    def generate(self, *, prompt: str, source_image: Path) -> bytes:
        del prompt, source_image
        raise RuntimeError("image provider unavailable")


class FakeStorage:
    def __init__(self, fail_upload: bool = False) -> None:
        self.files: dict[str, bytes] = {"tests/source.png": b"source-image"}
        self.fail_upload = fail_upload

    def local_path(self, storage_key: str) -> Path | None:
        return None

    def materialize_private(self, storage_key: str, destination: Path) -> None:
        destination.write_bytes(self.files.get(storage_key, b"source-image"))

    def upload_private(self, storage_key: str, source: Path, content_type: str) -> None:
        if self.fail_upload:
            raise OSError("storage unavailable")
        self.files[storage_key] = source.read_bytes()

    def presigned_get_url(self, storage_key: str, expires_seconds: int = 3600) -> str:
        raise NotImplementedError


def _image_executor_kwargs() -> dict[str, object]:
    return {
        "image_provider": FakeImageProvider(),
        "image_model_name": "test-image-model",
        "storage": FakeStorage(),
        "image_check_model": RunnableLambda(_image_check_model),
        "image_check_model_name": "test-image-check-model",
    }


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


PROMPT_TEMPLATE = (
    "Request: {request_text}\nMarketing: {marketing_info}\nTheme: {theme}\n"
    "Rules: {rules_data}\nOriginal input: {input_data}"
)
TEXT_GENERATE_PROMPT_TEMPLATE = (
    "Message: {core_message}\nSlides: {slide_structure}\n"
    "Rules: {rules_data}\nOriginal input: {input_data}"
)
TEXT_CHECK_PROMPT_TEMPLATE = "Slides: {slides}\nRules: {rules_data}\nInput: {input_data}"
IMAGE_CHECK_PROMPT_TEMPLATE = (
    "Slides: {slides}\nRules: {rules_data}\nAssets: {asset_ids}\nInput: {input_data}"
)


def _content_plan_model(_: object) -> ContentPlanOutput:
    return ContentPlanOutput(
        core_message="A focused message",
        slide_structure=["hook", "point 2", "point 3", "point 4", "call to action"],
    )


def _failing_content_plan_model(_: object) -> ContentPlanOutput:
    raise RuntimeError("CONTENT_PLAN model unavailable")


def _text_generate_model(_: object) -> TextGenerateOutput:
    return TextGenerateOutput(
        slides=[
            {"slide_no": 1, "text": "Headline"},
            {"slide_no": 2, "text": "Supporting copy"},
            {"slide_no": 3, "text": "Benefit"},
            {"slide_no": 4, "text": "Proof"},
            {"slide_no": 5, "text": "Call to action"},
        ]
    )


def _failing_text_generate_model(_: object) -> TextGenerateOutput:
    raise RuntimeError("TEXT_GENERATE model unavailable")


def _text_check_model(_: object) -> TextCheckOutput:
    return TextCheckOutput(passed=True, violations=[], feedback=None)


def _failed_text_check_model(_: object) -> TextCheckOutput:
    return TextCheckOutput(
        passed=False,
        violations=["Forbidden word used"],
        feedback="Remove the forbidden word.",
    )


def _failing_text_check_model(_: object) -> TextCheckOutput:
    raise RuntimeError("TEXT_CHECK model unavailable")


def _image_check_model(_: object) -> ImageCheckOutput:
    return ImageCheckOutput(passed=True, violations=[], feedback=None)


def _failed_image_check_model(_: object) -> ImageCheckOutput:
    return ImageCheckOutput(
        passed=False,
        violations=["Product appearance differs from the reference"],
        feedback="Use the reference product image more faithfully.",
    )


def _failing_image_check_model(_: object) -> ImageCheckOutput:
    raise RuntimeError("IMAGE_CHECK model unavailable")


def _v3_input_contract(
    session: Session, execution: WorkflowExecution, version: int = 1
) -> None:
    rules = RulesVersion(version=version, rules_data={"tone": "clear"})
    content_plan_prompt = PromptVersion(
        node_type="CONTENT_PLAN", version=version, prompt_template=PROMPT_TEMPLATE
    )
    text_generate_prompt = PromptVersion(
        node_type="TEXT_GENERATE", version=version, prompt_template=TEXT_GENERATE_PROMPT_TEMPLATE
    )
    text_check_prompt = PromptVersion(
        node_type="TEXT_CHECK", version=version, prompt_template=TEXT_CHECK_PROMPT_TEMPLATE
    )
    image_check_prompt = PromptVersion(
        node_type="IMAGE_CHECK", version=version, prompt_template=IMAGE_CHECK_PROMPT_TEMPLATE
    )
    session.add_all(
        [rules, content_plan_prompt, text_generate_prompt, text_check_prompt, image_check_prompt]
    )
    session.flush()
    source_asset = FileAsset(
        workflow_execution_id=execution.id,
        created_by_node_execution_id=None,
        asset_type=AssetType.IMAGE,
        storage_key=f"tests/{uuid4()}.png",
        file_name="input.png",
        mime_type="image/png",
        file_size=1,
        slide_no=None,
    )
    session.add(source_asset)
    session.flush()
    session.add(
        ExecutionInputSnapshot(
            workflow_execution_id=execution.id,
            request_text="Plan a launch",
            marketing_info="Product details",
            theme="Minimal",
            source_asset_id=source_asset.id,
            rules_version_id=rules.id,
        )
    )
    session.commit()


def _v3_engine_with_image_check(
    session: Session, image_check_model: RunnableLambda
) -> WorkflowEngine:
    return WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            content_plan_model=RunnableLambda(_content_plan_model),
            text_generate_model=RunnableLambda(_text_generate_model),
            text_check_model=RunnableLambda(_text_check_model),
            image_check_model=image_check_model,
            **{
                key: value
                for key, value in _image_executor_kwargs().items()
                if key not in {"image_check_model", "image_check_model_name"}
            },
        ),
    )


def _approval_client(
    session: Session, workflow_engine: WorkflowEngine, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    def get_test_session() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_session] = get_test_session
    monkeypatch.setattr(main, "get_ai_workflow_services", lambda _: object())
    monkeypatch.setattr(main, "get_workflow_engine", lambda *_args: workflow_engine)
    monkeypatch.setattr(
        main.celery_app,
        "send_task",
        lambda *_args, **_kwargs: SimpleNamespace(id="test-generation-task"),
    )
    return TestClient(main.app)


def _consume_latest_v3_dispatch(
    session: Session, workflow_engine: WorkflowEngine, workflow_execution_id: int
) -> WorkflowDispatchIntent:
    """Consume the persisted intent through the production dispatch boundary."""
    intent = session.scalar(
        select(WorkflowDispatchIntent)
        .where(WorkflowDispatchIntent.workflow_execution_id == workflow_execution_id)
        .order_by(WorkflowDispatchIntent.id.desc())
    )
    assert intent is not None
    dispatcher = GenerationDispatchService(session, Mock(), "test_generation")
    claimed = dispatcher.claim(intent.id)
    assert claimed is not None
    execute_claimed_intent(session, claimed, workflow_engine, lambda _id: {})
    assert claimed.claim_token is not None
    assert dispatcher.complete(claimed.id, claimed.claim_token, claimed.claim_generation)
    session.refresh(intent)
    assert intent.status == COMPLETED
    return intent


def _cleanup_independent_v3_fixture(workflow_execution_id: int, version: int) -> None:
    """Remove only rows owned by an independently committed visibility test."""
    with Session(engine) as cleanup:
        node_ids = select(NodeExecution.id).where(
            NodeExecution.workflow_execution_id == workflow_execution_id
        )
        cleanup.execute(delete(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id.in_(node_ids)))
        approval_ids = select(UserApproval.id).where(
            UserApproval.workflow_execution_id == workflow_execution_id
        )
        cleanup.execute(delete(Revision).where(Revision.user_approval_id.in_(approval_ids)))
        cleanup.execute(delete(UserApproval).where(UserApproval.workflow_execution_id == workflow_execution_id))
        cleanup.execute(delete(WorkflowDispatchIntent).where(WorkflowDispatchIntent.workflow_execution_id == workflow_execution_id))
        cleanup.execute(delete(ExecutionInputSnapshot).where(ExecutionInputSnapshot.workflow_execution_id == workflow_execution_id))
        cleanup.execute(delete(FileAsset).where(FileAsset.workflow_execution_id == workflow_execution_id))
        cleanup.execute(delete(NodeExecution).where(NodeExecution.workflow_execution_id == workflow_execution_id))
        user_id = cleanup.scalar(select(WorkflowExecution.user_id).where(WorkflowExecution.id == workflow_execution_id))
        cleanup.execute(delete(WorkflowExecution).where(WorkflowExecution.id == workflow_execution_id))
        if user_id is not None:
            cleanup.execute(delete(User).where(User.id == user_id))
        cleanup.execute(delete(PromptVersion).where(PromptVersion.version == version))
        cleanup.execute(delete(RulesVersion).where(RulesVersion.version == version))
        cleanup.commit()


def test_v3_chain_reaches_human_approval_with_runnable_attempts(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            content_plan_model=RunnableLambda(_content_plan_model),
            content_plan_model_name="test-content-plan-model",
            text_generate_model=RunnableLambda(_text_generate_model),
            text_generate_model_name="test-text-generate-model",
            text_check_model=RunnableLambda(_text_check_model),
            text_check_model_name="test-text-check-model",
            **_image_executor_kwargs(),
        ),
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
    assert all(attempt.error_code is None for attempt in attempts)
    assert all(attempt.latency_ms is not None for attempt in attempts)
    assert attempts[0].prompt_version_id is not None
    assert attempts[0].ai_company == "OPENAI"
    assert attempts[0].model == "test-content-plan-model"
    assert attempts[1].prompt_version_id is not None
    assert attempts[1].ai_company == "OPENAI"
    assert attempts[1].model == "test-text-generate-model"
    assert attempts[2].prompt_version_id is not None
    assert attempts[2].ai_company == "OPENAI"
    assert attempts[2].model == "test-text-check-model"


def test_v3_chain_failure_marks_attempt_node_and_workflow_failed(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
            session,
            workflow_registry,
            V3NodeExecutor(
                session=session,
                fail_node="TEXT_GENERATE",
                content_plan_model=RunnableLambda(_content_plan_model),
            ),
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
    assert attempt.error_code == "INTERNAL_ERROR"
    assert attempt.latency_ms is not None


def test_v3_content_plan_uses_prompt_version_and_persists_structured_output(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            fail_node="TEXT_GENERATE",
            content_plan_model=RunnableLambda(_content_plan_model),
            content_plan_model_name="test-content-plan-model",
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    content_plan = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "CONTENT_PLAN",
        )
    )
    assert content_plan is not None
    assert content_plan.output_data == {
        "core_message": "A focused message",
            "slide_structure": ["hook", "point 2", "point 3", "point 4", "call to action"],
            "slides": [
                {"slide_no": 1, "purpose": "hook"},
                {"slide_no": 2, "purpose": "point 2"},
                {"slide_no": 3, "purpose": "point 3"},
                {"slide_no": 4, "purpose": "point 4"},
                {"slide_no": 5, "purpose": "call to action"},
        ],
    }
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == content_plan.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.SUCCEEDED
    assert attempt.prompt_version_id is not None
    assert attempt.metadata_ == {"runtime": "v3-langchain", "prompt_version": 1}


def test_v3_content_plan_model_failure_marks_attempt_node_and_workflow_failed(
    session: Session,
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(session=session, content_plan_model=RunnableLambda(_failing_content_plan_model)),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    content_plan = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "CONTENT_PLAN",
        )
    )
    assert content_plan is not None
    assert content_plan.status is NodeExecutionStatus.FAILED
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == content_plan.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.FAILED


def test_v3_text_generate_uses_prompt_version_and_persists_structured_output(
    session: Session,
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            fail_node="TEXT_CHECK",
            content_plan_model=RunnableLambda(_content_plan_model),
            text_generate_model=RunnableLambda(_text_generate_model),
            text_generate_model_name="test-text-generate-model",
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    text_generate = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "TEXT_GENERATE",
        )
    )
    assert text_generate is not None
    assert text_generate.output_data == {
        "slides": [
                {"slide_no": 1, "text": "Headline"},
                {"slide_no": 2, "text": "Supporting copy"},
                {"slide_no": 3, "text": "Benefit"},
                {"slide_no": 4, "text": "Proof"},
                {"slide_no": 5, "text": "Call to action"},
        ]
    }
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == text_generate.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.SUCCEEDED
    assert attempt.prompt_version_id is not None
    assert attempt.ai_company == "OPENAI"
    assert attempt.model == "test-text-generate-model"
    assert attempt.metadata_ == {"runtime": "v3-langchain", "prompt_version": 1}


def test_v3_text_generate_model_failure_marks_attempt_node_and_workflow_failed(
    session: Session,
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            content_plan_model=RunnableLambda(_content_plan_model),
            text_generate_model=RunnableLambda(_failing_text_generate_model),
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    text_generate = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "TEXT_GENERATE",
        )
    )
    assert text_generate is not None
    assert text_generate.status is NodeExecutionStatus.FAILED
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == text_generate.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.FAILED


def test_v3_text_check_persists_passed_result_and_attempt_provenance(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            fail_node="IMAGE_GENERATE",
            content_plan_model=RunnableLambda(_content_plan_model),
            text_generate_model=RunnableLambda(_text_generate_model),
            text_check_model=RunnableLambda(_text_check_model),
            text_check_model_name="test-text-check-model",
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    text_check = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "TEXT_CHECK",
        )
    )
    assert text_check is not None
    assert text_check.output_data == {
        "slides": [
                {"slide_no": 1, "text": "Headline"},
                {"slide_no": 2, "text": "Supporting copy"},
                {"slide_no": 3, "text": "Benefit"},
                {"slide_no": 4, "text": "Proof"},
                {"slide_no": 5, "text": "Call to action"},
        ],
        "passed": True,
        "violations": [],
        "feedback": None,
    }
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == text_check.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.SUCCEEDED
    assert attempt.prompt_version_id is not None
    assert attempt.ai_company == "OPENAI"
    assert attempt.model == "test-text-check-model"


def test_v3_text_check_failed_review_is_a_successful_node_execution(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            fail_node="IMAGE_GENERATE",
            content_plan_model=RunnableLambda(_content_plan_model),
            text_generate_model=RunnableLambda(_text_generate_model),
            text_check_model=RunnableLambda(_failed_text_check_model),
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    text_check = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "TEXT_CHECK",
        )
    )
    assert text_check is not None
    assert text_check.status is NodeExecutionStatus.SUCCEEDED
    assert text_check.output_data is not None
    assert text_check.output_data["passed"] is False
    assert text_check.output_data["violations"] == ["Forbidden word used"]
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == text_check.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.SUCCEEDED


def test_v3_text_check_model_failure_marks_attempt_node_and_workflow_failed(
    session: Session,
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            content_plan_model=RunnableLambda(_content_plan_model),
            text_generate_model=RunnableLambda(_text_generate_model),
            text_check_model=RunnableLambda(_failing_text_check_model),
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "ignored"})

    assert status is WorkflowExecutionStatus.FAILED
    text_check = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "TEXT_CHECK",
        )
    )
    assert text_check is not None
    assert text_check.status is NodeExecutionStatus.FAILED
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == text_check.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.FAILED


def test_image_generate_persists_each_generated_file_asset(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    provider = FakeImageProvider(b"generated-png")
    storage = FakeStorage()
    executor = V3NodeExecutor(
        session=session,
        image_provider=provider,
        image_model_name="test-image-model",
        storage=storage,
    )
    node = NodeExecution(
        workflow_execution_id=execution.id,
        node_type="IMAGE_GENERATE",
        revision_no=0,
        input_data={},
        status=NodeExecutionStatus.RUNNING,
    )
    session.add(node)
    session.flush()

    result = executor.execute(
        "IMAGE_GENERATE",
        {"slides": [{"slide_no": 1, "text": "Headline"}]},
        workflow_execution_id=execution.id,
        node_execution_id=node.id,
    )

    assert isinstance(result, NodeExecutionResult)
    assert result.output_data["asset_ids"]
    asset = session.get(FileAsset, result.output_data["asset_ids"][0])
    assert asset is not None
    assert asset.slide_no == 1
    assert asset.workflow_execution_id == execution.id
    assert asset.created_by_node_execution_id == node.id
    assert asset.mime_type == "image/png"
    assert asset.file_size == len(b"generated-png")
    assert storage.files[asset.storage_key] == b"generated-png"
    assert "Headline" in provider.calls[0][0]


def test_image_generate_storage_failure_does_not_create_asset(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    node = NodeExecution(
        workflow_execution_id=execution.id,
        node_type="IMAGE_GENERATE",
        revision_no=0,
        input_data={},
        status=NodeExecutionStatus.RUNNING,
    )
    session.add(node)
    session.flush()
    executor = V3NodeExecutor(
        session=session,
        image_provider=FakeImageProvider(),
        storage=FakeStorage(fail_upload=True),
    )

    with pytest.raises(OSError, match="storage unavailable"):
        executor.execute(
            "IMAGE_GENERATE",
            {"slides": [{"slide_no": 1, "text": "Headline"}]},
            workflow_execution_id=execution.id,
            node_execution_id=node.id,
        )
    assert session.scalar(select(FileAsset).where(FileAsset.created_by_node_execution_id == node.id)) is None


def test_image_generate_provider_failure_does_not_create_asset(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    node = NodeExecution(
        workflow_execution_id=execution.id,
        node_type="IMAGE_GENERATE",
        revision_no=0,
        input_data={},
        status=NodeExecutionStatus.RUNNING,
    )
    session.add(node)
    session.flush()
    executor = V3NodeExecutor(
        session=session,
        image_provider=FailingImageProvider(),
        storage=FakeStorage(),
    )

    with pytest.raises(RuntimeError, match="image provider unavailable"):
        executor.execute(
            "IMAGE_GENERATE",
            {"slides": [{"slide_no": 1, "text": "Headline"}]},
            workflow_execution_id=execution.id,
            node_execution_id=node.id,
        )
    assert session.scalar(select(FileAsset).where(FileAsset.created_by_node_execution_id == node.id)) is None


def test_image_generate_retry_reuses_asset_row_and_storage_key(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    node = NodeExecution(
        workflow_execution_id=execution.id,
        node_type="IMAGE_GENERATE",
        revision_no=0,
        input_data={},
        status=NodeExecutionStatus.RUNNING,
    )
    session.add(node)
    session.flush()
    storage = FakeStorage()
    executor = V3NodeExecutor(session=session, image_provider=FakeImageProvider(), storage=storage)
    payload = {"slides": [{"slide_no": 1, "text": "Headline"}]}

    first = executor.execute("IMAGE_GENERATE", payload, workflow_execution_id=execution.id, node_execution_id=node.id)
    second = executor.execute("IMAGE_GENERATE", payload, workflow_execution_id=execution.id, node_execution_id=node.id)

    assert first.output_data["asset_ids"] == second.output_data["asset_ids"]
    assets = session.scalars(
        select(FileAsset).where(FileAsset.created_by_node_execution_id == node.id)
    ).all()
    assert len(assets) == 1


def test_image_check_reads_generated_assets_and_persists_structured_result(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    storage = FakeStorage()
    image_node = NodeExecution(
        workflow_execution_id=execution.id,
        node_type="IMAGE_GENERATE",
        revision_no=0,
        input_data={},
        status=NodeExecutionStatus.RUNNING,
    )
    session.add(image_node)
    session.flush()
    executor = V3NodeExecutor(
        session=session,
        image_provider=FakeImageProvider(b"slide-one"),
        storage=storage,
        image_check_model=RunnableLambda(_image_check_model),
        image_check_model_name="test-image-check-model",
    )
    generated = executor.execute(
        "IMAGE_GENERATE",
        {"slides": [{"slide_no": 1, "text": "Headline"}, {"slide_no": 2, "text": "Copy"}]},
        workflow_execution_id=execution.id,
        node_execution_id=image_node.id,
    )
    assert isinstance(generated, NodeExecutionResult)

    checked = executor.execute(
        "IMAGE_CHECK", generated.output_data, workflow_execution_id=execution.id
    )

    assert isinstance(checked, NodeExecutionResult)
    assert checked.output_data == {
        "asset_ids": generated.output_data["asset_ids"],
        "passed": True,
        "violations": [],
        "feedback": None,
    }
    assert checked.attempt_metadata == {"runtime": "v3-langchain-vision", "prompt_version": 1}
    assert checked.model == "test-image-check-model"


def test_image_check_rejection_preserves_feedback_and_still_reaches_human_approval(
    session: Session,
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            content_plan_model=RunnableLambda(_content_plan_model),
            text_generate_model=RunnableLambda(_text_generate_model),
            text_check_model=RunnableLambda(_text_check_model),
            image_check_model=RunnableLambda(_failed_image_check_model),
            **{
                key: value
                for key, value in _image_executor_kwargs().items()
                if key not in {"image_check_model", "image_check_model_name"}
            },
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "fake"})

    assert status is WorkflowExecutionStatus.WAITING_APPROVAL
    image_check = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_CHECK",
        )
    )
    assert image_check is not None
    assert image_check.output_data is not None
    assert image_check.output_data["passed"] is False
    assert image_check.output_data["feedback"] == "Use the reference product image more faithfully."


def test_image_check_missing_file_and_provider_error_fail_explicitly(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    storage = FakeStorage()
    executor = V3NodeExecutor(
        session=session,
        storage=storage,
        image_check_model=RunnableLambda(_failing_image_check_model),
    )

    with pytest.raises(ValueError, match="generated image asset does not exist"):
        executor.execute(
            "IMAGE_CHECK", {"asset_ids": [999999], "slides": []}, workflow_execution_id=execution.id
        )

    generated = FileAsset(
        workflow_execution_id=execution.id,
        created_by_node_execution_id=None,
        asset_type=AssetType.IMAGE,
        storage_key="missing/generated.png",
        file_name="generated.png",
        mime_type="image/png",
        file_size=1,
        slide_no=1,
    )
    session.add(generated)
    session.flush()

    class MissingStorage(FakeStorage):
        def materialize_private(self, storage_key: str, destination: Path) -> None:
            raise FileNotFoundError("storage image was not found")

    missing_file_executor = V3NodeExecutor(
        session=session,
        storage=MissingStorage(),
        image_check_model=RunnableLambda(_image_check_model),
    )
    with pytest.raises(FileNotFoundError, match="storage"):
        missing_file_executor.execute(
            "IMAGE_CHECK", {"asset_ids": [generated.id], "slides": []}, workflow_execution_id=execution.id
        )
    with pytest.raises(RuntimeError, match="IMAGE_CHECK model unavailable"):
        executor.execute(
            "IMAGE_CHECK", {"asset_ids": [generated.id], "slides": []}, workflow_execution_id=execution.id
        )


def test_image_check_provider_error_marks_attempt_node_and_workflow_failed(session: Session) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = WorkflowEngine(
        session,
        workflow_registry,
        V3NodeExecutor(
            session=session,
            content_plan_model=RunnableLambda(_content_plan_model),
            text_generate_model=RunnableLambda(_text_generate_model),
            text_check_model=RunnableLambda(_text_check_model),
            image_check_model=RunnableLambda(_failing_image_check_model),
            **{
                key: value
                for key, value in _image_executor_kwargs().items()
                if key not in {"image_check_model", "image_check_model_name"}
            },
        ),
    )

    status = engine_instance.start_execution(execution.id, {"request_text": "fake"})

    assert status is WorkflowExecutionStatus.FAILED
    image_check = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_CHECK",
        )
    )
    assert image_check is not None
    assert image_check.status is NodeExecutionStatus.FAILED
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == image_check.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.FAILED


def test_image_check_rejection_blocks_approval_and_preserves_feedback(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    workflow_engine = _v3_engine_with_image_check(
        session, RunnableLambda(_failed_image_check_model)
    )
    assert workflow_engine.start_execution(execution.id, {"request_text": "fake"}) is WorkflowExecutionStatus.WAITING_APPROVAL
    waiting_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
            NodeExecution.status == NodeExecutionStatus.WAITING,
        )
    )
    assert waiting_node is not None
    with _approval_client(session, workflow_engine, monkeypatch) as client:
        response = client.post(
            f"/workflow-executions/{execution.id}/approvals",
            json={"node_execution_id": waiting_node.id},
        )
    main.app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "Use the reference product image" in response.json()["detail"]
    assert session.scalar(select(UserApproval).where(UserApproval.node_execution_id == waiting_node.id)) is None


def test_image_rejection_rechecks_then_allows_approval_after_pass(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = iter([_failed_image_check_model(None), _image_check_model(None)])
    execution = _execution(session)
    _v3_input_contract(session, execution)
    workflow_engine = _v3_engine_with_image_check(
        session, RunnableLambda(lambda _: next(outcomes))
    )
    assert workflow_engine.start_execution(execution.id, {"request_text": "fake"}) is WorkflowExecutionStatus.WAITING_APPROVAL
    original_waiting_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
            NodeExecution.revision_no == 0,
        )
    )
    assert original_waiting_node is not None
    with _approval_client(session, workflow_engine, monkeypatch) as client:
        rejected = client.post(
            f"/workflow-executions/{execution.id}/approvals",
            json={
                "node_execution_id": original_waiting_node.id,
                "decision": "REJECTED",
                "revision_target": "IMAGE",
                "revision_request": "제품 로고를 원본과 일치시켜 주세요.",
            },
        )
        assert rejected.status_code == 202
        assert rejected.json()["status"] == "RUNNING"
        revision_intent = _consume_latest_v3_dispatch(session, workflow_engine, execution.id)
        assert revision_intent.action == ACTION_REVISION
        revised_waiting_node = session.scalar(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_type == "HUMAN_APPROVAL",
                NodeExecution.revision_no == 1,
            )
            .order_by(NodeExecution.id.desc())
        )
        assert revised_waiting_node is not None
        approved = client.post(
            f"/workflow-executions/{execution.id}/approvals",
            json={"node_execution_id": revised_waiting_node.id},
        )
        assert approved.status_code == 202
        resume_intent = _consume_latest_v3_dispatch(session, workflow_engine, execution.id)
        assert resume_intent.action == ACTION_RESUME
    main.app.dependency_overrides.clear()

    assert approved.json()["status"] == "WAITING_APPROVAL"
    publication_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "PUBLICATION",
            NodeExecution.revision_no == 1,
        )
    )
    assert publication_node is not None
    assert publication_node.status is NodeExecutionStatus.SUCCEEDED
    assert isinstance(publication_node.output_data, dict)
    publication_asset_ids = publication_node.output_data["asset_ids"]
    assert isinstance(publication_asset_ids, list)
    publication_assets = list(
        session.scalars(select(FileAsset).where(FileAsset.id.in_(publication_asset_ids)))
    )
    assert [asset.slide_no for asset in sorted(publication_assets, key=lambda asset: asset.slide_no or 0)] == [
        1,
        2,
        3,
        4,
        5,
    ]
    original_decision = session.scalar(
        select(UserApproval).where(UserApproval.node_execution_id == original_waiting_node.id)
    )
    assert original_decision is not None
    assert original_decision.decision is ApprovalDecision.REJECTED
    revised_image_check = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_CHECK",
            NodeExecution.revision_no == 1,
        )
    )
    assert revised_image_check is not None
    assert revised_image_check.output_data is not None
    assert revised_image_check.output_data["passed"] is True


@pytest.mark.parametrize(
    ("revision_target", "start_node", "expected_nodes", "reused_nodes"),
    [
        (
            "PLAN",
            "CONTENT_PLAN",
            [
                "CONTENT_PLAN",
                "TEXT_GENERATE",
                "TEXT_CHECK",
                "IMAGE_GENERATE",
                "IMAGE_CHECK",
                "HUMAN_APPROVAL",
            ],
            [],
        ),
        (
            "TEXT",
            "TEXT_GENERATE",
            ["TEXT_GENERATE", "TEXT_CHECK", "IMAGE_GENERATE", "IMAGE_CHECK", "HUMAN_APPROVAL"],
            ["CONTENT_PLAN"],
        ),
        (
            "IMAGE",
            "IMAGE_GENERATE",
            ["IMAGE_GENERATE", "IMAGE_CHECK", "HUMAN_APPROVAL"],
            ["CONTENT_PLAN", "TEXT_GENERATE", "TEXT_CHECK"],
        ),
    ],
)
def test_rejected_revision_starts_at_target_and_preserves_unaffected_history(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    revision_target: str,
    start_node: str,
    expected_nodes: list[str],
    reused_nodes: list[str],
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    workflow_engine = _v3_engine_with_image_check(session, RunnableLambda(_image_check_model))
    assert workflow_engine.start_execution(execution.id, {"request_text": "fake"}) is WorkflowExecutionStatus.WAITING_APPROVAL
    waiting_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
            NodeExecution.revision_no == 0,
        )
    )
    assert waiting_node is not None

    with _approval_client(session, workflow_engine, monkeypatch) as client:
        response = client.post(
            f"/workflow-executions/{execution.id}/approvals",
            json={
                "node_execution_id": waiting_node.id,
                "decision": "REJECTED",
                "revision_target": revision_target,
                "revision_request": "Please revise this card news.",
            },
        )
    main.app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["status"] == "RUNNING"
    intent = _consume_latest_v3_dispatch(session, workflow_engine, execution.id)
    assert intent.action == ACTION_REVISION
    revision = session.scalar(
        select(Revision).where(Revision.workflow_execution_id == execution.id)
    )
    assert revision is not None
    assert revision.revision_no == 1
    assert revision.start_node_type == start_node
    assert revision.request_text == "Please revise this card news."
    revised_nodes = list(
        session.scalars(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.revision_no == 1,
            )
            .order_by(NodeExecution.id)
        )
    )
    assert [node.node_type for node in revised_nodes] == expected_nodes
    assert all(node.status is not NodeExecutionStatus.PENDING for node in revised_nodes)
    assert revised_nodes[0].input_data["revision_request"] == "Please revise this card news."
    for upstream, downstream in pairwise(revised_nodes):
        assert downstream.input_data == upstream.output_data
    for node_type in reused_nodes:
        reused_node = session.scalar(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_type == node_type,
                NodeExecution.revision_no == 0,
                NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
            )
        )
        assert reused_node is not None
        reused_attempt = session.scalar(
            select(NodeExecutionAttempt).where(
                NodeExecutionAttempt.node_execution_id == reused_node.id,
                NodeExecutionAttempt.attempt_no == 1,
            )
        )
        assert reused_attempt is not None
        assert reused_attempt.status is NodeExecutionAttemptStatus.SUCCEEDED


def test_image_rejection_recheck_failure_and_duplicate_request_are_rejected(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    workflow_engine = _v3_engine_with_image_check(
        session, RunnableLambda(_failed_image_check_model)
    )
    assert workflow_engine.start_execution(execution.id, {"request_text": "fake"}) is WorkflowExecutionStatus.WAITING_APPROVAL
    original_waiting_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
            NodeExecution.revision_no == 0,
        )
    )
    assert original_waiting_node is not None
    rejection = {
        "node_execution_id": original_waiting_node.id,
        "decision": "REJECTED",
        "revision_target": "IMAGE",
        "revision_request": "상품 색상을 수정해 주세요.",
    }
    with _approval_client(session, workflow_engine, monkeypatch) as client:
        invalid_rejection = client.post(
            f"/workflow-executions/{execution.id}/approvals",
            json={
                "node_execution_id": original_waiting_node.id,
                "decision": "REJECTED",
                "revision_request": "상품 색상을 수정해 주세요.",
            },
        )
        first = client.post(f"/workflow-executions/{execution.id}/approvals", json=rejection)
        duplicate = client.post(f"/workflow-executions/{execution.id}/approvals", json=rejection)
        assert first.status_code == 202
        revision_intent = _consume_latest_v3_dispatch(session, workflow_engine, execution.id)
        assert revision_intent.action == ACTION_REVISION
        revised_waiting_node = session.scalar(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_type == "HUMAN_APPROVAL",
                NodeExecution.revision_no == 1,
            )
        )
        assert revised_waiting_node is not None
        blocked = client.post(
            f"/workflow-executions/{execution.id}/approvals",
            json={"node_execution_id": revised_waiting_node.id},
        )
    main.app.dependency_overrides.clear()

    assert invalid_rejection.status_code == 422
    assert duplicate.status_code == 422
    assert blocked.status_code == 422
    assert "IMAGE_CHECK must pass" in blocked.json()["detail"]


def test_rejected_api_commits_approval_revision_prepared_attempt_and_intent_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second Session observes the complete committed revision boundary before a worker runs."""
    # Keep independently committed test fixtures below normal prompt version 1
    # so they cannot become the default "latest" prompt for rollback fixtures.
    version = -int(uuid4().hex[:7], 16)
    with Session(engine) as api_session:
        execution = _execution(api_session)
        _v3_input_contract(api_session, execution, version)
        workflow_engine = _v3_engine_with_image_check(
            api_session, RunnableLambda(_image_check_model)
        )
        assert (
            workflow_engine.start_execution(execution.id, {"request_text": "fake"})
            is WorkflowExecutionStatus.WAITING_APPROVAL
        )
        waiting_node = api_session.scalar(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_type == "HUMAN_APPROVAL",
                NodeExecution.revision_no == 0,
            )
        )
        assert waiting_node is not None

        def get_api_session() -> Iterator[Session]:
            yield api_session

        main.app.dependency_overrides[main.get_session] = get_api_session
        monkeypatch.setattr(main, "get_ai_workflow_services", lambda _: object())
        monkeypatch.setattr(main, "get_workflow_engine", lambda *_args: workflow_engine)
        monkeypatch.setattr(
            main.celery_app,
            "send_task",
            lambda *_args, **_kwargs: SimpleNamespace(id="atomic-revision-task"),
        )
        try:
            with TestClient(main.app) as client:
                response = client.post(
                    f"/workflow-executions/{execution.id}/approvals",
                    json={
                        "node_execution_id": waiting_node.id,
                        "decision": "REJECTED",
                        "revision_target": "IMAGE",
                        "revision_request": "Use the current product color.",
                    },
                )
            assert response.status_code == 202
            assert response.json()["status"] == "RUNNING"

            # No worker/AI execution occurs before this separate-session read.
            with Session(engine) as observed:
                approval = observed.scalar(
                    select(UserApproval).where(UserApproval.node_execution_id == waiting_node.id)
                )
                assert approval is not None
                assert approval.decision is ApprovalDecision.REJECTED
                revision = observed.scalar(
                    select(Revision).where(Revision.user_approval_id == approval.id)
                )
                assert revision is not None
                assert revision.revision_no == 1
                assert revision.start_node_type == "IMAGE_GENERATE"
                prepared = observed.scalar(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution.id,
                        NodeExecution.node_type == "IMAGE_GENERATE",
                        NodeExecution.revision_no == revision.revision_no,
                    )
                )
                assert prepared is not None
                assert prepared.status is NodeExecutionStatus.RUNNING
                attempt = observed.scalar(
                    select(NodeExecutionAttempt).where(
                        NodeExecutionAttempt.node_execution_id == prepared.id,
                        NodeExecutionAttempt.attempt_no == 1,
                    )
                )
                assert attempt is not None
                assert attempt.status is NodeExecutionAttemptStatus.RUNNING
                assert attempt.metadata_["retry_kind"] == "USER_REQUESTED"
                intent = observed.scalar(
                    select(WorkflowDispatchIntent).where(
                        WorkflowDispatchIntent.workflow_execution_id == execution.id,
                        WorkflowDispatchIntent.action == ACTION_REVISION,
                    )
                )
                assert intent is not None
                assert intent.revision_target == "IMAGE_GENERATE"
                assert intent.revision_request == revision.request_text
                observed_execution = observed.get(WorkflowExecution, execution.id)
                assert observed_execution is not None
                assert observed_execution.status is WorkflowExecutionStatus.RUNNING
                assert observed_execution.current_node_type == prepared.node_type
        finally:
            main.app.dependency_overrides.clear()
            _cleanup_independent_v3_fixture(execution.id, version)


def test_rejected_enqueue_failure_keeps_revision_intent_for_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broker failure is post-commit: relay may enqueue the exact same durable intent."""
    version = -int(uuid4().hex[:7], 16)
    with Session(engine) as api_session:
        execution = _execution(api_session)
        _v3_input_contract(api_session, execution, version)
        workflow_engine = _v3_engine_with_image_check(
            api_session, RunnableLambda(_image_check_model)
        )
        assert (
            workflow_engine.start_execution(execution.id, {"request_text": "fake"})
            is WorkflowExecutionStatus.WAITING_APPROVAL
        )
        waiting_node = api_session.scalar(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_type == "HUMAN_APPROVAL",
            )
        )
        assert waiting_node is not None

        def get_api_session() -> Iterator[Session]:
            yield api_session

        main.app.dependency_overrides[main.get_session] = get_api_session
        monkeypatch.setattr(main, "get_ai_workflow_services", lambda _: object())
        monkeypatch.setattr(main, "get_workflow_engine", lambda *_args: workflow_engine)
        monkeypatch.setattr(
            main.celery_app,
            "send_task",
            Mock(side_effect=ConnectionError("test Redis unavailable")),
        )
        try:
            with TestClient(main.app) as client:
                response = client.post(
                    f"/workflow-executions/{execution.id}/approvals",
                    json={
                        "node_execution_id": waiting_node.id,
                        "decision": "REJECTED",
                        "revision_target": "IMAGE",
                        "revision_request": "Keep the source product color.",
                    },
                )
            assert response.status_code == 202
            assert response.json()["status"] == "RUNNING"

            with Session(engine) as observed:
                revision = observed.scalar(
                    select(Revision).where(Revision.workflow_execution_id == execution.id)
                )
                prepared = observed.scalar(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution.id,
                        NodeExecution.revision_no == 1,
                    )
                )
                attempt = (
                    observed.scalar(
                        select(NodeExecutionAttempt).where(
                            NodeExecutionAttempt.node_execution_id == prepared.id  # type: ignore[union-attr]
                        )
                    )
                    if prepared is not None
                    else None
                )
                intent = observed.scalar(
                    select(WorkflowDispatchIntent).where(
                        WorkflowDispatchIntent.workflow_execution_id == execution.id,
                        WorkflowDispatchIntent.action == ACTION_REVISION,
                    )
                )
                assert revision is not None and prepared is not None and attempt is not None
                assert intent is not None
                assert intent.status == PENDING
                assert intent.last_error == "ConnectionError"
                assert prepared.status is NodeExecutionStatus.RUNNING
                assert attempt.attempt_no == 1

                relay_celery = Mock()
                relay_celery.send_task.return_value = SimpleNamespace(id="relay-task")
                relayed = GenerationDispatchService(observed, relay_celery, "test_generation").relay_pending()
                assert relayed == 1
                observed.refresh(intent)
                assert intent.status == QUEUED
                assert intent.dispatch_count == 1
                assert observed.scalar(
                    select(Revision).where(Revision.workflow_execution_id == execution.id)
                ).id == revision.id  # type: ignore[union-attr]
                assert observed.scalar(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution.id,
                        NodeExecution.revision_no == 1,
                    )
                ).id == prepared.id  # type: ignore[union-attr]
                assert observed.scalar(
                    select(NodeExecutionAttempt).where(
                        NodeExecutionAttempt.node_execution_id == prepared.id
                    )
                ).id == attempt.id  # type: ignore[union-attr]
        finally:
            main.app.dependency_overrides.clear()
            _cleanup_independent_v3_fixture(execution.id, version)


def test_duplicate_revision_dispatch_delivery_reuses_prepared_state_and_completed_intent_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sequential duplicate delivery may not create a second user revision or attempt."""
    version = -int(uuid4().hex[:7], 16)
    with Session(engine) as api_session:
        execution = _execution(api_session)
        _v3_input_contract(api_session, execution, version)
        workflow_engine = _v3_engine_with_image_check(
            api_session, RunnableLambda(_image_check_model)
        )
        assert (
            workflow_engine.start_execution(execution.id, {"request_text": "fake"})
            is WorkflowExecutionStatus.WAITING_APPROVAL
        )
        waiting_node = api_session.scalar(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_type == "HUMAN_APPROVAL",
            )
        )
        assert waiting_node is not None

        def get_api_session() -> Iterator[Session]:
            yield api_session

        main.app.dependency_overrides[main.get_session] = get_api_session
        monkeypatch.setattr(main, "get_ai_workflow_services", lambda _: object())
        monkeypatch.setattr(main, "get_workflow_engine", lambda *_args: workflow_engine)
        monkeypatch.setattr(
            main.celery_app,
            "send_task",
            lambda *_args, **_kwargs: SimpleNamespace(id="duplicate-revision-task"),
        )
        try:
            with TestClient(main.app) as client:
                response = client.post(
                    f"/workflow-executions/{execution.id}/approvals",
                    json={
                        "node_execution_id": waiting_node.id,
                        "decision": "REJECTED",
                        "revision_target": "IMAGE",
                        "revision_request": "Keep the current product color.",
                    },
                )
            assert response.status_code == 202

            with Session(engine) as observed:
                intent = observed.scalar(
                    select(WorkflowDispatchIntent).where(
                        WorkflowDispatchIntent.workflow_execution_id == execution.id,
                        WorkflowDispatchIntent.action == ACTION_REVISION,
                    )
                )
                revision = observed.scalar(
                    select(Revision).where(Revision.workflow_execution_id == execution.id)
                )
                prepared = observed.scalar(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution.id,
                        NodeExecution.revision_no == 1,
                    )
                )
                attempt = (
                    observed.scalar(
                        select(NodeExecutionAttempt).where(
                            NodeExecutionAttempt.node_execution_id == prepared.id  # type: ignore[union-attr]
                        )
                    )
                    if prepared is not None
                    else None
                )
                assert intent is not None and revision is not None
                assert prepared is not None and attempt is not None
                intent_id, revision_id, node_id, attempt_id = (
                    intent.id,
                    revision.id,
                    prepared.id,
                    attempt.id,
                )

            with Session(engine) as worker_session:
                worker_engine = _v3_engine_with_image_check(
                    worker_session, RunnableLambda(_image_check_model)
                )
                dispatch = GenerationDispatchService(worker_session, Mock(), "test_generation")
                claimed = dispatch.claim(intent_id)
                assert claimed is not None
                execute_claimed_intent(worker_session, claimed, worker_engine, lambda _id: {})
                assert claimed.claim_token is not None
                assert dispatch.complete(intent_id, claimed.claim_token, claimed.claim_generation)
                # The same broker delivery after completion cannot reacquire the intent.
                assert dispatch.claim(intent_id) is None

            with Session(engine) as observed:
                completed = observed.get(WorkflowDispatchIntent, intent_id)
                assert completed is not None
                assert completed.status == COMPLETED
                assert [row.id for row in observed.scalars(
                    select(Revision).where(Revision.workflow_execution_id == execution.id)
                )] == [revision_id]
                assert [row.id for row in observed.scalars(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution.id,
                        NodeExecution.revision_no == 1,
                    )
                ) if row.node_type == "IMAGE_GENERATE"] == [node_id]
                assert [row.id for row in observed.scalars(
                    select(NodeExecutionAttempt).where(
                        NodeExecutionAttempt.node_execution_id == node_id
                    )
                )] == [attempt_id]
        finally:
            main.app.dependency_overrides.clear()
            _cleanup_independent_v3_fixture(execution.id, version)


def test_generation_worker_uses_v3_executor_for_approved_resume_and_persists_atomic_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual worker composition root selects V3NodeExecutor from workflow_version."""
    from app import generation_tasks

    version = -int(uuid4().hex[:7], 16)
    with Session(engine) as api_session:
        execution = _execution(api_session)
        _v3_input_contract(api_session, execution, version)
        api_engine = _v3_engine_with_image_check(api_session, RunnableLambda(_image_check_model))
        assert (
            api_engine.start_execution(execution.id, {"request_text": "fake"})
            is WorkflowExecutionStatus.WAITING_APPROVAL
        )
        waiting_node = api_session.scalar(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_type == "HUMAN_APPROVAL",
            )
        )
        assert waiting_node is not None
        try:
            with monkeypatch.context() as api_patch, _approval_client(
                api_session, api_engine, api_patch
            ) as client:
                response = client.post(
                    f"/workflow-executions/{execution.id}/approvals",
                    json={"node_execution_id": waiting_node.id, "decision": "APPROVED"},
                )
            assert response.status_code == 202
            assert response.json()["status"] == "WAITING_APPROVAL"

            with Session(engine) as observed:
                approval = observed.scalar(
                    select(UserApproval).where(UserApproval.node_execution_id == waiting_node.id)
                )
                intent = observed.scalar(
                    select(WorkflowDispatchIntent).where(
                        WorkflowDispatchIntent.workflow_execution_id == execution.id,
                        WorkflowDispatchIntent.action == ACTION_RESUME,
                    )
                )
                persisted = observed.get(WorkflowExecution, execution.id)
                assert approval is not None
                assert approval.decision is ApprovalDecision.APPROVED
                assert intent is not None
                assert intent.status == QUEUED
                assert persisted is not None
                assert persisted.status is WorkflowExecutionStatus.WAITING_APPROVAL
                intent_id = intent.id

            executed_nodes: list[str] = []
            original_v3_execute = V3NodeExecutor.execute

            def record_v3_execute(
                executor: V3NodeExecutor, node_key: str, *args: object, **kwargs: object
            ) -> NodeExecutionResult:
                executed_nodes.append(node_key)
                return original_v3_execute(executor, node_key, *args, **kwargs)

            def rule_executor_must_not_run(*args: object, **kwargs: object) -> NodeExecutionResult:
                del args, kwargs
                raise AssertionError("V3 worker selected RuleBasedNodeExecutor")

            # The worker currently builds legacy services before it branches on
            # workflow_version.  V3 PUBLICATION does not invoke this provider.
            monkeypatch.setattr(main.app.state, "llm_provider", object(), raising=False)
            monkeypatch.setattr(V3NodeExecutor, "execute", record_v3_execute)
            monkeypatch.setattr(main.RuleBasedNodeExecutor, "execute", rule_executor_must_not_run)
            generation_tasks.execute_generation.run(intent_id)

            with Session(engine) as observed:
                completed = observed.get(WorkflowDispatchIntent, intent_id)
                persisted = observed.get(WorkflowExecution, execution.id)
                publication = observed.scalar(
                    select(NodeExecution).where(
                        NodeExecution.workflow_execution_id == execution.id,
                        NodeExecution.node_type == "PUBLICATION",
                    )
                )
                assert completed is not None
                assert completed.status == COMPLETED
                assert persisted is not None
                assert persisted.status is WorkflowExecutionStatus.SUCCEEDED
                assert publication is not None
                assert publication.status is NodeExecutionStatus.SUCCEEDED
                assert observed.scalar(
                    select(SocialPublication).where(SocialPublication.node_execution_id == publication.id)
                ) is None
            assert executed_nodes == ["PUBLICATION"]
        finally:
            main.app.dependency_overrides.clear()
            _cleanup_independent_v3_fixture(execution.id, version)


def test_approved_commit_failure_rolls_back_approval_and_resume_intent(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    workflow_engine = _v3_engine_with_image_check(session, RunnableLambda(_image_check_model))
    assert (
        workflow_engine.start_execution(execution.id, {"request_text": "fake"})
        is WorkflowExecutionStatus.WAITING_APPROVAL
    )
    waiting_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
        )
    )
    assert waiting_node is not None

    def fail_commit() -> None:
        raise RuntimeError("simulated approved commit failure")

    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="simulated approved commit failure"):
        main.approve_workflow_node(
            execution.id,
            main.ApprovalRequest(node_execution_id=waiting_node.id, decision=ApprovalDecision.APPROVED),
            session,
        )
    session.expire_all()

    assert session.scalar(
        select(UserApproval).where(UserApproval.node_execution_id == waiting_node.id)
    ) is None
    assert session.scalar(
        select(WorkflowDispatchIntent).where(
            WorkflowDispatchIntent.workflow_execution_id == execution.id,
            WorkflowDispatchIntent.action == ACTION_RESUME,
        )
    ) is None
    persisted = session.get(WorkflowExecution, execution.id)
    assert persisted is not None
    assert persisted.status is WorkflowExecutionStatus.WAITING_APPROVAL
    assert waiting_node.status is NodeExecutionStatus.WAITING


def test_expired_generation_claim_reclaims_with_new_fencing_token_and_rejects_old_completion(
    session: Session,
) -> None:
    execution = _execution(session)
    intent = GenerationDispatchService(session, Mock(), "test_generation").create(
        execution.id, "START"
    )
    session.commit()
    first_dispatch = GenerationDispatchService(session, Mock(), "test_generation")
    first = first_dispatch.claim(intent.id, lease_seconds=1)
    assert first is not None and first.claim_token is not None
    first_token, first_generation = first.claim_token, first.claim_generation
    original_expiry = first.lease_expires_at
    assert original_expiry is not None
    assert first_dispatch.heartbeat(intent.id, first_token, first_generation, 30)
    session.refresh(first)
    assert first.status == CLAIMED
    assert first.lease_expires_at is not None and first.lease_expires_at > original_expiry

    first.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    session.expire_all()
    second = first_dispatch.claim(intent.id, lease_seconds=30)
    assert second is not None and second.claim_token is not None
    assert second.claim_generation == first_generation + 1
    assert second.claim_token != first_token
    assert first_dispatch.complete(intent.id, first_token, first_generation) is False
    assert first_dispatch.complete(intent.id, second.claim_token, second.claim_generation) is True
    session.expire_all()
    assert session.get(WorkflowDispatchIntent, intent.id).status == COMPLETED  # type: ignore[union-attr]


def test_stale_claim_cannot_commit_workflow_state_or_intent_from_separate_sessions(
    session: Session, request: pytest.FixtureRequest,
) -> None:
    """A real DB re-claim fences both dispatch finalization and Engine state commit."""
    with Session(engine) as setup:
        user = User(email=f"{uuid4()}@example.com", name="fencing concurrency tester")
        setup.add(user)
        setup.flush()
        execution = WorkflowExecution(
            user_id=user.id,
            workflow_key="vora_content_creation",
            workflow_version=3,
        )
        setup.add(execution)
        setup.flush()
        intent = GenerationDispatchService(setup, Mock(), "test_generation").create(
            execution.id, ACTION_START
        )
        execution_id = execution.id
        intent_id = intent.id
        setup.commit()
    request.addfinalizer(lambda: _cleanup_independent_v3_fixture(execution_id, -999999999))

    with Session(engine) as worker_a_session, Session(engine) as worker_b_session:
        worker_a_dispatch = GenerationDispatchService(worker_a_session, Mock(), "test_generation")
        first = worker_a_dispatch.claim(intent_id, lease_seconds=30)
        assert first is not None and first.claim_token is not None
        first_token, first_generation = first.claim_token, first.claim_generation
        assert worker_a_dispatch.owns(intent_id, first_token, first_generation)

        worker_b_dispatch = GenerationDispatchService(worker_b_session, Mock(), "test_generation")
        expired = worker_b_session.get(WorkflowDispatchIntent, intent_id)
        assert expired is not None
        expired.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        worker_b_session.commit()
        second = worker_b_dispatch.claim(intent_id, lease_seconds=30)
        assert second is not None and second.claim_token is not None
        assert second.claim_generation == first_generation + 1
        second_generation = second.claim_generation

        assert worker_a_dispatch.complete(intent_id, first_token, first_generation) is False
        assert (
            worker_a_dispatch.fail(
                intent_id,
                RuntimeError("stale worker"),
                first_token,
                first_generation,
            )
            is False
        )

        stale_execution = worker_a_session.get(WorkflowExecution, execution_id)
        assert stale_execution is not None
        stale_execution.status = WorkflowExecutionStatus.SUCCEEDED
        stale_engine = WorkflowEngine(worker_a_session, workflow_registry, Mock())
        stale_engine.set_execution_fence(
            lambda: worker_a_dispatch.owns_for_update(
                intent_id, first_token, first_generation
            )
        )
        with pytest.raises(RuntimeError, match="lease ownership was lost"):
            stale_engine._commit_state()
        worker_a_session.rollback()

        current_execution = worker_b_session.get(WorkflowExecution, execution_id)
        assert current_execution is not None
        current_execution.status = WorkflowExecutionStatus.SUCCEEDED
        current_execution.current_node_type = None
        current_node = NodeExecution(
            workflow_execution_id=execution_id,
            node_type="CONTENT_PLAN",
            revision_no=0,
            input_data={},
            output_data={"fenced": True},
            status=NodeExecutionStatus.SUCCEEDED,
        )
        worker_b_session.add(current_node)
        worker_b_session.flush()
        worker_b_session.add(
            NodeExecutionAttempt(
                node_execution_id=current_node.id,
                attempt_no=1,
                status=NodeExecutionAttemptStatus.SUCCEEDED,
            )
        )
        current_engine = WorkflowEngine(worker_b_session, workflow_registry, Mock())
        current_engine.set_execution_fence(
            lambda: worker_b_dispatch.owns_for_update(
                intent_id, second.claim_token, second.claim_generation
            )
        )
        current_engine._commit_state()
        assert worker_b_dispatch.complete(
            intent_id, second.claim_token, second.claim_generation
        )

    with Session(engine) as observed:
        persisted_intent = observed.get(WorkflowDispatchIntent, intent_id)
        persisted_execution = observed.get(WorkflowExecution, execution_id)
        persisted_node = observed.scalar(
            select(NodeExecution).where(NodeExecution.workflow_execution_id == execution_id)
        )
        persisted_attempt = observed.scalar(
            select(NodeExecutionAttempt).where(
                NodeExecutionAttempt.node_execution_id == persisted_node.id  # type: ignore[union-attr]
            )
        )
        assert persisted_intent is not None and persisted_intent.status == COMPLETED
        assert persisted_intent.claim_generation == second_generation
        assert persisted_execution is not None
        assert persisted_execution.status is WorkflowExecutionStatus.SUCCEEDED
        assert persisted_node is not None and persisted_node.output_data == {"fenced": True}
        assert persisted_attempt is not None
        assert persisted_attempt.status is NodeExecutionAttemptStatus.SUCCEEDED

    with Session(engine) as cleanup:
        node_ids = select(NodeExecution.id).where(
            NodeExecution.workflow_execution_id == execution_id
        )
        cleanup.execute(
            delete(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id.in_(node_ids))
        )
        cleanup.execute(delete(NodeExecution).where(NodeExecution.workflow_execution_id == execution_id))
        cleanup.execute(
            delete(WorkflowDispatchIntent).where(
                WorkflowDispatchIntent.workflow_execution_id == execution_id
            )
        )
        user_id = cleanup.scalar(
            select(WorkflowExecution.user_id).where(WorkflowExecution.id == execution_id)
        )
        cleanup.execute(delete(WorkflowExecution).where(WorkflowExecution.id == execution_id))
        if user_id is not None:
            cleanup.execute(delete(User).where(User.id == user_id))
        cleanup.commit()


def test_start_crash_recovery_reuses_completed_nodes_and_fences_late_result(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    engine_instance = _v3_engine_with_image_check(session, RunnableLambda(_image_check_model))
    original_execute = V3NodeExecutor.execute

    def crash_during_text(
        executor: V3NodeExecutor, node_key: str, *args: object, **kwargs: object
    ) -> NodeExecutionResult:
        if node_key == "TEXT_GENERATE":
            raise KeyboardInterrupt("simulated worker crash")
        return original_execute(executor, node_key, *args, **kwargs)

    monkeypatch.setattr(V3NodeExecutor, "execute", crash_during_text)
    with pytest.raises(KeyboardInterrupt, match="simulated worker crash"):
        engine_instance.start_execution(execution.id, {"request_text": "fake"})
    monkeypatch.setattr(V3NodeExecutor, "execute", original_execute)

    completed_plan = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "CONTENT_PLAN",
            NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
        )
    )
    interrupted_text = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "TEXT_GENERATE",
            NodeExecution.status == NodeExecutionStatus.RUNNING,
        )
    )
    assert completed_plan is not None and interrupted_text is not None
    assert engine_instance.recover_interrupted_execution(execution.id) is WorkflowExecutionStatus.WAITING_APPROVAL
    assert session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "CONTENT_PLAN",
        )
    ).id == completed_plan.id  # type: ignore[union-attr]
    assert interrupted_text.status is NodeExecutionStatus.SUCCEEDED
    attempts = list(
        session.scalars(
            select(NodeExecutionAttempt)
            .where(NodeExecutionAttempt.node_execution_id == interrupted_text.id)
            .order_by(NodeExecutionAttempt.attempt_no)
        )
    )
    assert [attempt.attempt_no for attempt in attempts] == [1, 2]
    assert attempts[0].status is NodeExecutionAttemptStatus.FAILED
    assert attempts[0].metadata_["recovery_reason"] == "WORKER_INTERRUPTED"
    assert attempts[1].status is NodeExecutionAttemptStatus.SUCCEEDED
    assert attempts[1].metadata_["retry_kind"] == "SYSTEM"
    assert interrupted_text.revision_no == 0

    fenced_execution = _execution(session)
    _v3_input_contract(session, fenced_execution, -int(uuid4().hex[:7], 16))
    still_owner = {"value": True}
    fenced_engine = _v3_engine_with_image_check(session, RunnableLambda(_image_check_model))
    fenced_engine.set_execution_guard(lambda: still_owner["value"])

    def lose_claim_after_provider(
        executor: V3NodeExecutor, node_key: str, *args: object, **kwargs: object
    ) -> NodeExecutionResult:
        result = original_execute(executor, node_key, *args, **kwargs)
        still_owner["value"] = False
        return result

    monkeypatch.setattr(V3NodeExecutor, "execute", lose_claim_after_provider)
    with pytest.raises(RuntimeError, match="lease ownership was lost"):
        fenced_engine.start_execution(fenced_execution.id, {"request_text": "fake"})
    monkeypatch.setattr(V3NodeExecutor, "execute", original_execute)
    stale_node = session.scalar(
        select(NodeExecution).where(NodeExecution.workflow_execution_id == fenced_execution.id)
    )
    stale_attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == stale_node.id)  # type: ignore[union-attr]
    )
    assert stale_node is not None and stale_attempt is not None
    assert stale_node.status is NodeExecutionStatus.RUNNING
    assert stale_attempt.status is NodeExecutionAttemptStatus.RUNNING


def test_resume_and_revision_crash_recovery_continue_only_unfinished_boundary(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    workflow_engine = _v3_engine_with_image_check(session, RunnableLambda(_image_check_model))
    assert (
        workflow_engine.start_execution(execution.id, {"request_text": "fake"})
        is WorkflowExecutionStatus.WAITING_APPROVAL
    )
    waiting_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
        )
    )
    assert waiting_node is not None
    session.add(
        UserApproval(
            workflow_execution_id=execution.id,
            node_execution_id=waiting_node.id,
            decision=ApprovalDecision.APPROVED,
        )
    )
    session.commit()
    original_run_nodes = workflow_engine._run_nodes

    def crash_before_publication(*args: object, **kwargs: object) -> WorkflowExecutionStatus:
        del args, kwargs
        raise KeyboardInterrupt("simulated resume crash")

    monkeypatch.setattr(workflow_engine, "_run_nodes", crash_before_publication)
    with pytest.raises(KeyboardInterrupt, match="simulated resume crash"):
        workflow_engine.resume_after_approval(execution.id)
    monkeypatch.setattr(workflow_engine, "_run_nodes", original_run_nodes)
    assert workflow_engine.recover_interrupted_execution(execution.id) is WorkflowExecutionStatus.SUCCEEDED
    assert session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "PUBLICATION",
        )
    ).status is NodeExecutionStatus.SUCCEEDED  # type: ignore[union-attr]

    revision_execution = _execution(session)
    _v3_input_contract(session, revision_execution, -int(uuid4().hex[:7], 16))
    revision_engine = _v3_engine_with_image_check(session, RunnableLambda(_image_check_model))
    assert (
        revision_engine.start_execution(revision_execution.id, {"request_text": "fake"})
        is WorkflowExecutionStatus.WAITING_APPROVAL
    )
    revision_waiting = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == revision_execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
        )
    )
    assert revision_waiting is not None
    approval = UserApproval(
        workflow_execution_id=revision_execution.id,
        node_execution_id=revision_waiting.id,
        decision=ApprovalDecision.REJECTED,
    )
    session.add(approval)
    session.flush()
    original_execute_prepared = revision_engine._execute_prepared_node

    def crash_after_revision_prepare(*args: object, **kwargs: object) -> WorkflowExecutionStatus:
        del args, kwargs
        raise KeyboardInterrupt("simulated revision crash")

    monkeypatch.setattr(revision_engine, "_execute_prepared_node", crash_after_revision_prepare)
    with pytest.raises(KeyboardInterrupt, match="simulated revision crash"):
        revision_engine.start_rejected_revision(
            revision_execution.id,
            "IMAGE_GENERATE",
            "Preserve the product color.",
            user_approval_id=approval.id,
        )
    monkeypatch.setattr(revision_engine, "_execute_prepared_node", original_execute_prepared)
    assert (
        revision_engine.recover_interrupted_execution(revision_execution.id)
        is WorkflowExecutionStatus.WAITING_APPROVAL
    )
    assert len(
        list(
            session.scalars(
                select(Revision).where(Revision.workflow_execution_id == revision_execution.id)
            )
        )
    ) == 1


@pytest.mark.parametrize("revision_target", ["PLAN", "TEXT", "IMAGE"])
def test_rejected_revision_commit_failure_rolls_back_all_prepared_state(
    session: Session, monkeypatch: pytest.MonkeyPatch, revision_target: str
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    workflow_engine = _v3_engine_with_image_check(
        session, RunnableLambda(_failed_image_check_model)
    )
    assert workflow_engine.start_execution(execution.id, {"request_text": "fake"}) is WorkflowExecutionStatus.WAITING_APPROVAL
    waiting_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
            NodeExecution.revision_no == 0,
        )
    )
    assert waiting_node is not None
    def fail_commit() -> None:
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(session, "commit", fail_commit)
    monkeypatch.setattr(main, "get_ai_workflow_services", lambda _: object())
    monkeypatch.setattr(main, "get_workflow_engine", lambda *_args: workflow_engine)

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        main.approve_workflow_node(
            execution.id,
            main.ApprovalRequest(
                node_execution_id=waiting_node.id,
                decision=ApprovalDecision.REJECTED,
                revision_target=revision_target,
                revision_request="제품 이미지를 수정해 주세요.",
            ),
            session,
        )
    session.expire_all()

    assert session.scalar(
        select(UserApproval).where(UserApproval.node_execution_id == waiting_node.id)
    ) is None
    persisted_execution = session.get(WorkflowExecution, execution.id)
    assert persisted_execution is not None
    assert persisted_execution.status is WorkflowExecutionStatus.WAITING_APPROVAL
    assert session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.revision_no == 1,
        )
    ) is None
    assert session.scalar(select(Revision).where(Revision.workflow_execution_id == execution.id)) is None


def test_rejected_image_revision_execution_failure_marks_prepared_state_failed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    workflow_engine = _v3_engine_with_image_check(
        session, RunnableLambda(_failed_image_check_model)
    )
    assert workflow_engine.start_execution(execution.id, {"request_text": "fake"}) is WorkflowExecutionStatus.WAITING_APPROVAL
    waiting_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
            NodeExecution.revision_no == 0,
        )
    )
    assert waiting_node is not None
    workflow_engine._executor._image_provider = FailingImageProvider()  # type: ignore[attr-defined]
    with _approval_client(session, workflow_engine, monkeypatch) as client:
        response = client.post(
            f"/workflow-executions/{execution.id}/approvals",
            json={
                "node_execution_id": waiting_node.id,
                "decision": "REJECTED",
                "revision_target": "IMAGE",
                "revision_request": "제품 이미지를 수정해 주세요.",
            },
        )
    main.app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["status"] == "RUNNING"
    intent = _consume_latest_v3_dispatch(session, workflow_engine, execution.id)
    assert intent.action == ACTION_REVISION
    revision = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_GENERATE",
            NodeExecution.revision_no == 1,
        )
    )
    assert revision is not None
    assert revision.status is NodeExecutionStatus.FAILED
    attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == revision.id)
    )
    assert attempt is not None
    assert attempt.status is NodeExecutionAttemptStatus.FAILED
    assert session.get(WorkflowExecution, execution.id).status is WorkflowExecutionStatus.FAILED  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("target_node_key", "revision_target"),
    [("CONTENT_PLAN", "PLAN"), ("TEXT_GENERATE", "TEXT")],
)
def test_recover_prepared_plan_or_text_revision_reuses_committed_attempt(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    target_node_key: str,
    revision_target: str,
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    workflow_engine = _v3_engine_with_image_check(session, RunnableLambda(_image_check_model))
    assert workflow_engine.start_execution(execution.id, {"request_text": "fake"}) is WorkflowExecutionStatus.WAITING_APPROVAL
    waiting_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
            NodeExecution.revision_no == 0,
        )
    )
    assert waiting_node is not None
    approval = UserApproval(
        workflow_execution_id=execution.id,
        node_execution_id=waiting_node.id,
        decision=ApprovalDecision.REJECTED,
    )
    session.add(approval)
    session.flush()
    original_execute = workflow_engine._execute_prepared_node

    def simulate_process_exit(*args: object, **kwargs: object) -> WorkflowExecutionStatus:
        del args, kwargs
        raise RuntimeError("simulated process exit after commit")

    monkeypatch.setattr(workflow_engine, "_execute_prepared_node", simulate_process_exit)
    with pytest.raises(RuntimeError, match="simulated process exit"):
        workflow_engine.start_rejected_revision(
            execution.id,
            target_node_key,
            f"Revise {revision_target}.",
            user_approval_id=approval.id,
        )
    monkeypatch.setattr(workflow_engine, "_execute_prepared_node", original_execute)

    prepared = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == target_node_key,
            NodeExecution.revision_no == 1,
        )
    )
    assert prepared is not None
    assert prepared.status is NodeExecutionStatus.RUNNING
    assert workflow_engine.recover_prepared_revision(execution.id) is WorkflowExecutionStatus.WAITING_APPROVAL
    session.refresh(prepared)
    assert prepared.status is NodeExecutionStatus.SUCCEEDED
    assert session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == prepared.id)
    ) is not None
    assert len(list(session.scalars(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == prepared.id)
    ))) == 1


def test_recover_prepared_image_revision_reuses_committed_node_and_attempt(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = _execution(session)
    _v3_input_contract(session, execution)
    workflow_engine = _v3_engine_with_image_check(
        session, RunnableLambda(_image_check_model)
    )
    assert workflow_engine.start_execution(execution.id, {"request_text": "fake"}) is WorkflowExecutionStatus.WAITING_APPROVAL
    waiting_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "HUMAN_APPROVAL",
            NodeExecution.revision_no == 0,
        )
    )
    assert waiting_node is not None
    session.add(
        UserApproval(
            workflow_execution_id=execution.id,
            node_execution_id=waiting_node.id,
            decision=ApprovalDecision.REJECTED,
        )
    )
    original_execute = workflow_engine._execute_prepared_node

    def simulate_process_exit(*args: object, **kwargs: object) -> WorkflowExecutionStatus:
        del args, kwargs
        raise RuntimeError("simulated process exit after commit")

    monkeypatch.setattr(workflow_engine, "_execute_prepared_node", simulate_process_exit)
    with pytest.raises(RuntimeError, match="simulated process exit"):
        workflow_engine.start_rejected_image_revision(execution.id, "제품 이미지를 수정해 주세요.")
    monkeypatch.setattr(workflow_engine, "_execute_prepared_node", original_execute)

    prepared = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_GENERATE",
            NodeExecution.revision_no == 1,
        )
    )
    assert prepared is not None
    assert prepared.status is NodeExecutionStatus.RUNNING
    prepared_attempt = session.scalar(
        select(NodeExecutionAttempt).where(NodeExecutionAttempt.node_execution_id == prepared.id)
    )
    assert prepared_attempt is not None
    assert prepared_attempt.status is NodeExecutionAttemptStatus.RUNNING

    # A prior operator claim is persisted so another operator cannot unknowingly
    # re-run an attempt that may still be executing. --force is the explicit
    # manual recovery path after the operator has verified the prior process died.
    prepared_attempt.metadata_ = {"manual_recovery_claimed_at": "2026-10-19T00:00:00+00:00"}
    session.commit()
    with pytest.raises(ValueError, match="already claimed"):
        workflow_engine.recover_prepared_revision(execution.id)

    assert workflow_engine.recover_prepared_revision(
        execution.id, force=True
    ) is WorkflowExecutionStatus.WAITING_APPROVAL
    session.refresh(prepared)
    session.refresh(prepared_attempt)
    assert prepared.status is NodeExecutionStatus.SUCCEEDED
    assert prepared_attempt.status is NodeExecutionAttemptStatus.SUCCEEDED
