"""add publication attempts and publishing status

Revision ID: 9a15b001
Revises: 29661fec5f28
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a15b001"
down_revision: str | None = "29661fec5f28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL enum values must be committed before they can be used by a column.
    op.execute("ALTER TYPE social_publication_status ADD VALUE IF NOT EXISTS 'PUBLISHING'")
    op.drop_constraint("uq_social_account_connection", "social_account_connections", type_="unique")
    op.create_unique_constraint(
        "uq_social_account_connection_platform",
        "social_account_connections",
        ["user_id", "platform"],
    )
    op.add_column("social_publications", sa.Column("youtube_title", sa.String(100)))
    op.add_column("social_publications", sa.Column("youtube_description", sa.Text()))
    op.add_column("social_publications", sa.Column("instagram_caption", sa.Text()))
    op.create_table(
        "social_publication_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("social_publication_id", sa.BigInteger(), sa.ForeignKey("social_publications.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("RUNNING", "SUCCESS", "FAILED", name="social_publication_attempt_status"), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("social_publication_id", "attempt_no", name="uq_social_publication_attempt"),
    )


def downgrade() -> None:
    op.drop_table("social_publication_attempts")
    op.drop_column("social_publications", "instagram_caption")
    op.drop_column("social_publications", "youtube_description")
    op.drop_column("social_publications", "youtube_title")
    op.drop_constraint("uq_social_account_connection_platform", "social_account_connections", type_="unique")
    op.create_unique_constraint(
        "uq_social_account_connection",
        "social_account_connections",
        ["user_id", "platform", "external_account_id"],
    )
    # Existing PUBLISHING rows must be resolved before downgrading this enum in production.
