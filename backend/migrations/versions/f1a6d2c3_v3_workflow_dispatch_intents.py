"""add durable V3 workflow dispatch intents

Revision ID: f1a6d2c3
Revises: d4f6a910
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a6d2c3"
down_revision: str | None = "e5b7c9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_dispatch_intents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workflow_execution_id", sa.BigInteger(), sa.ForeignKey("workflow_executions.id"), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("revision_target", sa.String(100)),
        sa.Column("revision_request", sa.Text()),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("dispatch_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("celery_task_id", sa.String(255)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_workflow_dispatch_intents_ready", "workflow_dispatch_intents", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_workflow_dispatch_intents_ready", table_name="workflow_dispatch_intents")
    op.drop_table("workflow_dispatch_intents")
