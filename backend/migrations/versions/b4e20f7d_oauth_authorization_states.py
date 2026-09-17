"""persist one-time OAuth authorization states

Revision ID: b4e20f7d
Revises: 9a15b001
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4e20f7d"
down_revision: str | None = "9a15b001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_authorization_states",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "platform",
            postgresql.ENUM("YOUTUBE", "INSTAGRAM", name="social_platform", create_type=False),
            nullable=False,
        ),
        sa.Column("return_to", sa.String(500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("oauth_authorization_states")
