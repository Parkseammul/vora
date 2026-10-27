"""add Cognito identity mapping to users

Revision ID: c7e8f9a0b1c2
Revises: b2c4d6e8f3a1
"""

import sqlalchemy as sa
from alembic import op

revision = "c7e8f9a0b1c2"
down_revision = "b2c4d6e8f3a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("cognito_issuer", sa.String(length=500), nullable=True))
    op.add_column("users", sa.Column("cognito_sub", sa.String(length=255), nullable=True))
    op.create_unique_constraint("uq_user_cognito_identity", "users", ["cognito_issuer", "cognito_sub"])
    op.create_check_constraint(
        "ck_user_cognito_identity_pair",
        "users",
        "(cognito_issuer IS NULL AND cognito_sub IS NULL) OR (cognito_issuer IS NOT NULL AND cognito_sub IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_user_cognito_identity_pair", "users", type_="check")
    op.drop_constraint("uq_user_cognito_identity", "users", type_="unique")
    op.drop_column("users", "cognito_sub")
    op.drop_column("users", "cognito_issuer")
