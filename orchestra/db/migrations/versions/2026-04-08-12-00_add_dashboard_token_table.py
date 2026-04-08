"""Add dashboard_token table.

Lightweight lookup table that maps opaque tokens to Unify context paths for
dashboard tiles and layouts. Content lives in Unify contexts; this table
provides the routing information the console needs to resolve token-based URLs.

Revision ID: add_dashboard_token
Revises: backfill_credit_ledger
Create Date: 2026-04-03 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_dashboard_token"
down_revision = "backfill_credit_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
        sa.Column(
            "created_at",
            sa.TIMESTAMP,
            server_default=sa.func.now(),
        ),
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


def downgrade() -> None:
    op.drop_index("idx_dashboard_token_user_id", table_name="dashboard_token")
    op.drop_index("idx_dashboard_token_project_id", table_name="dashboard_token")
    op.drop_table("dashboard_token")
