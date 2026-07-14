"""Drop sales demo assistant tables and columns.

On databases that still have ``assistants.demo_id``, fails loud if any
non-null rows remain so those assistants must be deleted first.

On fresh installs where the squashed initial schema never created demo
tables/columns, this is a no-op aside from ``DROP TABLE IF EXISTS``.

Revision ID: drop_demo_assistants
Revises: provider_trigger_worker
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "drop_demo_assistants"
down_revision = "provider_trigger_worker"
branch_labels = None
depends_on = None


def _column_exists(bind, *, table: str, column: str) -> bool:
    return (
        bind.execute(
            sa.text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table
                  AND column_name = :column
                """,
            ),
            {"table": table, "column": column},
        ).scalar()
        is not None
    )


def _drop_fk_on_column(bind, *, table: str, column: str) -> None:
    """Drop every FK on ``table.column`` (legacy or postgres-default name)."""
    rows = bind.execute(
        sa.text(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = 'public'
              AND tc.table_name = :table
              AND kcu.column_name = :column
            """,
        ),
        {"table": table, "column": column},
    ).fetchall()
    for (constraint_name,) in rows:
        op.drop_constraint(constraint_name, table, type_="foreignkey")


def upgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, table="assistants", column="demo_id"):
        remaining = bind.execute(
            sa.text("SELECT COUNT(*) FROM assistants WHERE demo_id IS NOT NULL"),
        ).scalar()
        if remaining:
            raise RuntimeError(
                f"Cannot drop demo assistant schema: {remaining} assistant(s) still "
                "have demo_id set. Delete those assistants (and their "
                "demo_assistant_meta rows), then re-run this migration.",
            )

        _drop_fk_on_column(bind, table="assistants", column="demo_id")
        op.execute("DROP INDEX IF EXISTS idx_assistants_demo_id")
        op.execute("ALTER TABLE assistants DROP COLUMN IF EXISTS demo_id")

    op.execute("DROP TABLE IF EXISTS demo_assistant_meta")


def downgrade() -> None:
    op.create_table(
        "demo_assistant_meta",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_assistant_id", sa.Integer(), nullable=True),
        sa.Column("demoer_user_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
        ),
        sa.Column("prospect_first_name", sa.String(), nullable=True),
        sa.Column("prospect_surname", sa.String(), nullable=True),
        sa.Column("prospect_email", sa.String(), nullable=True),
        sa.Column("prospect_phone", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["demoer_user_id"],
            ["user.id"],
            name="assistants_demo_id_demoer_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_assistant_id"],
            ["assistants.agent_id"],
            name="demo_assistant_meta_source_assistant_id_fkey",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "idx_demo_meta_demoer",
        "demo_assistant_meta",
        ["demoer_user_id"],
    )
    op.add_column(
        "assistants",
        sa.Column("demo_id", sa.Integer(), nullable=True),
    )
    op.create_index("idx_assistants_demo_id", "assistants", ["demo_id"])
    op.create_foreign_key(
        "assistants_demo_id_fkey",
        "assistants",
        "demo_assistant_meta",
        ["demo_id"],
        ["id"],
        ondelete="SET NULL",
    )
