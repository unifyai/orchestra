"""Denormalize project_id onto log_unique_constraint.

Revision ID: log_unique_constraint_project_id
Revises: team_profile_image

Closes the uniqueness seam left by kernel LIST(project_id) partitioning:
log_unique_constraint lost its FK to log_event and stayed unscoped, so
create/delete contended on a global hot table. Adding project_id (backfilled
from context) lets delete/cleanup prune by project + context, matching the
kernel write paths. Full LIST partitioning of this table can follow as a
separate increment once the column is live everywhere.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "log_unique_constraint_project_id"
down_revision = "team_profile_image"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "log_unique_constraint",
        sa.Column("project_id", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE log_unique_constraint AS luc
        SET project_id = c.project_id
        FROM context AS c
        WHERE c.id = luc.context_id
        """,
    )
    # Orphans (context already gone) cannot be placed; drop them rather than
    # leave NOT NULL violations — they are already unreachable uniqueness rows.
    op.execute("DELETE FROM log_unique_constraint WHERE project_id IS NULL")
    op.alter_column(
        "log_unique_constraint",
        "project_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_index(
        "idx_log_unique_constraint_context_log_event",
        "log_unique_constraint",
        ["context_id", "log_event_id"],
    )
    op.create_index(
        "idx_log_unique_constraint_project_log_event",
        "log_unique_constraint",
        ["project_id", "log_event_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_log_unique_constraint_project_log_event",
        table_name="log_unique_constraint",
    )
    op.drop_index(
        "idx_log_unique_constraint_context_log_event",
        table_name="log_unique_constraint",
    )
    op.drop_column("log_unique_constraint", "project_id")
