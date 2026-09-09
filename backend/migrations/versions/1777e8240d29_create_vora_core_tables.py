"""create vora core tables

Revision ID: 1777e8240d29
Revises: 
Create Date: 2026-09-08 17:02:09.729325
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1777e8240d29"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workflow_key", sa.String(100), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "RUNNING", "WAITING_APPROVAL", "SUCCESS", "FAILED", name="workflow_execution_status"),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "execution_input_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workflow_execution_id", sa.BigInteger(), sa.ForeignKey("workflow_executions.id"), nullable=False, unique=True),
        sa.Column("input_type", sa.Enum("TEXT", "IMAGE", "TEXT_IMAGE", name="input_type"), nullable=False),
        sa.Column("request_text", sa.Text()),
        sa.Column("input_data", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "node_executions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workflow_execution_id", sa.BigInteger(), sa.ForeignKey("workflow_executions.id"), nullable=False),
        sa.Column("node_key", sa.String(100), nullable=False),
        sa.Column("user_requested_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "RUNNING", "WAITING_APPROVAL", "SUCCESS", "FAILED", "RETRYING", name="node_execution_status"),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("input_data", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("output_data", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workflow_execution_id", "node_key", "user_requested_version", name="uq_node_execution_version"),
    )
    op.create_table(
        "file_assets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workflow_execution_id", sa.BigInteger(), sa.ForeignKey("workflow_executions.id"), nullable=False),
        sa.Column("execution_input_snapshot_id", sa.BigInteger(), sa.ForeignKey("execution_input_snapshots.id")),
        sa.Column("node_execution_id", sa.BigInteger(), sa.ForeignKey("node_executions.id")),
        sa.Column("asset_type", sa.Enum("IMAGE", "AUDIO", "VIDEO", "THUMBNAIL", name="asset_type"), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False, unique=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("(execution_input_snapshot_id IS NOT NULL) <> (node_execution_id IS NOT NULL)", name="ck_file_asset_exactly_one_source"),
    )
    op.create_table(
        "node_execution_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("node_execution_id", sa.BigInteger(), sa.ForeignKey("node_executions.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("RUNNING", "SUCCESS", "FAILED", name="node_execution_attempt_status"), nullable=False),
        sa.Column("llm_provider", sa.String(50)),
        sa.Column("llm_model", sa.String(100)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cost", sa.Numeric(12, 6)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("node_execution_id", "attempt_no", name="uq_node_execution_attempt"),
    )
    op.create_table(
        "user_approvals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("node_execution_id", sa.BigInteger(), sa.ForeignKey("node_executions.id"), nullable=False, unique=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decision", sa.Enum("APPROVED", "REVISION_REQUESTED", name="approval_decision"), nullable=False),
        sa.Column("revision_request", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("decision <> 'REVISION_REQUESTED' OR revision_request IS NOT NULL", name="ck_revision_request_required"),
    )
    op.create_table(
        "social_account_connections",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("platform", sa.Enum("YOUTUBE", "INSTAGRAM", name="social_platform"), nullable=False),
        sa.Column("external_account_id", sa.String(255), nullable=False),
        sa.Column("account_name", sa.String(255)),
        sa.Column("status", sa.Enum("CONNECTED", "EXPIRED", "REVOKED", name="social_connection_status"), server_default="CONNECTED", nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text()),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "platform", "external_account_id", name="uq_social_account_connection"),
    )
    op.create_table(
        "social_publications",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("social_account_connection_id", sa.BigInteger(), sa.ForeignKey("social_account_connections.id"), nullable=False),
        sa.Column("file_asset_id", sa.BigInteger(), sa.ForeignKey("file_assets.id"), nullable=False),
        sa.Column("status", sa.Enum("PENDING", "SUCCESS", "FAILED", name="social_publication_status"), server_default="PENDING", nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("external_post_id", sa.String(255)),
        sa.Column("external_post_url", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    for table_name in (
        "social_publications",
        "social_account_connections",
        "user_approvals",
        "node_execution_attempts",
        "file_assets",
        "node_executions",
        "execution_input_snapshots",
        "workflow_executions",
        "users",
    ):
        op.drop_table(table_name)
    for enum_name in (
        "social_publication_status",
        "social_connection_status",
        "social_platform",
        "approval_decision",
        "node_execution_attempt_status",
        "asset_type",
        "node_execution_status",
        "input_type",
        "workflow_execution_status",
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
