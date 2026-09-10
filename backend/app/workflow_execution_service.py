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
    InputType,
    NodeExecution,
    NodeExecutionStatus,
    User,
    WorkflowExecution,
    WorkflowExecutionStatus,
)
from app.workflow_engine import WorkflowEngine

logger = logging.getLogger(__name__)

DEV_USER_EMAIL = "dev@vora.local"
MAX_IMAGES = 6
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
        images: list[UploadedInputImage],
        workflow_engine: WorkflowEngine,
    ) -> WorkflowStartResult:
        self._validate_request(request_text, images)
        stored_paths: list[Path] = []
        try:
            user = self._session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
            if user is None:
                raise WorkflowCreationError("Development user does not exist; run the seed script first")

            execution = WorkflowExecution(
                user_id=user.id,
                workflow_key="vora_content_creation",
                workflow_version=1,
            )
            self._session.add(execution)
            self._session.flush()
            snapshot = ExecutionInputSnapshot(
                workflow_execution_id=execution.id,
                input_type=InputType.TEXT_IMAGE if images else InputType.TEXT,
                request_text=request_text,
                input_data={},
            )
            self._session.add(snapshot)
            self._session.flush()

            for sort_order, image in enumerate(images, start=1):
                storage_key, local_path = self._store_image(execution.id, image)
                stored_paths.append(local_path)
                self._session.add(
                    FileAsset(
                        workflow_execution_id=execution.id,
                        execution_input_snapshot_id=snapshot.id,
                        asset_type=AssetType.IMAGE,
                        storage_key=storage_key,
                        file_name=image.file_name,
                        mime_type=image.content_type,
                        file_size=len(image.content),
                        sort_order=sort_order,
                    )
                )
            # Files and input rows are durable before the engine reconstructs its input from DB.
            self._session.commit()
        except (OSError, SQLAlchemyError, WorkflowCreationError) as exc:
            self._session.rollback()
            self._cleanup_files(stored_paths)
            if isinstance(exc, WorkflowCreationError):
                raise
            raise WorkflowCreationError("Failed to persist workflow input") from exc

        input_data = self._assemble_input_data(execution.id)
        status = workflow_engine.start_execution(execution.id, input_data)
        if status is not WorkflowExecutionStatus.WAITING_APPROVAL:
            raise WorkflowCreationError("Workflow did not reach content planning approval")
        current_node = self._session.scalars(
            select(NodeExecution).where(
                NodeExecution.workflow_execution_id == execution.id,
                NodeExecution.node_key == "content_planning",
                NodeExecution.status == NodeExecutionStatus.WAITING_APPROVAL,
            )
        ).one()
        return WorkflowStartResult(workflow_execution=execution, current_node=current_node)

    def _assemble_input_data(self, workflow_execution_id: int) -> dict[str, object]:
        snapshot = self._session.scalar(
            select(ExecutionInputSnapshot).where(
                ExecutionInputSnapshot.workflow_execution_id == workflow_execution_id
            )
        )
        if snapshot is None:
            raise WorkflowCreationError("Execution input snapshot does not exist")
        source_asset_ids = list(
            self._session.scalars(
                select(FileAsset.id)
                .where(
                    FileAsset.execution_input_snapshot_id == snapshot.id,
                    FileAsset.asset_type == AssetType.IMAGE,
                )
                .order_by(FileAsset.sort_order)
            )
        )
        return {
            "request_text": snapshot.request_text,
            "input_type": snapshot.input_type.value,
            "source_asset_ids": source_asset_ids,
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
    def _validate_request(request_text: str, images: list[UploadedInputImage]) -> None:
        if not request_text.strip():
            raise ValueError("request_text must not be blank")
        if len(images) > MAX_IMAGES:
            raise ValueError("At most 6 images are allowed")
        for image in images:
            if image.content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
                raise ValueError("Only JPEG and PNG images are allowed")
            if len(image.content) > MAX_IMAGE_SIZE_BYTES:
                raise ValueError("Each image must be 10MB or smaller")

    @staticmethod
    def _cleanup_files(paths: list[Path]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to clean up input file after database failure: %s", path)
