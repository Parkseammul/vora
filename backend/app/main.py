import base64
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr, field_validator, model_validator

# SQL 문자열을 안전하게 실행하기 위한 SQLAlchemy 함수
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.cognito_auth import CognitoAuthenticationError, get_or_create_user, verifier
from app.config import settings
from app.content_planning import ContentPlanningService

# database.py에서 만든 PostgreSQL 연결 Engine 가져오기
from app.database import engine
from app.e2e_providers import (
    DeterministicE2EImageProvider,
    DeterministicE2ELLMProvider,
    FFmpegE2ETTSProvider,
    FFmpegE2EVideoProvider,
    wait_for_e2e_node_gate,
)
from app.generation_dispatch import (
    ACTION_RESUME,
    ACTION_REVISION,
    ACTION_START,
    GenerationDispatchService,
)
from app.image_provider import OpenAIImageProvider
from app.llm_provider import LLMProvider, LLMProviderType, MultiVendorLLMProvider
from app.media_providers import (
    ElevenLabsHTTPClient,
    ElevenLabsTTSProvider,
    FFmpegVideoComposer,
    RunwayHTTPClient,
    RunwayVideoProvider,
)
from app.models import (
    ApprovalDecision,
    AssetType,
    ExecutionInputSnapshot,
    FileAsset,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionStatus,
    OAuthAuthorizationState,
    PromptVersion,
    Revision,
    RulesVersion,
    SocialAccountConnection,
    SocialConnectionStatus,
    SocialPlatform,
    SocialPublication,
    SocialPublicationAttempt,
    User,
    UserApproval,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.node_executors import RuleBasedNodeExecutor
from app.openai_client import OpenAIStructuredClient
from app.publication import PublicationDraft, RedisPublicationEvents
from app.publication_runtime import get_publication_service, get_storage_provider
from app.revision_impact import CoreNodeKey, RevisionImpactService
from app.script_generation import ScriptGenerationService
from app.social_providers import (
    InstagramProvider,
    OAuthIdentity,
    SocialProviderError,
    YouTubeProvider,
    platform_from_string,
)
from app.v3_runtime import (
    ContentPlanOutput,
    ImageCheckOutput,
    SlideText,
    TextCheckOutput,
    TextGenerateOutput,
    V3NodeExecutor,
)
from app.video_async import (
    CeleryVideoTaskDispatcher,
    RedisVideoProgressEvents,
    VideoProgressEvents,
    VideoTaskDispatcher,
    sse_event,
)
from app.video_generation import (
    TTSProvider,
    VideoComposer,
    VideoGenerationService,
    VideoProvider,
)
from app.workflow_definitions import workflow_registry
from app.workflow_engine import WorkflowEngine
from app.workflow_execution_service import (
    UploadedInputImage,
    WorkflowCreationError,
    WorkflowExecutionService,
)

# VORA Backend의 FastAPI 애플리케이션 객체 생성
app = FastAPI()
bearer_scheme = HTTPBearer(auto_error=False)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RevisionRequest(BaseModel):
    current_node: CoreNodeKey
    revision_request: str

    @field_validator("revision_request")
    @classmethod
    def revision_request_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("revision_request must not be blank")
        return value


class ApprovalRequest(BaseModel):
    node_execution_id: int
    decision: ApprovalDecision = ApprovalDecision.APPROVED
    revision_target: Literal["PLAN", "TEXT", "IMAGE"] | None = None
    revision_request: str | None = None
    # target/feedback/slide_numbers are the structured V3 revision contract.
    # The legacy revision_target/revision_request fields remain accepted.
    target: Literal["PLAN", "TEXT", "IMAGE"] | None = None
    slide_numbers: list[int] | None = None
    feedback: str | None = None

    @model_validator(mode="after")
    def rejected_revision_must_be_complete(self) -> "ApprovalRequest":
        target = self.revision_target or self.target
        feedback = self.feedback or self.revision_request
        if self.decision is ApprovalDecision.REJECTED and (
            target is None or feedback is None or not feedback.strip()
        ):
            raise ValueError(
                "REJECTED requires revision_target and a non-blank revision_request"
            )
        if self.slide_numbers is not None:
            if target != "IMAGE":
                raise ValueError("slide_numbers is only supported for IMAGE revisions")
            normalized = sorted(set(self.slide_numbers))
            if normalized != self.slide_numbers or not normalized or any(
                slide_no not in range(1, 6) for slide_no in normalized
            ):
                raise ValueError("slide_numbers must contain unique values from 1 through 5")
        return self


class WorkflowListItem(BaseModel):
    workflow_execution_id: int
    workflow_version: int
    status: str
    title: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    thumbnail_url: str | None = None


class WorkflowListResponse(BaseModel):
    items: list[WorkflowListItem]
    next_cursor: str | None = None


class CompletedRevisionRequest(BaseModel):
    target: Literal["PLAN", "TEXT", "IMAGE"]
    slide_numbers: list[int] | None = None
    feedback: str

    @model_validator(mode="after")
    def validate_revision(self) -> "CompletedRevisionRequest":
        if not self.feedback.strip():
            raise ValueError("feedback must not be blank")
        if self.slide_numbers is not None:
            if self.target != "IMAGE":
                raise ValueError("slide_numbers is only supported for IMAGE revisions")
            if (
                not self.slide_numbers
                or sorted(set(self.slide_numbers)) != self.slide_numbers
                or any(slide_no not in range(1, 6) for slide_no in self.slide_numbers)
            ):
                raise ValueError("slide_numbers must contain unique values from 1 through 5")
        return self


class PublishRequest(BaseModel):
    platforms: list[str]
    youtube_title: str | None = None
    youtube_description: str | None = None
    instagram_caption: str | None = None
    force_republish: bool = False


NODE_KEYS = ("input_analysis", "content_planning", "script_generation", "video_generation")
V3_NODE_KEYS = (
    "CONTENT_PLAN",
    "TEXT_GENERATE",
    "TEXT_CHECK",
    "IMAGE_GENERATE",
    "IMAGE_CHECK",
    "HUMAN_APPROVAL",
    "PUBLICATION",
)
MAX_AUTOMATIC_VIDEO_ATTEMPTS = 3


@dataclass(frozen=True)
class AIWorkflowServices:
    content_planning: ContentPlanningService
    script_generation: ScriptGenerationService
    revision_impact: RevisionImpactService
    video_generation: VideoGenerationService | None


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        identity = verifier.verify(credentials.credentials)
        user = get_or_create_user(session, identity)
        # First-contact user provisioning is durable before the route executes.
        session.commit()
        return user
    except CognitoAuthenticationError as exc:
        session.rollback()
        raise HTTPException(status_code=401, detail="Invalid access token", headers={"WWW-Authenticate": "Bearer"}) from exc


def get_trace_authenticated_user_id(current_user: User = Depends(get_current_user)) -> int:  # noqa: B008
    return current_user.id


def configure_llm_provider(llm_provider: LLMProvider) -> None:
    """Application bootstrap injects the concrete vendor adapter once."""
    app.state.llm_provider = llm_provider


def configure_media_providers(
    video_provider: VideoProvider, tts_provider: TTSProvider, composer: VideoComposer
) -> None:
    app.state.media_providers = (video_provider, tts_provider, composer)


def _configure_default_providers() -> None:
    if settings.e2e_fake_providers:
        configure_llm_provider(DeterministicE2ELLMProvider())
        configure_media_providers(
            FFmpegE2EVideoProvider(),
            FFmpegE2ETTSProvider(),
            FFmpegVideoComposer(settings.ffmpeg_subtitle_font_path),
        )
        return
    if settings.openai_api_key:
        configure_llm_provider(
            MultiVendorLLMProvider(
                {LLMProviderType.OPENAI: OpenAIStructuredClient(settings.openai_api_key)}
            )
        )
    if settings.runway_api_key and settings.elevenlabs_api_key and settings.elevenlabs_voice_id:
        configure_media_providers(
            RunwayVideoProvider(RunwayHTTPClient(settings.runway_api_key)),
            ElevenLabsTTSProvider(
                ElevenLabsHTTPClient(
                    settings.elevenlabs_api_key,
                    settings.elevenlabs_voice_id,
                    settings.elevenlabs_model,
                )
            ),
            FFmpegVideoComposer(settings.ffmpeg_subtitle_font_path),
        )


_configure_default_providers()


def get_llm_provider() -> LLMProvider:
    llm_provider = getattr(app.state, "llm_provider", None)
    if llm_provider is None:
        raise RuntimeError("LLMProvider is not configured")
    return llm_provider


def get_video_generation_service(session: Session) -> VideoGenerationService | None:
    media = getattr(app.state, "media_providers", None)
    if media is None:
        return None
    return VideoGenerationService(
        session, media[0], media[1], media[2], settings.uploads_root, settings.fixed_bgm_asset_id
    )


def get_video_progress_events() -> VideoProgressEvents:
    return RedisVideoProgressEvents(settings.redis_url)


def get_video_task_dispatcher() -> VideoTaskDispatcher:
    return CeleryVideoTaskDispatcher(
        celery_app, get_video_progress_events(), settings.video_generation_queue
    )


def get_generation_dispatcher(session: Session) -> GenerationDispatchService:
    return GenerationDispatchService(session, celery_app, settings.generation_queue)


def get_ai_workflow_services(session: Session) -> AIWorkflowServices:
    llm_provider = get_llm_provider()
    return AIWorkflowServices(
        content_planning=ContentPlanningService(
            llm_provider,
            settings.llm_provider,
            settings.content_planning_model,
            session,
        ),
        script_generation=ScriptGenerationService(
            llm_provider,
            settings.llm_provider,
            settings.script_generation_model,
        ),
        revision_impact=RevisionImpactService(
            llm_provider,
            settings.llm_provider,
            settings.revision_impact_model,
        ),
        video_generation=get_video_generation_service(session),
    )


def get_content_plan_chat_model() -> ChatOpenAI:
    """Build the provider only when CONTENT_PLAN actually reaches its AI boundary."""
    return ChatOpenAI(
        model=settings.content_planning_model,
        api_key=SecretStr(settings.openai_api_key) if settings.openai_api_key else None,
        **_chat_model_settings(),
    )


def get_text_generate_chat_model() -> ChatOpenAI:
    """Build the provider only when TEXT_GENERATE reaches its AI boundary."""
    return ChatOpenAI(
        model=settings.script_generation_model,
        api_key=SecretStr(settings.openai_api_key) if settings.openai_api_key else None,
        **_chat_model_settings(),
    )


def get_text_check_chat_model() -> ChatOpenAI:
    """Build the provider only when TEXT_CHECK reaches its AI boundary."""
    return ChatOpenAI(
        model=settings.script_generation_model,
        api_key=SecretStr(settings.openai_api_key) if settings.openai_api_key else None,
        **_chat_model_settings(),
    )


def get_image_check_chat_model() -> ChatOpenAI:
    """Build the Vision-capable model only when IMAGE_CHECK executes."""
    return ChatOpenAI(
        model=settings.script_generation_model,
        api_key=SecretStr(settings.openai_api_key) if settings.openai_api_key else None,
        **_chat_model_settings(),
    )


def _chat_model_settings() -> dict[str, Any]:
    """Apply the shared retry override without changing the default deployment behavior."""
    value = os.environ.get("OPENAI_MAX_RETRIES")
    if value is None:
        return {}
    try:
        max_retries = int(value)
    except ValueError as exc:
        raise ValueError("OPENAI_MAX_RETRIES must be a non-negative integer") from exc
    if max_retries < 0:
        raise ValueError("OPENAI_MAX_RETRIES must be a non-negative integer")
    return {"max_retries": max_retries}


def get_workflow_engine(
    session: Session, services: AIWorkflowServices, workflow_version: int = 1
) -> WorkflowEngine:
    if workflow_version == 3:
        image_provider = (
            DeterministicE2EImageProvider()
            if settings.e2e_fake_providers
            else (
                OpenAIImageProvider(settings.openai_api_key, settings.image_generation_model)
                if settings.openai_api_key
                else None
            )
        )
        image_check_model: Runnable[dict[str, object], ImageCheckOutput] | None = (
            RunnableLambda(
                lambda _: ImageCheckOutput(passed=True, violations=[], feedback=None)
            )
            if settings.e2e_fake_providers
            else None
        )
        content_plan_model: Runnable[dict[str, object], ContentPlanOutput] | None = (
            RunnableLambda(
                lambda _: ContentPlanOutput(
                    core_message="Offline V3 carousel message",
                    slide_structure=["hook", "benefit", "proof", "detail", "cta"],
                )
            )
            if settings.e2e_fake_providers
            else None
        )
        text_generate_model: Runnable[dict[str, object], TextGenerateOutput] | None = (
            RunnableLambda(lambda _: _e2e_text_generate_output())
            if settings.e2e_fake_providers
            else None
        )
        text_check_model: Runnable[dict[str, object], TextCheckOutput] | None = (
            RunnableLambda(lambda _: _e2e_text_check_output())
            if settings.e2e_fake_providers
            else None
        )
        return WorkflowEngine(
            session,
            workflow_registry,
            V3NodeExecutor(
                session=session,
                content_plan_model=content_plan_model,
                content_plan_model_factory=None if content_plan_model else get_content_plan_chat_model,
                content_plan_model_name=settings.content_planning_model,
                text_generate_model=text_generate_model,
                text_generate_model_factory=None if text_generate_model else get_text_generate_chat_model,
                text_generate_model_name=settings.script_generation_model,
                text_check_model=text_check_model,
                text_check_model_factory=None if text_check_model else get_text_check_chat_model,
                text_check_model_name=settings.script_generation_model,
                image_provider=image_provider,
                image_model_name=settings.image_generation_model,
                storage=get_storage_provider(),
                image_check_model=image_check_model,
                image_check_model_factory=(
                    None if settings.e2e_fake_providers else get_image_check_chat_model
                ),
                image_check_model_name=settings.script_generation_model,
            ),
        )
    return WorkflowEngine(
        session,
        workflow_registry,
        RuleBasedNodeExecutor(
            content_planning_service=services.content_planning,
            script_generation_service=services.script_generation,
            video_generation_service=services.video_generation,
            session=session,
        ),
        get_video_task_dispatcher(),
    )


def _e2e_text_generate_output() -> TextGenerateOutput:
    wait_for_e2e_node_gate("TEXT_GENERATE")
    return TextGenerateOutput(
        slides=[SlideText(slide_no=index, text=f"Offline slide {index}") for index in range(1, 6)]
    )


def _e2e_text_check_output() -> TextCheckOutput:
    wait_for_e2e_node_gate("TEXT_CHECK")
    return TextCheckOutput(passed=True, violations=[], feedback=None)


def _get_execution_or_404(session: Session, workflow_execution_id: int) -> WorkflowExecution:
    execution = session.get(WorkflowExecution, workflow_execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Workflow execution was not found")
    return execution


def _get_owned_execution_or_404(
    session: Session, workflow_execution_id: int, user_id: int
) -> WorkflowExecution:
    execution = session.scalar(
        select(WorkflowExecution).where(
            WorkflowExecution.id == workflow_execution_id,
            WorkflowExecution.user_id == user_id,
        )
    )
    if execution is None:
        raise HTTPException(status_code=404, detail="Workflow execution was not found")
    return execution


def _owner_id_for_route(
    session: Session, workflow_execution_id: int, current_user: object
) -> int:
    """Resolve direct unit-test calls without weakening HTTP dependency auth."""
    if isinstance(current_user, User):
        return current_user.id
    execution = _get_execution_or_404(session, workflow_execution_id)
    return execution.user_id


def _latest_node(session: Session, workflow_execution_id: int, node_key: str) -> NodeExecution | None:
    return session.scalar(
        select(NodeExecution)
        .where(
            NodeExecution.workflow_execution_id == workflow_execution_id,
            NodeExecution.node_type == node_key,
        )
        .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
    )


def _latest_attempt(session: Session, node_execution_id: int) -> NodeExecutionAttempt | None:
    return session.scalar(
        select(NodeExecutionAttempt)
        .where(NodeExecutionAttempt.node_execution_id == node_execution_id)
        .order_by(NodeExecutionAttempt.attempt_no.desc())
    )


def _node_response(session: Session, node: NodeExecution | None) -> dict[str, object] | None:
    if node is None:
        return None
    attempt = _latest_attempt(session, node.id)
    return {
        "id": node.id,
        "node_type": node.node_type,
        "revision_no": node.revision_no,
        "status": node.status.value,
        "attempt": {
            "current": attempt.attempt_no if attempt is not None else 0,
            "automatic_max": MAX_AUTOMATIC_VIDEO_ATTEMPTS if node.node_type == "video_generation" else 1,
        },
    }


def _workflow_response(session: Session, execution: WorkflowExecution) -> dict[str, object]:
    nodes = {key: _node_response(session, _latest_node(session, execution.id, key)) for key in NODE_KEYS}
    return {
        "id": execution.id,
        "status": execution.status.value,
        "nodes": nodes,
    }


def _current_v3_revision(session: Session, workflow_execution_id: int) -> int:
    revision = session.scalar(
        select(func.max(NodeExecution.revision_no)).where(
            NodeExecution.workflow_execution_id == workflow_execution_id,
        )
    )
    return int(revision or 0)


def _v3_node_for_revision(
    session: Session, workflow_execution_id: int, node_type: str, revision_no: int
) -> NodeExecution | None:
    return session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == workflow_execution_id,
            NodeExecution.node_type == node_type,
            NodeExecution.revision_no == revision_no,
        )
    )


