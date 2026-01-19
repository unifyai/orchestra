"""add_table_view_and_plot_improvements

Revision ID: 8b471349c2b6
Revises: drop_eav_tables
Create Date: 2026-01-19 15:38:05.650153

This migration:
1. Creates the table_view table for shareable table configurations
2. Adds updated_at column to plot table
3. Adds JSONB indexes on project_config->>'context' for both tables
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "8b471349c2b6"
down_revision = "drop_eav_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ==========================================================================
    # 1. Create table_view table
    # ==========================================================================
    op.create_table(
        "table_view",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=12), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column(
            "table_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "project_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["auth_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # table_view indexes
    op.create_index("ix_table_view_token", "table_view", ["token"], unique=True)
    op.create_index(
        "idx_table_view_project_id", "table_view", ["project_id"], unique=False
    )
    op.create_index("idx_table_view_user_id", "table_view", ["user_id"], unique=False)
    op.create_index(
        "idx_table_view_organization_id",
        "table_view",
        ["organization_id"],
        unique=False,
    )

    # JSONB index for context filtering
    op.execute(
        "CREATE INDEX idx_table_view_project_config_context "
        "ON table_view ((project_config->>'context'))"
    )

    # ==========================================================================
    # 2. Add updated_at column to plot table
    # ==========================================================================
    op.add_column(
        "plot",
        sa.Column(
            "updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True
        ),
    )

    # JSONB index for context filtering on plot
    op.execute(
        "CREATE INDEX idx_plot_project_config_context "
        "ON plot ((project_config->>'context'))"
    )


def downgrade() -> None:
    # Remove JSONB indexes
    op.execute("DROP INDEX IF EXISTS idx_plot_project_config_context")
    op.execute("DROP INDEX IF EXISTS idx_table_view_project_config_context")

    # Remove updated_at from plot
    op.drop_column("plot", "updated_at")

    # Drop table_view indexes and table
    op.drop_index("idx_table_view_organization_id", table_name="table_view")
    op.drop_index("idx_table_view_user_id", table_name="table_view")
    op.drop_index("idx_table_view_project_id", table_name="table_view")
    op.drop_index("ix_table_view_token", table_name="table_view")
    op.drop_table("table_view")
