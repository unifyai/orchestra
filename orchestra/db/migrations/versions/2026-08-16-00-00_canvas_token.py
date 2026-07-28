"""Canvas token routing table.

Adds ``canvas_token``: the mapping console uses to turn a twelve-character token
in a URL into a Unify context path plus the identity to read it as. The canvas
content itself lives in Unify contexts under ``Canvas/*``; only routing and the
two authorization columns are relational.

``visibility`` and ``status`` are constrained in the database as well as in the
request schema. They are the authorization decision every read path makes, so a
row that arrived by some other route must still be safe to serve.

Constraint / index names match what ``meta.create_all`` produces from the ORM
model so fresh test DBs and migrated production DBs agree.

Revision ID: canvas_token
Revises: founder_interview_ask
Create Date: 2026-08-16 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "canvas_token"
down_revision = "founder_interview_ask"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canvas_token",
        sa.Column("token", sa.String(length=12), primary_key=True),
        sa.Column("context_name", sa.String(length=500), nullable=False),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "visibility",
            sa.String(length=20),
            nullable=False,
            server_default="private",
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now()),
        sa.CheckConstraint(
            "visibility IN ('private', 'team', 'public_link')",
            name="ck_canvas_token_visibility",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'quarantined')",
            name="ck_canvas_token_status",
        ),
    )
    op.create_index("idx_canvas_token_project_id", "canvas_token", ["project_id"])
    op.create_index("idx_canvas_token_user_id", "canvas_token", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_canvas_token_user_id", table_name="canvas_token")
    op.drop_index("idx_canvas_token_project_id", table_name="canvas_token")
    op.drop_table("canvas_token")
