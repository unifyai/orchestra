"""Add public-read flag to projects.

A project marked ``is_public_read`` is readable (data-plane reads only:
logs, fields, metrics, contexts) by any authenticated account. Writes
remain restricted to the owning account. This backs platform-wide
read-only datasets such as the builtin function primitives catalogue,
which is stored once in an admin-owned public project instead of being
duplicated per assistant.

``project`` is a kernel table: fresh databases materialize it from the
live model via ``0001_core_initial`` (already including this column), so
every operation here is idempotent and only converges pre-existing
databases.

Revision ID: project_public_read
Revises: integration_tool_exec_approvals
Create Date: 2026-06-19 02:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "project_public_read"
down_revision = "integration_tool_exec_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.project
            ADD COLUMN IF NOT EXISTS is_public_read BOOLEAN
            DEFAULT false NOT NULL;
        """,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_project_is_public_read
            ON public.project (is_public_read)
            WHERE is_public_read;
        """,
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_project_is_public_read;")
    op.execute(
        "ALTER TABLE public.project DROP COLUMN IF EXISTS is_public_read;",
    )
