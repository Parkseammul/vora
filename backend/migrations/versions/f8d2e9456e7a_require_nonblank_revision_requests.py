"""require nonblank revision requests

Revision ID: f8d2e9456e7a
Revises: 1777e8240d29
Create Date: 2026-09-08 17:51:01.764354
"""
from collections.abc import Sequence

from alembic import op

revision: str = "f8d2e9456e7a"
down_revision: str | None = "1777e8240d29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_revision_request_required", "user_approvals", type_="check")
    op.create_check_constraint(
        "ck_revision_request_required",
        "user_approvals",
        "decision <> 'REVISION_REQUESTED' OR NULLIF(btrim(revision_request), '') IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_revision_request_required", "user_approvals", type_="check")
    op.create_check_constraint(
        "ck_revision_request_required",
        "user_approvals",
        "decision <> 'REVISION_REQUESTED' OR revision_request IS NOT NULL",
    )