def _v3_attempt_response(session: Session, node: NodeExecution) -> dict[str, int]:
    attempt = _latest_attempt(session, node.id)
    return {
        "current": attempt.attempt_no if attempt is not None else 0,
        "automatic_max": 1,
    }


def _v3_safe_output(node: NodeExecution | None) -> dict[str, object] | None:
    """Expose only UI-safe V3 result fields; prompts and storage keys stay private."""
    if node is None or not isinstance(node.output_data, dict):
        return None
    output = node.output_data
    allowed: dict[str, object]
    if node.node_type == "CONTENT_PLAN":
        allowed = {key: output[key] for key in ("core_message", "slide_structure") if key in output}
    elif node.node_type == "TEXT_GENERATE":
        allowed = {"slides": output.get("slides", [])}
    elif node.node_type in {"TEXT_CHECK", "IMAGE_CHECK"}:
        allowed = {
            key: output[key]
            for key in ("passed", "violations", "feedback")
            if key in output
        }
    else:
        allowed = {}
    return allowed


def _v3_revision_assets(
    session: Session,
    execution: WorkflowExecution,
    image_node: NodeExecution,
) -> dict[int, FileAsset] | None:
    output = image_node.output_data
    asset_ids = output.get("asset_ids") if isinstance(output, dict) else None
    if (
        image_node.status is not NodeExecutionStatus.SUCCEEDED
        or not isinstance(asset_ids, list)
        or len(asset_ids) != 5
        or len(set(asset_ids)) != 5
        or not all(isinstance(asset_id, int) for asset_id in asset_ids)
    ):
        return None

    assets = list(
        session.scalars(
            select(FileAsset).where(
                FileAsset.workflow_execution_id == execution.id,
                FileAsset.asset_type == AssetType.IMAGE,
                FileAsset.slide_no.in_([1, 2, 3, 4, 5]),
                FileAsset.id.in_(asset_ids),
            )
        )
    )
    by_slide = {
        asset.slide_no: asset
        for asset in assets
        if asset.slide_no is not None
    }
    creator_ids = {
        creator_id
        for creator_id in (asset.created_by_node_execution_id for asset in assets)
        if creator_id is not None
    }
    successful_image_node_ids = set(
        session.scalars(
            select(NodeExecution.id).where(
                NodeExecution.id.in_(creator_ids),
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_type == "IMAGE_GENERATE",
                NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
            )
        )
    )
    if (
        set(by_slide) != {1, 2, 3, 4, 5}
        or any(
            asset.created_by_node_execution_id not in successful_image_node_ids
            for asset in by_slide.values()
        )
    ):
        return None
    return by_slide


