"""Drop unused temp_interface and dashboard_view tables

Revision ID: drop_unused_interface_tables
Revises: drop_legacy_tables
Create Date: 2026-01-27 12:00:00.000000

These tables are dead code:
- temp_interface: Was intended for autosave/draft functionality but never used.
  The checkpoint functionality uses the regular Interface model with is_checkpoint field.
- dashboard_view: Has no corresponding DAO and is not used anywhere in the codebase.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "drop_unused_interface_tables"
down_revision = "drop_legacy_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop the unused temp_interface and dashboard_view tables."""
    op.drop_table("temp_interface")
    op.drop_table("dashboard_view")


def downgrade() -> None:
    """Recreate the temp_interface and dashboard_view tables."""
    # Recreate dashboard_view
    op.create_table(
        "dashboard_view",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("view", sa.String(), nullable=True),
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
    )
    op.create_index(
        "ix_dashboard_view_project_id",
        "dashboard_view",
        ["project_id"],
    )

    # Recreate temp_interface
    op.create_table(
        "temp_interface",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("new_counter", sa.Integer(), nullable=False),
        sa.Column("items", sa.String(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("context", sa.String(), nullable=True),
        sa.Column("color", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
            nullable=True,
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
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id",
            "project_id",
            "name",
            name="temp_it_uq_project_name",
        ),
    )
    op.create_index(
        "ix_temp_interface_user_id",
        "temp_interface",
        ["user_id"],
    )
    op.create_index(
        "ix_temp_interface_organization_id",
        "temp_interface",
        ["organization_id"],
    )
    op.create_index(
        "ix_temp_interface_project_id",
        "temp_interface",
        ["project_id"],
    )
