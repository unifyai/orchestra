"""Drop ``dashboard_token``: dashboards are retired in favour of canvas.

The table held only routing rows — token → (context, creator identity) —
for the dashboard tile/layout viewer. The content those tokens pointed at
lived in ``Dashboards/*`` Unify contexts and was archived before removal;
the canvas equivalent (``canvas_token``) carries its own visibility and
status columns and is untouched.

Dropped with ``IF EXISTS`` so the migration is a no-op on a fresh
database whose baseline never created the table, and still generates
clean SQL in alembic's offline mode.

Revision ID: drop_dashboard_token
Revises: drop_legacy_tasks_instance_id
Create Date: 2026-08-21 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "drop_dashboard_token"
down_revision = "drop_legacy_tasks_instance_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_dashboard_token_project_id")
    op.execute("DROP INDEX IF EXISTS idx_dashboard_token_user_id")
    op.execute("DROP TABLE IF EXISTS dashboard_token")


def downgrade() -> None:
    # Recreates the table empty, as it stood: routing rows only. The content
    # rows the tokens pointed at are in the dashboards retirement archive.
    op.create_table(
        "dashboard_token",
        sa.Column("token", sa.String(12), primary_key=True),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("context_name", sa.String(500), nullable=False),
        sa.Column(
            "project_id",
            sa.Integer,
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String,
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_dashboard_token_project_id",
        "dashboard_token",
        ["project_id"],
    )
    op.create_index(
        "idx_dashboard_token_user_id",
        "dashboard_token",
        ["user_id"],
    )
