import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    AssetType,
    ExecutionInputSnapshot,
    FileAsset,
    NodeExecution,
    NodeExecutionStatus,
    RulesVersion,
    User,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.workflow_engine import WorkflowEngine

logger = logging.getLogger(__name__)

DEV_USER_EMAIL = "dev@vora.local"
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024
ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}
UPLOADS_ROOT = Path("uploads")


@dataclass(frozen=True)
class UploadedInputImage:
    file_name: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class WorkflowStartResult:
    workflow_execution: WorkflowExecution
    current_node: NodeExecution


class WorkflowCreationError(Exception):
    pass


class WorkflowExecutionService:
    def __init__(self, session: Session, uploads_root: Path | None = None) -> None:
        self._session = session
        self._uploads_root = uploads_root or UPLOADS_ROOT

    def create_and_start(
        self,
        request_text: str,
        marketing_info: str,
        theme: str | None,
        rules_version_id: int,
        source_image: UploadedInputImage,
        workflow_engine: WorkflowEngine,
    ) -> WorkflowStartResult:
        execution = self.create_pending(
            request_text, marketing_info, theme, rules_version_id, source_image
        )
        input_data = self.input_data(execution.id)
        status = workflow_engine.start_execution(execution.id, input_data)
        if status is not WorkflowExecutionStatus.WAITING_APPROVAL:
            raise WorkflowCreationError("Workflow did not reach human approval")
        current_node = self._session.scalars(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_type == "HUMAN_APPROVAL",
                NodeExecution.status == NodeExecutionStatus.WAITING,
            )
        ).one()
        return WorkflowStartResult(workflow_execution=execution, current_node=current_node)

    def create_pending(
        self,
        request_text: str,
        marketing_info: str,
        theme: str | None,
        rules_version_id: int,
        source_image: UploadedInputImage,
        user_id: int | None = None,
    ) -> WorkflowExecution:
        self._validate_request(request_text, marketing_info, source_image)
        stored_paths: list[Path] = []
        try:
            if self._session.get(RulesVersion, rules_version_id) is None:
                raise WorkflowCreationError("Rules version does not exist")

            user = self._session.get(User, user_id) if user_id is not None else self._session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
            if user is None:
                raise WorkflowCreationError("Development user does not exist; run the seed script first")

            execution = WorkflowExecution(
                user_id=user.id,
                workflow_key="vora_content_creation",
                workflow_version=3,
            )
            self._session.add(execution)
            self._session.flush()
            storage_key, local_path = self._store_image(execution.id, source_image)
            stored_paths.append(local_path)
            source_asset = FileAsset(
                workflow_execution_id=execution.id,
                created_by_node_execution_id=None,
                asset_type=AssetType.IMAGE,
                storage_key=storage_key,
                file_name=source_image.file_name,
                mime_type=source_image.content_type,
                file_size=len(source_image.content),
                slide_no=None,
            )
            self._session.add(source_asset)
            self._session.flush()
            snapshot = ExecutionInputSnapshot(
                workflow_execution_id=execution.id,
                request_text=request_text,
                marketing_info=marketing_info,
                theme=theme,
                source_asset_id=source_asset.id,
                rules_version_id=rules_version_id,
            )
            self._session.add(snapshot)
            # The caller commits this together with its durable dispatch intent.
            self._session.flush()
        except (OSError, SQLAlchemyError, WorkflowCreationError) as exc:
            self._session.rollback()
            self._cleanup_files(stored_paths)
            if isinstance(exc, WorkflowCreationError):
                raise
            raise WorkflowCreationError("Failed to persist workflow input") from exc

        return execution

    def input_data(self, workflow_execution_id: int) -> dict[str, object]:
        return self._assemble_input_data(workflow_execution_id)

    def _assemble_input_data(self, workflow_execution_id: int) -> dict[str, object]:
        snapshot = self._session.scalar(
            select(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id == workflow_execution_id
            )
        )
        if snapshot is None:
            raise WorkflowCreationError("Execution input snapshot does not exist")
        return {
            "request_text": snapshot.request_text,
            "marketing_info": snapshot.marketing_info,
            "theme": snapshot.theme,
            "source_asset_id": snapshot.source_asset_id,
            "rules_version_id": snapshot.rules_version_id,
        }

    def _store_image(self, workflow_execution_id: int, image: UploadedInputImage) -> tuple[str, Path]:
        extension = ALLOWED_IMAGE_CONTENT_TYPES[image.content_type]
        filename = f"{uuid4()}{extension}"
        storage_key = f"inputs/{workflow_execution_id}/{filename}"
        local_path = self._uploads_root / storage_key
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(image.content)
        return storage_key, local_path

    @staticmethod
    def _validate_request(
        request_text: str, marketing_info: str, source_image: UploadedInputImage
    ) -> None:
        if not request_text.strip():
            raise ValueError("request_text must not be blank")
        if not marketing_info.strip():
            raise ValueError("marketing_info must not be blank")
        if source_image.content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
            raise ValueError("Only JPEG and PNG images are allowed")
        if len(source_image.content) > MAX_IMAGE_SIZE_BYTES:
            raise ValueError("Source image must be 10MB or smaller")

    @staticmethod
    def _cleanup_files(paths: list[Path]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to clean up input file after database failure: %s", path)
