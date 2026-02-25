"""Drop voice_mode column from assistants table

The voice_mode field (tts/sts) is no longer used. All voice interactions
now use tts mode exclusively, so the column and its check constraint
are removed.

Revision ID: drop_voice_mode
Revises: add_is_local
Create Date: 2026-02-25 14:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "drop_voice_mode"
down_revision = "add_is_local"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the check constraint first, then the column
    op.drop_constraint("ck_assistant_voice_mode", "assistants", type_="check")
    op.drop_column("assistants", "voice_mode")


def downgrade() -> None:
    # Re-add the column
    op.add_column(
        "assistants",
        sa.Column("voice_mode", sa.String(), nullable=True),
    )
    # Set default value for existing rows
    op.execute(
        "UPDATE assistants SET voice_mode = 'tts' WHERE voice_provider IS NOT NULL",
    )
    # Re-add the check constraint
    op.create_check_constraint(
        "ck_assistant_voice_mode",
        "assistants",
        "voice_mode IN ('tts', 'sts')",
    )
