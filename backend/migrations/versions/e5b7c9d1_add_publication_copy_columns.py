"""Persist the Instagram caption with the durable V3 publication intent.

Revision ID: e5b7c9d1
Revises: d4f6a910
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b7c9d1"
down_revision: str | None = "d4f6a910"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # c3f4a9b7e2d1's upgrade unconditionally removes all legacy copy columns.
    # This revision descends from that migration through d4f6a910, so this
    # column is owned by e5b7c9d1 and downgrade may safely remove only it.
    op.add_column("social_publications", sa.Column("instagram_caption", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("social_publications", "instagram_caption")