def _v3_images_for_revision(
    session: Session,
    execution: WorkflowExecution,
    revision_no: int,
) -> list[dict[str, object]]:
    image_node = _v3_node_for_revision(
        session, execution.id, "IMAGE_GENERATE", revision_no
    )
    if image_node is None:
        return []
    by_slide = _v3_revision_assets(session, execution, image_node)
    if by_slide is None:
        return []
    return [
        {
            "slide_no": slide_no,
            "file_name": by_slide[slide_no].file_name,
            "mime_type": by_slide[slide_no].mime_type,
            "image_url": (
                f"/workflow-executions/{execution.id}/slides/{slide_no}/image"
                f"?revision_no={revision_no}"
            ),
        }
        for slide_no in range(1, 6)
    ]


def _v3_workflow_response(session: Session, execution: WorkflowExecution) -> dict[str, object]:
    revision_no = _current_v3_revision(session, execution.id)
    current_image_node = _v3_node_for_revision(
        session, execution.id, "IMAGE_GENERATE", revision_no
    )
    editing_slides = (
        current_image_node.input_data.get("image_slide_numbers")
        if current_image_node is not None and isinstance(current_image_node.input_data, dict)
        and execution.status in {WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.FAILED}
        else None
    )
    prior_image_node = session.scalar(
        select(NodeExecution).where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_GENERATE",
            NodeExecution.revision_no < revision_no,
            NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
        ).order_by(NodeExecution.revision_no.desc()).limit(1)
    )
    nodes: dict[str, dict[str, object] | None] = {}
    for node_type in V3_NODE_KEYS:
        node = _v3_node_for_revision(session, execution.id, node_type, revision_no)
        nodes[node_type] = (
            {
                "id": node.id,
                "node_type": node.node_type,
                "revision_no": node.revision_no,
                "status": node.status.value,
                "attempt": _v3_attempt_response(session, node),
                "result": _v3_safe_output(node),
            }
            if node is not None
            else None
        )
    approval = nodes["HUMAN_APPROVAL"]
    approval_waiting = bool(approval and approval["status"] == NodeExecutionStatus.WAITING.value)
    return {
        "id": execution.id,
        "workflow_execution_id": execution.id,
        "workflow_version": execution.workflow_version,
        "status": execution.status.value,
        "current_node_type": execution.current_node_type,
        "current_revision": revision_no,
        "editing_slides": editing_slides if isinstance(editing_slides, list) else None,
        "nodes": nodes,
        "human_approval": {
            "waiting": approval_waiting,
            "node_execution_id": approval["id"] if approval_waiting and approval else None,
            "status": approval["status"] if approval else None,
        },
        "images": _v3_images_for_revision(session, execution, revision_no),
        "previous_images": (
            _v3_images_for_revision(session, execution, prior_image_node.revision_no)
            if prior_image_node is not None else []
        ),
    }


