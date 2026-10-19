"""Allow several platform publication intents for one PUBLICATION node."""

from collections.abc import Sequence

from alembic import op

revision: str = "d4f6a910"
down_revision: str | None = "c3f4a9b7e2d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Test bootstrap may create the V3 metadata without Alembic history; a
    # production c3f4a9b7e2d1 database has this named constraint.
    op.execute(
        "ALTER TABLE social_publications "
        "DROP CONSTRAINT IF EXISTS uq_social_publication_node_execution"
    )
    op.execute(
        "ALTER TABLE social_publications "
        "DROP CONSTRAINT IF EXISTS social_publications_node_execution_id_key"
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_social_publication_node_execution",
        "social_publications",
        ["node_execution_id"],
    )
