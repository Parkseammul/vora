from collections.abc import Iterator

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile

# SQL 문자열을 안전하게 실행하기 위한 SQLAlchemy 함수
from sqlalchemy import text
from sqlalchemy.orm import Session

# database.py에서 만든 PostgreSQL 연결 Engine 가져오기
from app.database import engine
from app.node_executors import RuleBasedNodeExecutor
from app.workflow_definitions import workflow_registry
from app.workflow_engine import WorkflowEngine
from app.workflow_execution_service import (
    UploadedInputImage,
    WorkflowCreationError,
    WorkflowExecutionService,
)

# VORA Backend의 FastAPI 애플리케이션 객체 생성
app = FastAPI()


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


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
    workflow_engine = WorkflowEngine(session, workflow_registry, RuleBasedNodeExecutor())
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