def _detail_response(session: Session, execution: WorkflowExecution, node_key: str) -> dict[str, object]:
    node = _latest_node(session, execution.id, node_key)
    detail = _node_response(session, node)
    return {
        "workflow_execution_id": execution.id,
        "workflow_status": execution.status.value,
        "node": detail,
        "output": node.output_data if node is not None else None,
    }


def _publication_response(session: Session, publication: SocialPublication) -> dict[str, object]:
    connection = session.get(SocialAccountConnection, publication.social_account_connection_id)
    node = session.get(NodeExecution, publication.node_execution_id)
    attempts = list(session.scalars(select(SocialPublicationAttempt).where(SocialPublicationAttempt.social_publication_id == publication.id).order_by(SocialPublicationAttempt.attempt_no)))
    return {"id": publication.id, "platform": connection.platform.value if connection else "UNKNOWN", "status": publication.status.value, "revision_no": node.revision_no if node else None, "external_post_url": publication.external_post_url, "attempt": {"current": attempts[-1].attempt_no if attempts else 0, "max": 3}}


def _attempt_trace(session: Session, attempt: NodeExecutionAttempt) -> dict[str, object]:
    prompt = session.get(PromptVersion, attempt.prompt_version_id) if attempt.prompt_version_id else None
    return {
        "attempt_no": attempt.attempt_no,
        "status": attempt.status.value,
        "started_at": attempt.started_at,
        "completed_at": attempt.completed_at,
        "ai_company": attempt.ai_company,
        "model": attempt.model,
        "prompt_version": (
            {"id": prompt.id, "node_type": prompt.node_type, "version": prompt.version}
            if prompt is not None
            else None
        ),
        "latency_ms": attempt.latency_ms,
        "input_tokens": attempt.input_tokens,
        "output_tokens": attempt.output_tokens,
        "cost": attempt.cost,
        "error_code": attempt.error_code,
        # Stored diagnostics may contain provider details; external Trace clients
        # receive the durable domain code and a generic safe explanation instead.
        "error_message": "Execution failed; see error_code." if attempt.error_message else None,
    }


def _workflow_trace(session: Session, execution: WorkflowExecution) -> dict[str, object]:
    snapshot = session.scalar(
        select(ExecutionInputSnapshot).where(ExecutionInputSnapshot.workflow_execution_id == execution.id)
    )
    rules = session.get(RulesVersion, snapshot.rules_version_id) if snapshot else None
    nodes = list(session.scalars(
        select(NodeExecution)
        .where(NodeExecution.workflow_execution_id == execution.id)
        .order_by(NodeExecution.revision_no, NodeExecution.id)
    ))
    approvals = list(session.scalars(
        select(UserApproval).where(UserApproval.workflow_execution_id == execution.id).order_by(UserApproval.id)
    ))
    revisions = list(session.scalars(
        select(Revision).where(Revision.workflow_execution_id == execution.id).order_by(Revision.revision_no)
    ))
    publications = list(session.scalars(
        select(SocialPublication)
        .join(NodeExecution, SocialPublication.node_execution_id == NodeExecution.id)
        .where(NodeExecution.workflow_execution_id == execution.id)
        .order_by(SocialPublication.id)
    ))
    return {
        "workflow": {
            "id": execution.id,
            "workflow_key": execution.workflow_key,
            "workflow_version": execution.workflow_version,
            "status": execution.status.value,
            "started_at": execution.started_at,
            "completed_at": execution.completed_at,
        },
        "rules_version": {"id": rules.id, "version": rules.version} if rules else None,
        "nodes": [
            {
                "id": node.id,
                "node_type": node.node_type,
                "revision_no": node.revision_no,
                "status": node.status.value,
                "started_at": node.started_at,
                "completed_at": node.completed_at,
                "attempts": [
                    _attempt_trace(session, attempt) for attempt in session.scalars(
                        select(NodeExecutionAttempt)
                        .where(NodeExecutionAttempt.node_execution_id == node.id)
                        .order_by(NodeExecutionAttempt.attempt_no)
                    )
                ],
            }
            for node in nodes
        ],
        "approvals": [
            {"node_execution_id": item.node_execution_id, "decision": item.decision.value, "decided_at": item.decided_at}
            for item in approvals
        ],
        "revisions": [
            {
                "revision_no": item.revision_no,
                "start_node_type": item.start_node_type,
                "created_at": item.created_at,
            }
            for item in revisions
        ],
        "publications": [
            {
                "id": publication.id,
                "status": publication.status.value,
                "published_at": publication.published_at,
                "attempts": [
                    {
                        "attempt_no": item.attempt_no,
                        "status": item.status.value,
                        "started_at": item.started_at,
                        "finished_at": item.finished_at,
                        "error_code": item.error_code,
                        "error_message": (
                            "Publication failed; see error_code."
                            if item.error_message
                            else None
                        ),
                    }
                    for item in session.scalars(select(SocialPublicationAttempt).where(SocialPublicationAttempt.social_publication_id == publication.id).order_by(SocialPublicationAttempt.attempt_no))
                ],
            }
            for publication in publications
        ],
    }


