"""Add user_desktops table and refactor assistant desktop fields

Create user_desktops table for device registration. On assistants, replace
user_desktop_url and user_desktop_mode with a user_desktop_id FK to the
new table.

Revision ID: add_user_desktops
Revises: a1b2c3d4e5f6
Create Date: 2026-02-23 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_user_desktops"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create user_desktops table
    op.create_table(
        "user_desktops",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("os", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "os IN ('ubuntu', 'windows', 'macos')",
            name="ck_user_desktop_os",
        ),
    )

    # Add user_desktop_id FK to assistants
    op.add_column(
        "assistants",
        sa.Column(
            "user_desktop_id",
            sa.Integer(),
            sa.ForeignKey("user_desktops.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_assistant_user_desktop_id",
        "assistants",
        ["user_desktop_id"],
    )

    # Drop old columns
    op.drop_constraint(
        "ck_assistant_user_desktop_mode",
        "assistants",
        type_="check",
    )
    op.drop_column("assistants", "user_desktop_url")
    op.drop_column("assistants", "user_desktop_mode")


def downgrade() -> None:
    # Re-add old columns
    op.add_column(
        "assistants",
        sa.Column("user_desktop_mode", sa.String(), nullable=True),
    )
    op.add_column(
        "assistants",
        sa.Column("user_desktop_url", sa.String(), nullable=True),
    )
    op.create_check_constraint(
        "ck_assistant_user_desktop_mode",
        "assistants",
        "user_desktop_mode IN ('ubuntu', 'windows', 'macos')",
    )

    # Drop user_desktop_id
    op.drop_constraint("uq_assistant_user_desktop_id", "assistants", type_="unique")
    op.drop_column("assistants", "user_desktop_id")

    # Drop user_desktops table
    op.drop_table("user_desktops")
