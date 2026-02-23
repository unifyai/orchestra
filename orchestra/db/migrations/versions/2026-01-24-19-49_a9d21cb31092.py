"""drop orphaned dashboard_view and temp_interface tables

These tables were left behind when the API endpoints were removed.
The models and DAOs have been deleted, so the tables are now orphaned.

Revision ID: a9d21cb31092
Revises: 8a3f5c2d1e9b
Create Date: 2026-01-24 19:49:57.265721

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a9d21cb31092"
down_revision = "8a3f5c2d1e9b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop temp_interface table and its indexes
    op.drop_index("ix_temp_interface_organization_id", table_name="temp_interface")
    op.drop_index("ix_temp_interface_project_id", table_name="temp_interface")
    op.drop_index("ix_temp_interface_user_id", table_name="temp_interface")
    op.drop_table("temp_interface")

    # Drop dashboard_view table and its index
    op.drop_index("ix_dashboard_view_project_id", table_name="dashboard_view")
    op.drop_table("dashboard_view")


def downgrade() -> None:
    # Recreate dashboard_view table
    op.create_table(
        "dashboard_view",
        sa.Column("id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("name", sa.VARCHAR(), autoincrement=False, nullable=True),
        sa.Column("view", sa.VARCHAR(), autoincrement=False, nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name="dashboard_view_project_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="dashboard_view_pkey"),
    )
    op.create_index(
        "ix_dashboard_view_project_id",
        "dashboard_view",
        ["project_id"],
        unique=False,
    )

    # Recreate temp_interface table
    op.create_table(
        "temp_interface",
        sa.Column("id", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.VARCHAR(), autoincrement=False, nullable=True),
        sa.Column("organization_id", sa.INTEGER(), autoincrement=False, nullable=True),
        sa.Column("new_counter", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("items", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column("name", sa.VARCHAR(), autoincrement=False, nullable=False),
        sa.Column("project_id", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("context", sa.VARCHAR(), autoincrement=False, nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column("color", sa.VARCHAR(), autoincrement=False, nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name="temp_interface_organization_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name="temp_interface_project_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["auth_user.id"],
            name="temp_interface_user_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="temp_interface_pkey"),
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
        unique=False,
    )
    op.create_index(
        "ix_temp_interface_project_id",
        "temp_interface",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_temp_interface_organization_id",
        "temp_interface",
        ["organization_id"],
        unique=False,
    )
