"""Add generation-dispatch fencing ownership fields.

Revision ID: b2c4d6e8f3a1
Revises: f1a6d2c3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c4d6e8f3a1"
down_revision: str | None = "f1a6d2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflow_dispatch_intents",
        sa.Column("claim_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("workflow_dispatch_intents", sa.Column("claim_token", sa.String(36)))


def downgrade() -> None:
    op.drop_column("workflow_dispatch_intents", "claim_token")
    op.drop_column("workflow_dispatch_intents", "claim_generation")
