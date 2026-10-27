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
    REVISING = "REVISING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class AssetType(enum.Enum):
    IMAGE = "IMAGE"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"
    THUMBNAIL = "THUMBNAIL"


class NodeExecutionStatus(enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class NodeExecutionAttemptStatus(enum.Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ApprovalDecision(enum.Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SocialPlatform(enum.Enum):
    YOUTUBE = "YOUTUBE"
    INSTAGRAM = "INSTAGRAM"


class SocialConnectionStatus(enum.Enum):
    CONNECTED = "CONNECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class OAuthAuthorizationState(Base):
    """One-time OAuth authorization state; the raw browser value is never persisted."""

    __tablename__ = "oauth_authorization_states"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    platform: Mapped[SocialPlatform] = mapped_column(
        Enum(SocialPlatform, name="social_platform"), nullable=False
    )
    return_to: Mapped[str] = mapped_column(String(500), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SocialPublicationStatus(enum.Enum):
    PENDING = "PENDING"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    DELETED = "DELETED"


class SocialPublicationAttemptStatus(enum.Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("cognito_issuer", "cognito_sub", name="uq_user_cognito_identity"),
        CheckConstraint(
            "(cognito_issuer IS NULL AND cognito_sub IS NULL) OR "
            "(cognito_issuer IS NOT NULL AND cognito_sub IS NOT NULL)",
            name="ck_user_cognito_identity_pair",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    cognito_issuer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cognito_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    current_node_type: Mapped[str | None] = mapped_column(String(30))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExecutionInputSnapshot(Base):
    __tablename__ = "execution_input_snapshots"
    __table_args__ = (
        CheckConstraint(
            "NULLIF(btrim(request_text), '') IS NOT NULL",
            name="ck_execution_input_snapshot_request_text_nonblank",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workflow_execution_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id"), nullable=False, unique=True
    )
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    marketing_info: Mapped[str] = mapped_column(Text, nullable=False)
    theme: Mapped[str | None] = mapped_column(Text)
    source_asset_id: Mapped[int] = mapped_column(ForeignKey("file_assets.id"), nullable=False)
    rules_version_id: Mapped[int] = mapped_column(ForeignKey("rules_versions.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NodeExecution(Base):
    __tablename__ = "node_executions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_execution_id",
            "node_type",
            "revision_no",
            name="uq_node_execution_revision",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workflow_execution_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id"), nullable=False
    )
    node_type: Mapped[str] = mapped_column(String(100), nullable=False)
    revision_no: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
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
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
            "slide_no IS NULL OR slide_no >= 1",
            name="ck_file_asset_slide_no_positive",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workflow_execution_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id"), nullable=False
    )
    created_by_node_execution_id: Mapped[int | None] = mapped_column(
        ForeignKey("node_executions.id")
    )
    asset_type: Mapped[AssetType] = mapped_column(
        Enum(AssetType, name="asset_type"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    slide_no: Mapped[int | None] = mapped_column(Integer)
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
    ai_company: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version_id: Mapped[int | None] = mapped_column(ForeignKey("prompt_versions.id"))
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
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserApproval(Base):
    __tablename__ = "user_approvals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workflow_execution_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id"), nullable=False
    )
    node_execution_id: Mapped[int] = mapped_column(
        ForeignKey("node_executions.id"), nullable=False, unique=True
    )
    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(ApprovalDecision, name="approval_decision"), nullable=False
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SocialAccountConnection(Base):
    __tablename__ = "social_account_connections"
    __table_args__ = (
        # MVP supports one account per platform; OAuth reconnect refreshes this row.
        UniqueConstraint("user_id", "platform", name="uq_social_account_connection_platform"),
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
    node_execution_id: Mapped[int] = mapped_column(
        ForeignKey("node_executions.id"), nullable=False
    )
    social_account_connection_id: Mapped[int] = mapped_column(
        ForeignKey("social_account_connections.id"), nullable=False
    )
    status: Mapped[SocialPublicationStatus] = mapped_column(
        Enum(SocialPublicationStatus, name="social_publication_status"),
        nullable=False,
        server_default=SocialPublicationStatus.PENDING.value,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    instagram_caption: Mapped[str | None] = mapped_column(Text)
    external_post_id: Mapped[str | None] = mapped_column(String(255))
    external_post_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SocialPublicationAttempt(Base):
    __tablename__ = "social_publication_attempts"
    __table_args__ = (
        UniqueConstraint("social_publication_id", "attempt_no", name="uq_social_publication_attempt"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    social_publication_id: Mapped[int] = mapped_column(
        ForeignKey("social_publications.id"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[SocialPublicationAttemptStatus] = mapped_column(
        Enum(SocialPublicationAttemptStatus, name="social_publication_attempt_status"), nullable=False
    )
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


class RulesVersion(Base):
    __tablename__ = "rules_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    rules_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("node_type", "version", name="uq_prompt_version_node_type_version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    node_type: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (
        UniqueConstraint("workflow_execution_id", "revision_no", name="uq_revision_workflow_revision"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workflow_execution_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id"), nullable=False
    )
    user_approval_id: Mapped[int] = mapped_column(
        ForeignKey("user_approvals.id"), nullable=False, unique=True
    )
    # User revisions start at 1; initial execution and technical retries create no Revision row.
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    start_node_type: Mapped[str] = mapped_column(String(100), nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkflowDispatchIntent(Base):
    """Durable V3 generation dispatch record; Redis is never the source of truth."""

    __tablename__ = "workflow_dispatch_intents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workflow_execution_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    revision_target: Mapped[str | None] = mapped_column(String(100))
    revision_request: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="PENDING")
    dispatch_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    celery_task_id: Mapped[str | None] = mapped_column(String(255))
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    claim_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