def _dev_user_or_404(session: Session) -> User:
    user = session.scalar(select(User).where(User.email == "dev@vora.local"))
    if user is None:
        raise HTTPException(status_code=404, detail="Development user was not found")
    return user


# 서버 자체가 정상 실행 중인지 확인하는 API
@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/workflow-executions", response_model=WorkflowListResponse)
def list_workflow_executions(
    limit: int = Query(5, ge=1, le=50),
    cursor: str | None = Query(None),
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    statement = (
        select(WorkflowExecution)
        .where(WorkflowExecution.user_id == current_user.id)
        .order_by(WorkflowExecution.updated_at.desc(), WorkflowExecution.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        updated_at, execution_id = _decode_workflow_list_cursor(cursor)
        statement = statement.where(
            or_(
                WorkflowExecution.updated_at < updated_at,
                and_(
                    WorkflowExecution.updated_at == updated_at,
                    WorkflowExecution.id < execution_id,
                ),
            )
        )
    executions = list(session.scalars(statement))
    has_next = len(executions) > limit
    executions = executions[:limit]
    if not executions:
        return WorkflowListResponse(items=[], next_cursor=None)

    execution_ids = [execution.id for execution in executions]
    snapshots = list(
        session.scalars(
            select(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id.in_(execution_ids)
            )
        )
    )
    snapshots_by_execution = {snapshot.workflow_execution_id: snapshot for snapshot in snapshots}
    thumbnails = _workflow_list_thumbnail_urls(session, executions)
    items = [
        WorkflowListItem(
            workflow_execution_id=execution.id,
            workflow_version=execution.workflow_version,
            status=execution.status.value,
            title=_workflow_list_title(snapshots_by_execution.get(execution.id), execution),
            created_at=execution.created_at,
            updated_at=execution.updated_at,
            completed_at=execution.completed_at,
            thumbnail_url=thumbnails.get(execution.id),
        )
        for execution in executions
    ]
    return WorkflowListResponse(
        items=items,
        next_cursor=_encode_workflow_list_cursor(executions[-1]) if has_next else None,
    )


@app.get("/workflow-executions/{workflow_execution_id}")
def get_workflow_execution(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    execution = _get_owned_execution_or_404(
        session,
        workflow_execution_id,
        _owner_id_for_route(session, workflow_execution_id, current_user),
    )
    if execution.workflow_version == 3:
        return _v3_workflow_response(session, execution)
    return _workflow_response(session, execution)


@app.get("/workflow-executions/{workflow_execution_id}/trace")
def get_workflow_trace(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    authenticated_user_id: int = Depends(get_trace_authenticated_user_id),
):
    """Read durable execution history without exposing prompts, payloads, or credentials."""
    execution = session.scalar(
        select(WorkflowExecution).where(
            WorkflowExecution.id == workflow_execution_id,
            WorkflowExecution.user_id == authenticated_user_id,
        )
    )
    if execution is None:
        raise HTTPException(status_code=404, detail="Workflow execution was not found")
    return _workflow_trace(session, execution)


@app.get("/workflow-executions/{workflow_execution_id}/plan")
def get_workflow_plan(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    return _detail_response(
        session, _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user)), "content_planning"
    )


@app.get("/workflow-executions/{workflow_execution_id}/script")
def get_workflow_script(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    return _detail_response(
        session, _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user)), "script_generation"
    )


@app.get("/workflow-executions/{workflow_execution_id}/video")
def get_workflow_video(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    execution = _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user))
    response = _detail_response(session, execution, "video_generation")
    output = response["output"]
    if isinstance(output, dict) and isinstance(output.get("video_asset_id"), int):
        response["video"] = {
            "stream_url": f"/workflow-executions/{execution.id}/video/stream",
            "download_url": f"/workflow-executions/{execution.id}/video/download",
        }
    else:
        response["video"] = None
    return response


@app.get("/workflow-executions/{workflow_execution_id}/video-generation/events")
def stream_video_generation_events(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    events: VideoProgressEvents = Depends(get_video_progress_events),  # noqa: B008
):
    _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user))
    return StreamingResponse(
        (sse_event(payload) for payload in events.subscribe(workflow_execution_id)),
        media_type="text/event-stream",
    )


@app.get("/workflow-executions/{workflow_execution_id}/publications")
def get_publications(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    execution = _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user))
    publications = list(
        session.scalars(
            select(SocialPublication)
            .join(NodeExecution, SocialPublication.node_execution_id == NodeExecution.id)
            .where(NodeExecution.workflow_execution_id == execution.id)
            .order_by(SocialPublication.id.desc())
        )
    )
    draft = get_publication_service(session, get_llm_provider()).draft(execution)
    return {"draft": draft.__dict__, "publications": [_publication_response(session, item) for item in publications]}


@app.post("/workflow-executions/{workflow_execution_id}/publications")
def publish_video(
    workflow_execution_id: int,
    request: PublishRequest,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    execution = _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user))
    try:
        platforms = [platform_from_string(item) for item in request.platforms]
        if not platforms or len(set(platforms)) != len(platforms):
            raise ValueError("Select one or more unique platforms")
        service = get_publication_service(session, get_llm_provider())
        default = service.draft(execution)
        draft = PublicationDraft(
            request.youtube_title or default.youtube_title,
            request.youtube_description or default.youtube_description,
            request.instagram_caption or default.instagram_caption,
        )
        publications = service.request_publish(
            execution, platforms, draft, request.force_republish
        )
    except (SocialProviderError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"publications": [_publication_response(session, item) for item in publications]}


