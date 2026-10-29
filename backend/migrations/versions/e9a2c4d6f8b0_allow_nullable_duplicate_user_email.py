"""allow Cognito identities without unique email

Revision ID: e9a2c4d6f8b0
Revises: c7e8f9a0b1c2
"""

import sqlalchemy as sa
from alembic import op

revision = "e9a2c4d6f8b0"
down_revision = "c7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Identity remains (cognito_issuer, cognito_sub); email is profile data only.
    op.drop_constraint("users_email_key", "users", type_="unique")
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text('SELECT 1 FROM "users" WHERE "email" IS NULL LIMIT 1')).first():
        raise RuntimeError("Cannot downgrade: users contains null email values")
    if bind.execute(
        sa.text('SELECT 1 FROM "users" GROUP BY "email" HAVING count(*) > 1 LIMIT 1')
    ).first():
        raise RuntimeError("Cannot downgrade: users contains duplicate email values")
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=False)
    op.create_unique_constraint("users_email_key", "users", ["email"])
