"""Denormalize project_id onto log_unique_constraint.

Revision ID: log_unique_constraint_project_id
Revises: team_profile_image

Closes the uniqueness seam left by kernel LIST(project_id) partitioning:
log_unique_constraint lost its FK to log_event and stayed unscoped, so
create/delete contended on a global hot table. Adding project_id (backfilled
from context) lets delete/cleanup prune by project + context, matching the
kernel write paths. Full LIST partitioning of this table can follow as a
separate increment once the column is live everywhere.

Idempotent across both starting states:

* Fresh DB -- ``0001_core_initial``'s ``meta.create_all`` already built
  ``project_id`` and the project/context indexes from the current model, so
  the ``IF NOT EXISTS`` DDL is a no-op and the (empty) table needs no backfill.
* Existing DB -- adds the column/indexes and backfills ``project_id`` from
  ``context`` for every existing uniqueness row.
"""

from __future__ import annotations

from alembic import op

revision = "log_unique_constraint_project_id"
down_revision = "team_profile_image"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE log_unique_constraint "
        "ADD COLUMN IF NOT EXISTS project_id integer",
    )
    op.execute(
        """
        UPDATE log_unique_constraint AS luc
        SET project_id = c.project_id
        FROM context AS c
        WHERE c.id = luc.context_id
          AND luc.project_id IS NULL
        """,
    )
    # Orphans (context already gone) cannot be placed; drop them rather than
    # leave NOT NULL violations — they are already unreachable uniqueness rows.
    op.execute("DELETE FROM log_unique_constraint WHERE project_id IS NULL")
    op.execute(
        "ALTER TABLE log_unique_constraint ALTER COLUMN project_id SET NOT NULL",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_log_unique_constraint_context_log_event "
        "ON log_unique_constraint (context_id, log_event_id)",
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_log_unique_constraint_project_log_event "
        "ON log_unique_constraint (project_id, log_event_id)",
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_log_unique_constraint_project_log_event")
    op.execute("DROP INDEX IF EXISTS idx_log_unique_constraint_context_log_event")
    op.execute(
        "ALTER TABLE log_unique_constraint DROP COLUMN IF EXISTS project_id",
    )