@app.get("/workflow-executions/{workflow_execution_id}/publication/events")
def stream_publication_events(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user))
    return StreamingResponse(
        (sse_event(payload) for payload in RedisPublicationEvents(settings.redis_url).subscribe(workflow_execution_id)),
        media_type="text/event-stream",
    )


def _oauth_provider(platform: SocialPlatform):
    if platform is SocialPlatform.YOUTUBE:
        return YouTubeProvider(settings.youtube_client_id, settings.youtube_client_secret, settings.youtube_redirect_uri)
    return InstagramProvider(settings.instagram_client_id, settings.instagram_client_secret, settings.instagram_redirect_uri)


@dataclass(frozen=True)
class OAuthStateContext:
    user_id: int
    return_to: str


def _safe_oauth_return_path(return_to: str) -> str:
    """Accept only a local, path-only frontend destination."""
    parsed = urlsplit(return_to)
    if (
        not return_to.startswith("/")
        or return_to.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in return_to
        or "%" in return_to
        or any(ord(character) < 32 or ord(character) == 127 for character in return_to)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise SocialProviderError("Invalid OAuth return path", code="bad_request")
    return parsed.path


def _oauth_state_hash(state: str) -> str:
    return sha256(state.encode("utf-8")).hexdigest()


def _issue_oauth_state(
    session: Session, user: User, platform: SocialPlatform, return_to: str
) -> str:
    state = token_urlsafe(32)
    session.add(
        OAuthAuthorizationState(
            state_hash=_oauth_state_hash(state),
            user_id=user.id,
            platform=platform,
            return_to=return_to,
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.oauth_state_ttl_seconds),
        )
    )
    session.commit()
    return state


def _consume_oauth_state(
    session: Session, state: str | None, platform: SocialPlatform
) -> OAuthStateContext:
    if not state or len(state) > 1024:
        raise SocialProviderError("OAuth state validation failed", code="authorization")
    record = session.scalar(
        select(OAuthAuthorizationState)
        .where(
            OAuthAuthorizationState.state_hash == _oauth_state_hash(state),
            OAuthAuthorizationState.platform == platform,
            OAuthAuthorizationState.consumed_at.is_(None),
        )
        .with_for_update()
    )
    now = datetime.now(UTC)
    if record is None or record.expires_at <= now:
        raise SocialProviderError("OAuth state validation failed", code="authorization")
    context = OAuthStateContext(record.user_id, record.return_to)
    record.consumed_at = now
    # Commit before code exchange so concurrent callbacks cannot both exchange the same code.
    session.commit()
    return context


@app.get("/social/{platform}/authorize")
def social_authorize(
    platform: str,
    return_to: str = "/settings/social",
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    try:
        resolved = platform_from_string(platform)
        safe_return_to = _safe_oauth_return_path(return_to)
        state = _issue_oauth_state(session, current_user, resolved, safe_return_to)
        return {"authorization_url": _oauth_provider(resolved).authorization_url(state)}
    except SocialProviderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _upsert_social_connection(
    session: Session, user: User, platform: SocialPlatform, identity: OAuthIdentity
) -> None:
    connection = session.scalar(select(SocialAccountConnection).where(SocialAccountConnection.user_id == user.id, SocialAccountConnection.platform == platform))
    if connection is None:
        connection = SocialAccountConnection(user_id=user.id, platform=platform, external_account_id=identity.external_account_id, account_name=identity.account_name, access_token=identity.access_token, refresh_token=identity.refresh_token, token_expires_at=identity.token_expires_at)
        session.add(connection)
    else:
        keep_refresh_token = (
            platform is SocialPlatform.YOUTUBE
            and connection.external_account_id == identity.external_account_id
            and identity.refresh_token is None
        )
        connection.external_account_id, connection.account_name = identity.external_account_id, identity.account_name
        connection.access_token = identity.access_token
        if not keep_refresh_token:
            connection.refresh_token = identity.refresh_token
        connection.token_expires_at, connection.status = identity.token_expires_at, SocialConnectionStatus.CONNECTED
    session.commit()


@app.get("/social/{platform}/callback")
def social_browser_callback(
    platform: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),  # noqa: B008
):
    resolved_path = "/settings/social"
    try:
        resolved = platform_from_string(platform)
        saved = _consume_oauth_state(session, state, resolved)
        resolved_path = saved.return_to
        if error or not code:
            raise SocialProviderError("OAuth connection was denied", code="authorization")
        user = session.get(User, saved.user_id)
        if user is None:
            raise SocialProviderError("OAuth user was not found", code="authorization")
        _upsert_social_connection(session, user, resolved, _oauth_provider(resolved).exchange_code(code))
        query = urlencode({"oauth": "success", "platform": resolved.value})
    except SocialProviderError:
        query = urlencode({"oauth": "failed", "platform": platform})
    return RedirectResponse(f"{settings.frontend_url.rstrip('/')}{resolved_path}?{query}")


