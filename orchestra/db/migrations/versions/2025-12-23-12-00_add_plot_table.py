"""Add plot table for shareable plot configurations

This migration creates the plot table for storing shareable plot configurations.
Plots are linked to projects with cascade delete, and access is based on project
permissions.

Revision ID: add_plot_table
Revises: add_embedding_queue
Create Date: 2025-12-23 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "add_plot_table"
down_revision = "add_assistant_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create plot table for shareable plot configurations.
    """
    op.create_table(
        "plot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(12), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("plot_config", JSONB(), nullable=False),
        sa.Column("project_config", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth_user.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token", name="uq_plot_token"),
    )

    # Indexes for efficient lookups
    op.create_index("idx_plot_token", "plot", ["token"], unique=True)
    op.create_index("idx_plot_project_id", "plot", ["project_id"])
    op.create_index("idx_plot_user_id", "plot", ["user_id"])
    op.create_index("idx_plot_organization_id", "plot", ["organization_id"])


def downgrade() -> None:
    """
    Drop plot table.
    """
    op.drop_index("idx_plot_organization_id", table_name="plot")
    op.drop_index("idx_plot_user_id", table_name="plot")
    op.drop_index("idx_plot_project_id", table_name="plot")
    op.drop_index("idx_plot_token", table_name="plot")
    op.drop_table("plot")
