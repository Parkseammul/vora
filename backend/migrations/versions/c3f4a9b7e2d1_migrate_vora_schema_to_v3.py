"""Migrate the V1 schema to the V3 model contract.

This migration deliberately stops when historical rows cannot be converted
without inventing V3 data.  The application must provide an explicit
backfill for those rows before the migration can be applied to such a DB.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3f4a9b7e2d1"
down_revision: str | None = "b4e20f7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _drop_constraint_if_exists(table: str, constraint: str) -> None:
    op.execute(
        sa.text(
            f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{constraint}"'
        )
    )


def _drop_index_if_exists(index: str) -> None:
    op.execute(sa.text(f'DROP INDEX IF EXISTS "{index}"'))


def _assert_no_rows(table: str, message: str) -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(f'SELECT 1 FROM "{table}" LIMIT 1')).first() is not None:
        raise RuntimeError(message)


def _assert_no_nonnull(table: str, column: str, message: str) -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(f'SELECT 1 FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT 1')).first() is not None:
        raise RuntimeError(message)


def _assert_no_null(table: str, column: str, message: str) -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(f'SELECT 1 FROM "{table}" WHERE "{column}" IS NULL LIMIT 1')).first() is not None:
        raise RuntimeError(message)


def _assert_query_empty(query: str, message: str) -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(query)).first() is not None:
        raise RuntimeError(message)


def _replace_enum(
    type_name: str,
    table: str,
    column: str,
    values: Sequence[str],
    mapping: dict[str, str],
    default: str | None,
) -> None:
    """Replace a PostgreSQL enum so removed values are handled safely."""

    temporary_name = f"{type_name}_v3"
    op.execute(sa.text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT'))
    op.execute(sa.text(f'DROP TYPE IF EXISTS "{temporary_name}"'))
    enum_values = ", ".join(_sql_literal(value) for value in values)
    op.execute(sa.text(f'CREATE TYPE "{temporary_name}" AS ENUM ({enum_values})'))

    cases = " ".join(
        f"WHEN {_sql_literal(old)} THEN {_sql_literal(new)}"
        for old, new in mapping.items()
    )
    expression = f'CASE "{column}"::text {cases} ELSE "{column}"::text END'
    op.execute(
        sa.text(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE "{temporary_name}" '
            f'USING ({expression})::"{temporary_name}"'
        )
    )
    op.execute(sa.text(f'DROP TYPE "{type_name}"'))
    op.execute(sa.text(f'ALTER TYPE "{temporary_name}" RENAME TO "{type_name}"'))
    if default is not None:
        op.execute(
            sa.text(
                f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
                f'SET DEFAULT {_sql_literal(default)}::"{type_name}"'
            )
        )


def upgrade() -> None:
    # Independent V3 registries must exist before their new foreign keys.
    op.create_table(
        "rules_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, unique=True),
        sa.Column("rules_data", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("node_type", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("node_type", "version", name="uq_prompt_version_node_type_version"),
    )

    # These rows need business-level backfills that the V1 schema does not
    # contain. Stop before removing any of their old source columns.
    _assert_no_rows(
        "execution_input_snapshots",
        "Cannot migrate execution snapshots: V3 marketing_info, rules_version_id, and source_asset_id require explicit data",
    )
    _assert_no_rows(
        "social_publications",
        "Cannot migrate social publications: node_execution_id requires an actual PUBLICATION node backfill",
    )
    _assert_no_nonnull(
        "user_approvals",
        "revision_request",
        "Cannot migrate user approvals: revision_request data needs an explicit Revision backfill",
    )

    _replace_enum(
        "workflow_execution_status",
        "workflow_executions",
        "status",
        ("PENDING", "RUNNING", "WAITING_APPROVAL", "REVISING", "SUCCEEDED", "FAILED", "CANCELED"),
        {"SUCCESS": "SUCCEEDED"},
        "PENDING",
    )
    op.alter_column("workflow_executions", "finished_at", new_column_name="completed_at")
    op.add_column("workflow_executions", sa.Column("current_node_type", sa.String(30), nullable=True))

    _replace_enum(
        "node_execution_status",
        "node_executions",
        "status",
        ("PENDING", "RUNNING", "WAITING", "SUCCEEDED", "FAILED", "SKIPPED"),
        {"WAITING_APPROVAL": "WAITING", "SUCCESS": "SUCCEEDED", "RETRYING": "RUNNING"},
        "PENDING",
    )
    _drop_constraint_if_exists("node_executions", "uq_node_execution_version")
    op.alter_column("node_executions", "node_key", new_column_name="node_type")
    op.alter_column("node_executions", "user_requested_version", new_column_name="revision_no")
    op.alter_column("node_executions", "finished_at", new_column_name="completed_at")
    bind = op.get_bind()
    invalid_revision = bind.execute(
        sa.text('SELECT 1 FROM "node_executions" WHERE "revision_no" < 1 LIMIT 1')
    ).first()
    if invalid_revision is not None:
        raise RuntimeError("Cannot migrate node executions: revision values must all be >= 1")
    op.execute(sa.text('UPDATE "node_executions" SET "revision_no" = "revision_no" - 1'))
    op.alter_column(
        "node_executions",
        "revision_no",
        existing_type=sa.Integer(),
        server_default="0",
    )
    op.create_unique_constraint(
        "uq_node_execution_revision",
        "node_executions",
        ["workflow_execution_id", "node_type", "revision_no"],
    )

    _replace_enum(
        "node_execution_attempt_status",
        "node_execution_attempts",
        "status",
        ("RUNNING", "SUCCEEDED", "FAILED"),
        {"SUCCESS": "SUCCEEDED"},
        None,
    )
    op.alter_column("node_execution_attempts", "llm_provider", new_column_name="ai_company")
    op.alter_column("node_execution_attempts", "llm_model", new_column_name="model")
    op.alter_column("node_execution_attempts", "finished_at", new_column_name="completed_at")
    op.add_column("node_execution_attempts", sa.Column("prompt_version_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_node_execution_attempt_prompt_version",
        "node_execution_attempts",
        "prompt_versions",
        ["prompt_version_id"],
        ["id"],
    )

    _drop_constraint_if_exists("file_assets", "ck_file_asset_exactly_one_source")
    _drop_constraint_if_exists("file_assets", "ck_input_image_sort_order_required")
    _drop_constraint_if_exists("file_assets", "ck_file_asset_sort_order_positive")
    _drop_index_if_exists("uq_file_asset_snapshot_sort_order")
    _drop_constraint_if_exists("file_assets", "file_assets_execution_input_snapshot_id_fkey")
    op.alter_column("file_assets", "node_execution_id", new_column_name="created_by_node_execution_id")
    op.alter_column("file_assets", "sort_order", new_column_name="slide_no")
    op.create_check_constraint(
        "ck_file_asset_slide_no_positive",
        "file_assets",
        "slide_no IS NULL OR slide_no >= 1",
    )
    op.drop_column("file_assets", "execution_input_snapshot_id")

    _drop_constraint_if_exists("user_approvals", "ck_revision_request_required")
    _replace_enum(
        "approval_decision",
        "user_approvals",
        "decision",
        ("APPROVED", "REJECTED"),
        {"REVISION_REQUESTED": "REJECTED"},
        None,
    )
    op.add_column("user_approvals", sa.Column("workflow_execution_id", sa.BigInteger(), nullable=True))
    op.execute(
        sa.text(
            'UPDATE "user_approvals" AS ua SET "workflow_execution_id" = ne."workflow_execution_id" '
            'FROM "node_executions" AS ne WHERE ua."node_execution_id" = ne."id"'
        )
    )
    _assert_no_null(
        "user_approvals",
        "workflow_execution_id",
        "Cannot migrate user approvals: workflow_execution_id backfill is incomplete",
    )
    op.alter_column("user_approvals", "workflow_execution_id", nullable=False)
    op.create_foreign_key(
        "fk_user_approval_workflow_execution",
        "user_approvals",
        "workflow_executions",
        ["workflow_execution_id"],
        ["id"],
    )
    _assert_no_nonnull(
        "user_approvals",
        "revision_request",
        "Cannot migrate user approvals: revision_request data needs an explicit Revision backfill",
    )
    _drop_constraint_if_exists("user_approvals", "user_approvals_user_id_fkey")
    op.drop_column("user_approvals", "user_id")
    op.drop_column("user_approvals", "revision_request")
    op.alter_column("user_approvals", "created_at", new_column_name="decided_at")

    # Snapshot business inputs and source assets cannot be inferred safely from V1 rows.
    op.add_column("execution_input_snapshots", sa.Column("marketing_info", sa.Text(), nullable=True))
    op.add_column("execution_input_snapshots", sa.Column("theme", sa.Text(), nullable=True))
    op.add_column("execution_input_snapshots", sa.Column("source_asset_id", sa.BigInteger(), nullable=True))
    op.add_column("execution_input_snapshots", sa.Column("rules_version_id", sa.BigInteger(), nullable=True))
    _assert_no_rows(
        "execution_input_snapshots",
        "Cannot migrate execution snapshots: V3 marketing_info, rules_version_id, and source_asset_id require explicit data",
    )
    op.alter_column("execution_input_snapshots", "marketing_info", nullable=False)
    op.alter_column("execution_input_snapshots", "source_asset_id", nullable=False)
    op.alter_column("execution_input_snapshots", "rules_version_id", nullable=False)
    op.create_foreign_key(
        "fk_snapshot_source_asset",
        "execution_input_snapshots",
        "file_assets",
        ["source_asset_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_snapshot_rules_version",
        "execution_input_snapshots",
        "rules_versions",
        ["rules_version_id"],
        ["id"],
    )
    op.drop_column("execution_input_snapshots", "input_type")
    op.drop_column("execution_input_snapshots", "input_data")
    op.execute(sa.text('DROP TYPE IF EXISTS "input_type"'))

    op.create_table(
        "revisions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workflow_execution_id", sa.BigInteger(), nullable=False),
        sa.Column("user_approval_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("start_node_type", sa.String(100), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["workflow_execution_id"], ["workflow_executions.id"]),
        sa.ForeignKeyConstraint(["user_approval_id"], ["user_approvals.id"]),
        sa.UniqueConstraint("workflow_execution_id", "revision_no", name="uq_revision_workflow_revision"),
    )

    _replace_enum(
        "social_publication_status",
        "social_publications",
        "status",
        ("PENDING", "PUBLISHING", "PUBLISHED", "FAILED", "DELETED"),
        {"SUCCESS": "PUBLISHED"},
        "PENDING",
    )
    op.add_column("social_publications", sa.Column("node_execution_id", sa.BigInteger(), nullable=True))
    op.add_column("social_publications", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    _assert_no_rows(
        "social_publications",
        "Cannot migrate social publications: node_execution_id requires an actual PUBLICATION node backfill",
    )
    op.alter_column("social_publications", "node_execution_id", nullable=False)
    op.create_foreign_key(
        "fk_social_publication_node_execution",
        "social_publications",
        "node_executions",
        ["node_execution_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_social_publication_node_execution",
        "social_publications",
        ["node_execution_id"],
    )
    _drop_constraint_if_exists("social_publications", "social_publications_file_asset_id_fkey")
    op.drop_column("social_publications", "file_asset_id")
    op.drop_column("social_publications", "error_message")
    op.drop_column("social_publications", "youtube_title")
    op.drop_column("social_publications", "youtube_description")
    op.drop_column("social_publications", "instagram_caption")


def downgrade() -> None:
    # A lossless downgrade is only possible for an empty V3 data set in the
    # domains whose V1 values were removed.  Never synthesize those values.
    for table in ("social_publications", "revisions", "execution_input_snapshots", "user_approvals"):
        _assert_no_rows(table, f"Cannot downgrade V3 schema: {table} contains data")
    _assert_query_empty(
        'SELECT 1 FROM "workflow_executions" '
        'WHERE "status"::text IN (\'REVISING\', \'CANCELED\') LIMIT 1',
        "Cannot downgrade: workflow_executions contains V3-only statuses REVISING or CANCELED.",
    )
    _assert_query_empty(
        'SELECT 1 FROM "node_executions" WHERE "status"::text = \'SKIPPED\' LIMIT 1',
        "Cannot downgrade: node_executions contains V3-only status SKIPPED.",
    )
    _assert_query_empty(
        'SELECT 1 FROM "file_assets" '
        'WHERE "created_by_node_execution_id" IS NULL LIMIT 1',
        "Cannot downgrade: file_assets contains V3 data that cannot satisfy V1 source/sort constraints.",
    )

    op.drop_table("revisions")

    op.drop_constraint("uq_social_publication_node_execution", "social_publications", type_="unique")
    op.drop_constraint("fk_social_publication_node_execution", "social_publications", type_="foreignkey")
    op.add_column("social_publications", sa.Column("file_asset_id", sa.BigInteger(), nullable=True))
    op.alter_column("social_publications", "file_asset_id", nullable=False)
    op.create_foreign_key(
        "fk_social_publication_file_asset",
        "social_publications",
        "file_assets",
        ["file_asset_id"],
        ["id"],
    )
    op.add_column("social_publications", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("social_publications", sa.Column("youtube_title", sa.String(100), nullable=True))
    op.add_column("social_publications", sa.Column("youtube_description", sa.Text(), nullable=True))
    op.add_column("social_publications", sa.Column("instagram_caption", sa.Text(), nullable=True))
    op.drop_column("social_publications", "deleted_at")
    op.drop_column("social_publications", "node_execution_id")
    _replace_enum(
        "social_publication_status",
        "social_publications",
        "status",
        ("PENDING", "PUBLISHING", "SUCCESS", "FAILED"),
        {"PUBLISHED": "SUCCESS"},
        "PENDING",
    )

    op.drop_constraint("fk_snapshot_rules_version", "execution_input_snapshots", type_="foreignkey")
    op.drop_constraint("fk_snapshot_source_asset", "execution_input_snapshots", type_="foreignkey")
    op.drop_column("execution_input_snapshots", "rules_version_id")
    op.drop_column("execution_input_snapshots", "source_asset_id")
    op.drop_column("execution_input_snapshots", "theme")
    op.drop_column("execution_input_snapshots", "marketing_info")
    input_type_enum = sa.Enum("TEXT", "IMAGE", "TEXT_IMAGE", name="input_type")
    input_type_enum.create(op.get_bind(), checkfirst=True)
    op.add_column("execution_input_snapshots", sa.Column("input_type", input_type_enum, nullable=False))
    op.add_column("execution_input_snapshots", sa.Column("input_data", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False))

    op.alter_column("user_approvals", "decided_at", new_column_name="created_at")
    op.drop_constraint("fk_user_approval_workflow_execution", "user_approvals", type_="foreignkey")
    op.drop_column("user_approvals", "workflow_execution_id")
    op.add_column("user_approvals", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.add_column("user_approvals", sa.Column("revision_request", sa.Text(), nullable=True))
    op.alter_column("user_approvals", "user_id", nullable=False)
    op.create_foreign_key(
        "fk_user_approval_user",
        "user_approvals",
        "users",
        ["user_id"],
        ["id"],
    )
    _replace_enum(
        "approval_decision",
        "user_approvals",
        "decision",
        ("APPROVED", "REVISION_REQUESTED"),
        {"REJECTED": "REVISION_REQUESTED"},
        None,
    )
    op.create_check_constraint(
        "ck_revision_request_required",
        "user_approvals",
        "decision <> 'REVISION_REQUESTED' OR NULLIF(btrim(revision_request), '') IS NOT NULL",
    )

    op.drop_constraint("ck_file_asset_slide_no_positive", "file_assets", type_="check")
    op.add_column("file_assets", sa.Column("execution_input_snapshot_id", sa.BigInteger(), nullable=True))
    op.alter_column("file_assets", "slide_no", new_column_name="sort_order")
    op.alter_column("file_assets", "created_by_node_execution_id", new_column_name="node_execution_id")
    op.create_foreign_key(
        "fk_file_asset_execution_input_snapshot",
        "file_assets",
        "execution_input_snapshots",
        ["execution_input_snapshot_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_file_asset_exactly_one_source",
        "file_assets",
        "(execution_input_snapshot_id IS NOT NULL) <> (node_execution_id IS NOT NULL)",
    )
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
        postgresql_where=sa.text(
            "execution_input_snapshot_id IS NOT NULL AND sort_order IS NOT NULL"
        ),
    )

    op.drop_constraint("fk_node_execution_attempt_prompt_version", "node_execution_attempts", type_="foreignkey")
    op.drop_column("node_execution_attempts", "prompt_version_id")
    op.alter_column("node_execution_attempts", "completed_at", new_column_name="finished_at")
    op.alter_column("node_execution_attempts", "model", new_column_name="llm_model")
    op.alter_column("node_execution_attempts", "ai_company", new_column_name="llm_provider")
    _replace_enum(
        "node_execution_attempt_status",
        "node_execution_attempts",
        "status",
        ("RUNNING", "SUCCESS", "FAILED"),
        {"SUCCEEDED": "SUCCESS"},
        None,
    )

    op.drop_constraint("uq_node_execution_revision", "node_executions", type_="unique")
    op.alter_column("node_executions", "completed_at", new_column_name="finished_at")
    op.alter_column("node_executions", "revision_no", new_column_name="user_requested_version")
    op.execute(sa.text('UPDATE "node_executions" SET "user_requested_version" = "user_requested_version" + 1'))
    op.alter_column("node_executions", "user_requested_version", server_default="1")
    op.alter_column("node_executions", "node_type", new_column_name="node_key")
    op.create_unique_constraint(
        "uq_node_execution_version",
        "node_executions",
        ["workflow_execution_id", "node_key", "user_requested_version"],
    )
    _replace_enum(
        "node_execution_status",
        "node_executions",
        "status",
        ("PENDING", "RUNNING", "WAITING_APPROVAL", "SUCCESS", "FAILED", "RETRYING"),
        {"WAITING": "WAITING_APPROVAL", "SUCCEEDED": "SUCCESS"},
        "PENDING",
    )

    op.drop_column("workflow_executions", "current_node_type")
    op.alter_column("workflow_executions", "completed_at", new_column_name="finished_at")
    _replace_enum(
        "workflow_execution_status",
        "workflow_executions",
        "status",
        ("PENDING", "RUNNING", "WAITING_APPROVAL", "SUCCESS", "FAILED"),
        {"SUCCEEDED": "SUCCESS"},
        "PENDING",
    )
    op.drop_table("prompt_versions")
    op.drop_table("rules_versions")