@app.get("/social/connections")
def social_connections(
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    user = current_user
    connections = list(session.scalars(select(SocialAccountConnection).where(SocialAccountConnection.user_id == user.id)))
    return {"connections": [{"platform": item.platform.value, "account_name": item.account_name, "status": item.status.value} for item in connections]}


# FastAPI가 PostgreSQL에 실제로 연결되는지 확인하는 API
@app.get("/health/db")
def database_health_check():
    # PostgreSQL 연결을 하나 가져옴
    with engine.connect() as connection:
        # DB에 아주 간단한 쿼리를 보내 실제 통신 여부 확인
        connection.execute(text("SELECT 1"))

    # 여기까지 오류 없이 실행되면 DB 연결 성공
    return {
        "status": "ok",
        "database": "connected",
    }


def _video_asset_or_404(session: Session, execution: WorkflowExecution) -> FileAsset:
    video_node = _latest_node(session, execution.id, "video_generation")
    if video_node is None or not isinstance(video_node.output_data, dict):
        raise HTTPException(status_code=404, detail="Generated video was not found")
    asset_id = video_node.output_data.get("video_asset_id")
    if not isinstance(asset_id, int):
        raise HTTPException(status_code=404, detail="Generated video was not found")
    asset = session.get(FileAsset, asset_id)
    if asset is None or asset.workflow_execution_id != execution.id:
        raise HTTPException(status_code=404, detail="Generated video was not found")
    return asset


def _video_file_or_404(asset: FileAsset) -> Path:
    uploads_root = Path(settings.uploads_root).resolve()
    path = (uploads_root / asset.storage_key).resolve()
    try:
        path.relative_to(uploads_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Generated video was not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Generated video was not found")
    return path


def _v3_image_asset_or_404(
    session: Session, execution: WorkflowExecution, slide_no: int,
    revision_no: int | None = None,
) -> FileAsset:
    if execution.workflow_version != 3:
        raise HTTPException(status_code=404, detail="V3 slide image was not found")
    if slide_no not in range(1, 6):
        raise HTTPException(status_code=422, detail="slide_no must be between 1 and 5")
    current_revision = _current_v3_revision(session, execution.id)
    if revision_no is None:
        revision_no = current_revision
    if revision_no < 0 or revision_no > current_revision:
        raise HTTPException(status_code=404, detail="V3 slide image was not found")
    image_node = _v3_node_for_revision(session, execution.id, "IMAGE_GENERATE", revision_no)
    if image_node is None or image_node.status is not NodeExecutionStatus.SUCCEEDED:
        raise HTTPException(status_code=404, detail="V3 slide image was not found")
    assets = _v3_revision_assets(session, execution, image_node)
    asset = assets.get(slide_no) if assets is not None else None
    if asset is None or asset.mime_type not in {"image/png", "image/jpeg"}:
        raise HTTPException(status_code=404, detail="V3 slide image was not found")
    return asset


@app.get("/workflow-executions/{workflow_execution_id}/video/stream")
def stream_generated_video(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    asset = _video_asset_or_404(session, _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user)))
    return FileResponse(_video_file_or_404(asset), media_type=asset.mime_type)


@app.get("/workflow-executions/{workflow_execution_id}/slides/{slide_no}/image")
def get_v3_slide_image(
    workflow_execution_id: int,
    slide_no: int,
    revision_no: int | None = None,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    execution = _get_owned_execution_or_404(
        session,
        workflow_execution_id,
        _owner_id_for_route(session, workflow_execution_id, current_user),
    )
    asset = _v3_image_asset_or_404(session, execution, slide_no, revision_no)
    headers = {"Cache-Control": "private, no-store"}
    storage = get_storage_provider()
    if settings.storage_provider.lower() == "s3":
        try:
            url = storage.presigned_get_url(
                asset.storage_key, settings.instagram_presigned_url_ttl_seconds
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="V3 slide image was not found") from exc
        return RedirectResponse(url, status_code=307, headers=headers)
    path = storage.local_path(asset.storage_key)
    if path is None:
        raise HTTPException(status_code=404, detail="V3 slide image was not found")
    return FileResponse(path, media_type=asset.mime_type, headers=headers)


@app.get("/workflow-executions/{workflow_execution_id}/video/download")
def download_generated_video(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    asset = _video_asset_or_404(session, _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user)))
    return FileResponse(
        _video_file_or_404(asset), media_type=asset.mime_type, filename=asset.file_name
    )


@app.post("/workflow-executions")
async def create_workflow_execution(
    request_text: str = Form(...),
    marketing_info: str = Form(...),
    theme: str | None = Form(default=None),
    rules_version_id: int = Form(...),
    source_image: UploadFile = File(...),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    uploaded_source_image = UploadedInputImage(
        file_name=source_image.filename or "upload",
        content_type=source_image.content_type or "",
        content=await source_image.read(),
    )
    service = WorkflowExecutionService(session, settings.uploads_root)
    try:
        execution = service.create_pending(
            request_text=request_text,
            marketing_info=marketing_info,
            theme=theme,
            rules_version_id=rules_version_id,
            source_image=uploaded_source_image,
            user_id=current_user.id,
        )
        intent = get_generation_dispatcher(session).create(execution.id, ACTION_START)
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WorkflowCreationError as exc:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    get_generation_dispatcher(session).enqueue(intent.id)
    return JSONResponse(
        status_code=202,
        content={
            "workflow_execution_id": execution.id,
            "status": execution.status.value,
            "dispatch_intent_id": intent.id,
        },
    )


@app.post("/workflow-executions/{workflow_execution_id}/revisions")
def create_revision(
    workflow_execution_id: int,
    request: RevisionRequest,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user))
    try:
        services = get_ai_workflow_services(session)
        impact = services.revision_impact.determine(
            request.current_node, request.revision_request
        )
        workflow_engine = get_workflow_engine(session, services)
        status = workflow_engine.start_revision(
            workflow_execution_id,
            impact.data.target_node.value,
            request.revision_request,
        )
        current_node = session.scalars(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id == workflow_execution_id,
                NodeExecution.status == NodeExecutionStatus.WAITING,
            )
            .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
        ).first()
        if status is not WorkflowExecutionStatus.WAITING_APPROVAL or current_node is None:
            raise RuntimeError("Revision did not reach an approval node")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "workflow_execution_id": workflow_execution_id,
        "status": status.value,
        "current_node": current_node.node_type,
        "restart_node": impact.data.target_node.value,
        "current_node_execution_id": current_node.id,
        "result": current_node.output_data,
    }


