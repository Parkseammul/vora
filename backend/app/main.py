from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, field_validator

# SQL 문자열을 안전하게 실행하기 위한 SQLAlchemy 함수
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings
from app.content_planning import ContentPlanningService

# database.py에서 만든 PostgreSQL 연결 Engine 가져오기
from app.database import engine
from app.e2e_providers import (
    DeterministicE2ELLMProvider,
    FFmpegE2ETTSProvider,
    FFmpegE2EVideoProvider,
)
from app.llm_provider import LLMProvider
from app.media_providers import (
    ElevenLabsHTTPClient,
    ElevenLabsTTSProvider,
    FFmpegVideoComposer,
    RunwayHTTPClient,
    RunwayVideoProvider,
)
from app.models import (
    ApprovalDecision,
    FileAsset,
    NodeExecution,
    NodeExecutionAttempt,
    NodeExecutionStatus,
    UserApproval,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.node_executors import RuleBasedNodeExecutor
from app.revision_impact import CoreNodeKey, RevisionImpactService
from app.script_generation import ScriptGenerationService
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


NODE_KEYS = ("input_analysis", "content_planning", "script_generation", "video_generation")
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


def configure_llm_provider(llm_provider: LLMProvider) -> None:
    """Application bootstrap injects the concrete vendor adapter once."""
    app.state.llm_provider = llm_provider


def configure_media_providers(
    video_provider: VideoProvider, tts_provider: TTSProvider, composer: VideoComposer
) -> None:
    app.state.media_providers = (video_provider, tts_provider, composer)


if settings.e2e_fake_providers:
    configure_llm_provider(DeterministicE2ELLMProvider())
    configure_media_providers(
        FFmpegE2EVideoProvider(),
        FFmpegE2ETTSProvider(),
        FFmpegVideoComposer(settings.ffmpeg_subtitle_font_path),
    )
elif settings.runway_api_key and settings.elevenlabs_api_key and settings.elevenlabs_voice_id:
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
        get_video_task_dispatcher(),
    )


def _get_execution_or_404(session: Session, workflow_execution_id: int) -> WorkflowExecution:
    execution = session.get(WorkflowExecution, workflow_execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Workflow execution was not found")
    return execution


def _latest_node(session: Session, workflow_execution_id: int, node_key: str) -> NodeExecution | None:
    return session.scalar(
        select(NodeExecution)
        .where(
            NodeExecution.workflow_execution_id == workflow_execution_id,
            NodeExecution.node_key == node_key,
        )
        .order_by(NodeExecution.user_requested_version.desc(), NodeExecution.id.desc())
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
        "node_key": node.node_key,
        "user_requested_version": node.user_requested_version,
        "status": node.status.value,
        "attempt": {
            "current": attempt.attempt_no if attempt is not None else 0,
            "automatic_max": MAX_AUTOMATIC_VIDEO_ATTEMPTS if node.node_key == "video_generation" else 1,
        },
    }


def _workflow_response(session: Session, execution: WorkflowExecution) -> dict[str, object]:
    nodes = {key: _node_response(session, _latest_node(session, execution.id, key)) for key in NODE_KEYS}
    return {
        "id": execution.id,
        "status": execution.status.value,
        "nodes": nodes,
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


# 서버 자체가 정상 실행 중인지 확인하는 API
@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/workflow-executions/{workflow_execution_id}")
def get_workflow_execution(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
):
    return _workflow_response(session, _get_execution_or_404(session, workflow_execution_id))


@app.get("/workflow-executions/{workflow_execution_id}/plan")
def get_workflow_plan(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
):
    return _detail_response(
        session, _get_execution_or_404(session, workflow_execution_id), "content_planning"
    )


@app.get("/workflow-executions/{workflow_execution_id}/script")
def get_workflow_script(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
):
    return _detail_response(
        session, _get_execution_or_404(session, workflow_execution_id), "script_generation"
    )


@app.get("/workflow-executions/{workflow_execution_id}/video")
def get_workflow_video(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
):
    execution = _get_execution_or_404(session, workflow_execution_id)
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
    events: VideoProgressEvents = Depends(get_video_progress_events),  # noqa: B008
):
    return StreamingResponse(
        (sse_event(payload) for payload in events.subscribe(workflow_execution_id)),
        media_type="text/event-stream",
    )


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


@app.get("/workflow-executions/{workflow_execution_id}/video/stream")
def stream_generated_video(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
):
    asset = _video_asset_or_404(session, _get_execution_or_404(session, workflow_execution_id))
    return FileResponse(_video_file_or_404(asset), media_type=asset.mime_type)


@app.get("/workflow-executions/{workflow_execution_id}/video/download")
def download_generated_video(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
):
    asset = _video_asset_or_404(session, _get_execution_or_404(session, workflow_execution_id))
    return FileResponse(
        _video_file_or_404(asset), media_type=asset.mime_type, filename=asset.file_name
    )


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
        "restart_node": impact.data.target_node.value,
        "current_node_execution_id": current_node.id,
        "result": current_node.output_data,
    }


@app.post("/workflow-executions/{workflow_execution_id}/approvals")
def approve_workflow_node(
    workflow_execution_id: int,
    request: ApprovalRequest,
    session: Session = Depends(get_session),  # noqa: B008
):
    execution = _get_execution_or_404(session, workflow_execution_id)
    node = session.get(NodeExecution, request.node_execution_id)
    if (
        node is None
        or node.workflow_execution_id != execution.id
        or node.status is not NodeExecutionStatus.WAITING_APPROVAL
    ):
        raise HTTPException(status_code=422, detail="Node is not waiting for approval")
    if session.scalar(select(UserApproval).where(UserApproval.node_execution_id == node.id)) is not None:
        raise HTTPException(status_code=422, detail="Node already has a user decision")

    session.add(
        UserApproval(
            node_execution_id=node.id,
            user_id=execution.user_id,
            decision=ApprovalDecision.APPROVED,
        )
    )
    session.commit()
    try:
        status = get_workflow_engine(session, get_ai_workflow_services(session)).resume_after_approval(
            execution.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"workflow_execution_id": execution.id, "status": status.value}


@app.post("/workflow-executions/{workflow_execution_id}/retry")
def retry_failed_workflow_execution(
    workflow_execution_id: int,
    session: Session = Depends(get_session),  # noqa: B008
):
    execution = _get_execution_or_404(session, workflow_execution_id)
    try:
        status = get_workflow_engine(session, get_ai_workflow_services(session)).retry_failed_execution(
            execution.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"workflow_execution_id": execution.id, "status": status.value}
