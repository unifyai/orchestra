"""Refactor user desktop fields: remove is_user_desktop, add user_desktop_* fields

Replace the is_user_desktop boolean with three new fields:
- user_desktop_mode: enum for the user's desktop OS (ubuntu/windows/macos)
- user_desktop_filesys_sync: boolean for filesystem sync (default false)
- user_desktop_url: URL for communication with user's desktop

Revision ID: refactor_user_desktop_fields
Revises: a9d21cb31092
Create Date: 2026-01-27 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "refactor_user_desktop_fields"
down_revision = "7f8e9a1b2c3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new columns
    op.add_column(
        "assistants",
        sa.Column("user_desktop_mode", sa.String(), nullable=True),
    )
    op.add_column(
        "assistants",
        sa.Column(
            "user_desktop_filesys_sync",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "assistants",
        sa.Column("user_desktop_url", sa.String(), nullable=True),
    )

    # Add CHECK constraint for user_desktop_mode
    op.create_check_constraint(
        "ck_assistant_user_desktop_mode",
        "assistants",
        "user_desktop_mode IN ('ubuntu', 'windows', 'macos')",
    )

    # Drop the old is_user_desktop column
    op.drop_column("assistants", "is_user_desktop")


def downgrade() -> None:
    # Add back the old column
    op.add_column(
        "assistants",
        sa.Column("is_user_desktop", sa.Boolean(), nullable=True),
    )

    # Drop new constraint and columns
    op.drop_constraint(
        "ck_assistant_user_desktop_mode",
        "assistants",
        type_="check",
    )
    op.drop_column("assistants", "user_desktop_url")
    op.drop_column("assistants", "user_desktop_filesys_sync")
    op.drop_column("assistants", "user_desktop_mode")
