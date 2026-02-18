"""Refactor user_local_desktop to desktop_mode and is_user_desktop

Revision ID: refactor_user_local_desktop
Revises: remove_param_field_category
Create Date: 2026-01-14 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "refactor_user_local_desktop"
down_revision = "remove_param_field_category"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new columns
    op.add_column(
        "assistants",
        sa.Column("desktop_mode", sa.String(), nullable=True),
    )
    op.add_column(
        "assistants",
        sa.Column("is_user_desktop", sa.Boolean(), nullable=True),
    )

    # Add CHECK constraint for desktop_mode
    op.create_check_constraint(
        "ck_assistant_desktop_mode",
        "assistants",
        "desktop_mode IN ('ubuntu', 'windows', 'macos')",
    )

    # Migrate data from old column to new column
    op.execute("UPDATE assistants SET desktop_mode = user_local_desktop")

    # Drop old constraint and column
    op.drop_constraint("ck_assistant_user_local_desktop", "assistants", type_="check")
    op.drop_column("assistants", "user_local_desktop")


def downgrade() -> None:
    # Add back the old column
    op.add_column(
        "assistants",
        sa.Column("user_local_desktop", sa.String(), nullable=True),
    )

    # Add back the old CHECK constraint
    op.create_check_constraint(
        "ck_assistant_user_local_desktop",
        "assistants",
        "user_local_desktop IN ('ubuntu', 'windows', 'macos')",
    )

    # Migrate data back
    op.execute("UPDATE assistants SET user_local_desktop = desktop_mode")

    # Drop new constraint and columns
    op.drop_constraint("ck_assistant_desktop_mode", "assistants", type_="check")
    op.drop_column("assistants", "is_user_desktop")
    op.drop_column("assistants", "desktop_mode")
