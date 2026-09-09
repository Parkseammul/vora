import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorkflowExecutionStatus(enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class InputType(enum.Enum):
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    TEXT_IMAGE = "TEXT_IMAGE"


class AssetType(enum.Enum):
    IMAGE = "IMAGE"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"
    THUMBNAIL = "THUMBNAIL"


class NodeExecutionStatus(enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class NodeExecutionAttemptStatus(enum.Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ApprovalDecision(enum.Enum):
    APPROVED = "APPROVED"
    REVISION_REQUESTED = "REVISION_REQUESTED"


class SocialPlatform(enum.Enum):
    YOUTUBE = "YOUTUBE"
    INSTAGRAM = "INSTAGRAM"


class SocialConnectionStatus(enum.Enum):
    CONNECTED = "CONNECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class SocialPublicationStatus(enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    workflow_key: Mapped[str] = mapped_column(String(100), nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[WorkflowExecutionStatus] = mapped_column(
        Enum(WorkflowExecutionStatus, name="workflow_execution_status"),
        nullable=False,
        server_default=WorkflowExecutionStatus.PENDING.value,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExecutionInputSnapshot(Base):
    __tablename__ = "execution_input_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workflow_execution_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id"), nullable=False, unique=True
    )
    input_type: Mapped[InputType] = mapped_column(
        Enum(InputType, name="input_type"), nullable=False
    )
    request_text: Mapped[str | None] = mapped_column(Text)
    input_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NodeExecution(Base):
    __tablename__ = "node_executions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_execution_id",
            "node_key",
            "user_requested_version",
            name="uq_node_execution_version",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workflow_execution_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id"), nullable=False
    )
    node_key: Mapped[str] = mapped_column(String(100), nullable=False)
    user_requested_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    status: Mapped[NodeExecutionStatus] = mapped_column(
        Enum(NodeExecutionStatus, name="node_execution_status"),
        nullable=False,
        server_default=NodeExecutionStatus.PENDING.value,
    )
    input_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class FileAsset(Base):
    __tablename__ = "file_assets"
    __table_args__ = (
        CheckConstraint(
            "(execution_input_snapshot_id IS NOT NULL) <> (node_execution_id IS NOT NULL)",
            name="ck_file_asset_exactly_one_source",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workflow_execution_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id"), nullable=False
    )
    execution_input_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_input_snapshots.id")
    )
    node_execution_id: Mapped[int | None] = mapped_column(ForeignKey("node_executions.id"))
    asset_type: Mapped[AssetType] = mapped_column(
        Enum(AssetType, name="asset_type"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class NodeExecutionAttempt(Base):
    __tablename__ = "node_execution_attempts"
    __table_args__ = (
        UniqueConstraint("node_execution_id", "attempt_no", name="uq_node_execution_attempt"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    node_execution_id: Mapped[int] = mapped_column(
        ForeignKey("node_executions.id"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[NodeExecutionAttemptStatus] = mapped_column(
        Enum(NodeExecutionAttemptStatus, name="node_execution_attempt_status"), nullable=False
    )
    llm_provider: Mapped[str | None] = mapped_column(String(50))
    llm_model: Mapped[str | None] = mapped_column(String(100))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserApproval(Base):
    __tablename__ = "user_approvals"
    __table_args__ = (
        CheckConstraint(
            "decision <> 'REVISION_REQUESTED' OR NULLIF(btrim(revision_request), '') IS NOT NULL",
            name="ck_revision_request_required",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    node_execution_id: Mapped[int] = mapped_column(
        ForeignKey("node_executions.id"), nullable=False, unique=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(ApprovalDecision, name="approval_decision"), nullable=False
    )
    revision_request: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SocialAccountConnection(Base):
    __tablename__ = "social_account_connections"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "platform", "external_account_id", name="uq_social_account_connection"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    platform: Mapped[SocialPlatform] = mapped_column(
        Enum(SocialPlatform, name="social_platform"), nullable=False
    )
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[SocialConnectionStatus] = mapped_column(
        Enum(SocialConnectionStatus, name="social_connection_status"),
        nullable=False,
        server_default=SocialConnectionStatus.CONNECTED.value,
    )
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SocialPublication(Base):
    __tablename__ = "social_publications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    social_account_connection_id: Mapped[int] = mapped_column(
        ForeignKey("social_account_connections.id"), nullable=False
    )
    file_asset_id: Mapped[int] = mapped_column(ForeignKey("file_assets.id"), nullable=False)
    status: Mapped[SocialPublicationStatus] = mapped_column(
        Enum(SocialPublicationStatus, name="social_publication_status"),
        nullable=False,
        server_default=SocialPublicationStatus.PENDING.value,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    external_post_id: Mapped[str | None] = mapped_column(String(255))
    external_post_url: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
