"""add input analysis constraints

Revision ID: 29661fec5f28
Revises: f8d2e9456e7a
Create Date: 2026-09-10 15:51:03.052313
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "29661fec5f28"
down_revision: str | None = "f8d2e9456e7a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "execution_input_snapshots",
        "request_text",
        existing_type=sa.TEXT(),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_execution_input_snapshot_request_text_nonblank",
        "execution_input_snapshots",
        "NULLIF(btrim(request_text), '') IS NOT NULL",
    )
    op.add_column("file_assets", sa.Column("sort_order", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_file_asset_sort_order_positive",
        "file_assets",
        "sort_order IS NULL OR sort_order >= 1",
    )
    op.create_check_constraint(
        "ck_input_image_sort_order_required",
        "file_assets",
        "execution_input_snapshot_id IS NULL OR asset_type <> 'IMAGE' OR sort_order IS NOT NULL",
    )
    op.create_index(
        "uq_file_asset_snapshot_sort_order",
        "file_assets",
        ["execution_input_snapshot_id", "sort_order"],
        unique=True,
        postgresql_where=sa.text("execution_input_snapshot_id IS NOT NULL AND sort_order IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_file_asset_snapshot_sort_order", table_name="file_assets")
    op.drop_constraint("ck_input_image_sort_order_required", "file_assets", type_="check")
    op.drop_constraint("ck_file_asset_sort_order_positive", "file_assets", type_="check")
    op.drop_column("file_assets", "sort_order")
    op.drop_constraint(
        "ck_execution_input_snapshot_request_text_nonblank",
        "execution_input_snapshots",
        type_="check",
    )
    op.alter_column(
        "execution_input_snapshots",
        "request_text",
        existing_type=sa.TEXT(),
        nullable=True,
    )