def _encode_workflow_list_cursor(execution: WorkflowExecution) -> str:
    payload = json.dumps(
        {"updated_at": execution.updated_at.isoformat(), "id": execution.id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_workflow_list_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        updated_at = datetime.fromisoformat(payload["updated_at"])
        execution_id = int(payload["id"])
        if updated_at.tzinfo is None or execution_id <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid workflow list cursor") from exc
    return updated_at, execution_id


def _workflow_list_title(snapshot: ExecutionInputSnapshot | None, execution: WorkflowExecution) -> str:
    if snapshot is not None and snapshot.request_text.strip():
        return snapshot.request_text.strip()[:120]
    return "카드뉴스 프로젝트" if execution.workflow_version == 3 else "콘텐츠 프로젝트"


def _workflow_list_thumbnail_urls(
    session: Session, executions: list[WorkflowExecution]
) -> dict[int, str]:
    execution_ids = [execution.id for execution in executions]
    if not execution_ids:
        return {}
    image_nodes = list(
        session.scalars(
            select(NodeExecution)
            .where(
                NodeExecution.workflow_execution_id.in_(execution_ids),
                NodeExecution.node_type == "IMAGE_GENERATE",
                NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
            )
            .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
        )
    )
    latest_nodes: dict[int, NodeExecution] = {}
    asset_ids: set[int] = set()
    for node in image_nodes:
        if node.workflow_execution_id in latest_nodes:
            continue
        output = node.output_data if isinstance(node.output_data, dict) else {}
        ids = output.get("asset_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(asset_id, int) for asset_id in ids):
            continue
        latest_nodes[node.workflow_execution_id] = node
        asset_ids.update(ids)
    if not asset_ids:
        return {}
    assets = list(
        session.scalars(
            select(FileAsset).where(
                FileAsset.workflow_execution_id.in_(execution_ids),
                FileAsset.asset_type == AssetType.IMAGE,
                FileAsset.id.in_(asset_ids),
                FileAsset.slide_no == 1,
            )
        )
    )
    by_id = {asset.id: asset for asset in assets}
    result: dict[int, str] = {}
    for execution_id, node in latest_nodes.items():
        output = node.output_data if isinstance(node.output_data, dict) else {}
        ids = output.get("asset_ids")
        asset = by_id.get(ids[0]) if isinstance(ids, list) and ids else None
        if asset is not None and asset.workflow_execution_id == execution_id:
            result[execution_id] = f"/workflow-executions/{execution_id}/slides/1/image?revision_no={node.revision_no}"
    return result


@app.post("/workflow-executions/{workflow_execution_id}/revisions/from-completed")
def revise_completed_v3_workflow(
    workflow_execution_id: int,
    request: CompletedRevisionRequest,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    execution = _get_owned_execution_or_404(
        session, workflow_execution_id,
        _owner_id_for_route(session, workflow_execution_id, current_user),
    )
    target_node_key = {
        "PLAN": "CONTENT_PLAN",
        "TEXT": "TEXT_GENERATE",
        "IMAGE": "IMAGE_GENERATE",
    }[request.target]
    try:
        get_workflow_engine(
            session, get_ai_workflow_services(session), execution.workflow_version
        ).prepare_completed_revision(
            execution.id,
            target_node_key,
            request.feedback.strip(),
            image_slide_numbers=request.slide_numbers,
        )
        intent = get_generation_dispatcher(session).create(
            execution.id, ACTION_REVISION, target_node_key, request.feedback.strip()
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Revision was already started") from exc
    except Exception:
        session.rollback()
        raise
    get_generation_dispatcher(session).enqueue(intent.id)
    return JSONResponse(
        status_code=202,
        content={
            "workflow_execution_id": execution.id,
            "status": execution.status.value,
            "dispatch_intent_id": intent.id,
        },
    )


@app.post("/workflow-executions/{workflow_execution_id}/approvals")
def approve_workflow_node(
    workflow_execution_id: int,
    request: ApprovalRequest,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    execution = _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user))
    node = session.get(NodeExecution, request.node_execution_id)
    if (
        node is None
        or node.workflow_execution_id != execution.id
        or node.status is not NodeExecutionStatus.WAITING
    ):
        raise HTTPException(status_code=422, detail="Node is not waiting for approval")
    latest_waiting_node = session.scalar(
        select(NodeExecution)
        .where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.status == NodeExecutionStatus.WAITING,
        )
        .order_by(NodeExecution.revision_no.desc(), NodeExecution.id.desc())
        .limit(1)
    )
    if latest_waiting_node is None or latest_waiting_node.id != node.id:
        raise HTTPException(status_code=422, detail="Node is not the latest approval request")
    if session.scalar(select(UserApproval).where(UserApproval.node_execution_id == node.id)) is not None:
        raise HTTPException(status_code=422, detail="Node already has a user decision")
    if request.decision is ApprovalDecision.APPROVED:
        _ensure_v3_image_check_passed(session, execution, node)

    approval = UserApproval(
        workflow_execution_id=execution.id,
        node_execution_id=node.id,
        decision=request.decision,
    )
    session.add(approval)
    if execution.workflow_version == 3:
        if request.decision is ApprovalDecision.REJECTED:
            target = request.revision_target or request.target
            target_node_key = {
                "PLAN": "CONTENT_PLAN",
                "TEXT": "TEXT_GENERATE",
                "IMAGE": "IMAGE_GENERATE",
            }[target or ""]
            feedback = request.feedback or request.revision_request or ""
            # Engine owns revision/node/attempt state; this API transaction adds
            # the user decision and durable dispatch intent before any worker runs.
            session.flush()
            get_workflow_engine(
                session, get_ai_workflow_services(session), execution.workflow_version
            ).prepare_rejected_revision(
                execution.id,
                target_node_key,
                feedback,
                user_approval_id=approval.id,
                image_slide_numbers=request.slide_numbers,
            )
            intent = get_generation_dispatcher(session).create(
                execution.id, ACTION_REVISION, target_node_key, feedback
            )
        else:
            intent = get_generation_dispatcher(session).create(execution.id, ACTION_RESUME)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(status_code=422, detail="Node already has a user decision") from exc
        except Exception:
            # A failed transaction must not leave a prepared revision visible.
            session.rollback()
            raise
        get_generation_dispatcher(session).enqueue(intent.id)
        return JSONResponse(
            status_code=202,
            content={
                "workflow_execution_id": execution.id,
                "status": execution.status.value,
                "dispatch_intent_id": intent.id,
            },
        )
    if request.decision is ApprovalDecision.REJECTED:
        try:
            # Flush only assigns the approval id. The engine commits this row together
            # with Revision, NodeExecution, and Attempt before provider execution.
            session.flush()
            target_node_key = {
                "PLAN": "CONTENT_PLAN",
                "TEXT": "TEXT_GENERATE",
                "IMAGE": "IMAGE_GENERATE",
            }[request.revision_target or ""]
            status = get_workflow_engine(
                session, get_ai_workflow_services(session)
            ).start_rejected_revision(
                execution.id,
                target_node_key,
                request.revision_request or "",
                user_approval_id=approval.id,
            )
        except (IntegrityError, ValueError) as exc:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"workflow_execution_id": execution.id, "status": status.value}

    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail="Node already has a user decision") from exc
    try:
        status = get_workflow_engine(session, get_ai_workflow_services(session)).resume_after_approval(
            execution.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"workflow_execution_id": execution.id, "status": status.value}


def _ensure_v3_image_check_passed(
    session: Session, execution: WorkflowExecution, approval_node: NodeExecution
) -> None:
    if execution.workflow_version != 3 or approval_node.node_type != "HUMAN_APPROVAL":
        return
    image_check = session.scalar(
        select(NodeExecution)
        .where(
            NodeExecution.workflow_execution_id == execution.id,
            NodeExecution.node_type == "IMAGE_CHECK",
            NodeExecution.revision_no == approval_node.revision_no,
            NodeExecution.status == NodeExecutionStatus.SUCCEEDED,
        )
        .order_by(NodeExecution.id.desc())
        .limit(1)
    )
    output_data = image_check.output_data if image_check is not None else None
    if output_data is None or output_data.get("passed") is not True:
        feedback = output_data.get("feedback") if output_data else None
        detail = "IMAGE_CHECK must pass before approval"
        if isinstance(feedback, str) and feedback:
            detail = f"{detail}: {feedback}"
        raise HTTPException(status_code=422, detail=detail)


@app.post("/workflow-executions/{workflow_execution_id}/retry")
def retry_failed_workflow_execution(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
):
    execution = _get_owned_execution_or_404(session, workflow_execution_id, _owner_id_for_route(session, workflow_execution_id, current_user))
    try:
        status = get_workflow_engine(
            session,
            get_ai_workflow_services(session),
            execution.workflow_version,
        ).retry_failed_execution(execution.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"workflow_execution_id": execution.id, "status": status.value}
