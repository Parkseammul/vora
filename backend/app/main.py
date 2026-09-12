from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, field_validator

# SQL 문자열을 안전하게 실행하기 위한 SQLAlchemy 함수
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.content_planning import ContentPlanningService

# database.py에서 만든 PostgreSQL 연결 Engine 가져오기
from app.database import engine
from app.llm_provider import LLMProvider
from app.media_providers import (
    ElevenLabsHTTPClient,
    ElevenLabsTTSProvider,
    FFmpegVideoComposer,
    RunwayHTTPClient,
    RunwayVideoProvider,
)
from app.models import NodeExecution, NodeExecutionStatus, WorkflowExecutionStatus
from app.node_executors import RuleBasedNodeExecutor
from app.revision_impact import CoreNodeKey, RevisionImpactService
from app.script_generation import ScriptGenerationService
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


class RevisionRequest(BaseModel):
    current_node: CoreNodeKey
    revision_request: str

    @field_validator("revision_request")
    @classmethod
    def revision_request_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("revision_request must not be blank")
        return value


@dataclass(frozen=True)
class AIWorkflowServices:
    content_planning: ContentPlanningService
    script_generation: ScriptGenerationService
    revision_impact: RevisionImpactService
    video_generation: VideoGenerationService | None


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def configure_llm_provider(llm_provider: LLMProvider) -> None:
    """Application bootstrap injects the concrete vendor adapter once."""
    app.state.llm_provider = llm_provider


def configure_media_providers(
    video_provider: VideoProvider, tts_provider: TTSProvider, composer: VideoComposer
) -> None:
    app.state.media_providers = (video_provider, tts_provider, composer)


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
        FFmpegVideoComposer(),
    )


def get_llm_provider() -> LLMProvider:
    llm_provider = getattr(app.state, "llm_provider", None)
    if llm_provider is None:
        raise RuntimeError("LLMProvider is not configured")
    return llm_provider


def get_ai_workflow_services(session: Session) -> AIWorkflowServices:
    llm_provider = get_llm_provider()
    media = getattr(app.state, "media_providers", None)
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
        video_generation=(
            VideoGenerationService(
                session, media[0], media[1], media[2], settings.uploads_root, settings.fixed_bgm_asset_id
            )
            if media is not None
            else None
        ),
    )


def get_workflow_engine(session: Session, services: AIWorkflowServices) -> WorkflowEngine:
    return WorkflowEngine(
        session,
        workflow_registry,
        RuleBasedNodeExecutor(
            content_planning_service=services.content_planning,
            script_generation_service=services.script_generation,
            video_generation_service=services.video_generation,
            session=session,
        ),
    )


# 서버 자체가 정상 실행 중인지 확인하는 API
@app.get("/health")
def health_check():
    return {"status": "ok"}


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


@app.post("/workflow-executions")
async def create_workflow_execution(
    request_text: str = Form(...),
    images: list[UploadFile] | None = File(default=None),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
):
    uploaded_images = [
        UploadedInputImage(
            file_name=image.filename or "upload",
            content_type=image.content_type or "",
            content=await image.read(),
        )
        for image in images or []
    ]
    workflow_engine = get_workflow_engine(session, get_ai_workflow_services(session))
    service = WorkflowExecutionService(session)
    try:
        result = service.create_and_start(request_text, uploaded_images, workflow_engine)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WorkflowCreationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "workflow_execution_id": result.workflow_execution.id,
        "status": result.workflow_execution.status.value,
        "current_node": result.current_node.node_key,
        "current_node_execution_id": result.current_node.id,
        "result": result.current_node.output_data,
    }


@app.post("/workflow-executions/{workflow_execution_id}/revisions")
def create_revision(
    workflow_execution_id: int,
    request: RevisionRequest,
    session: Session = Depends(get_session),  # noqa: B008
):
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
                NodeExecution.status == NodeExecutionStatus.WAITING_APPROVAL,
            )
            .order_by(NodeExecution.user_requested_version.desc(), NodeExecution.id.desc())
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
        "current_node": current_node.node_key,
        "current_node_execution_id": current_node.id,
        "result": current_node.output_data,
    }
