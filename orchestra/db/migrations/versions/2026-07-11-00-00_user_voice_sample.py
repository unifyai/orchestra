"""Add voice enrollment sample columns to the user table.

Stores the gs:// URL of the user's recorded voice sample plus its upload
timestamp. Assistants pull the sample to build a speaker embedding that
voice-verifies the user's turns on calls.

Revision ID: user_voice_sample
Revises: idx_assistant_contacts_lookup
Create Date: 2026-07-11 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "user_voice_sample"
down_revision = "idx_assistant_contacts_lookup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("voice_sample", sa.String(), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column(
            "voice_sample_uploaded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("user", "voice_sample_uploaded_at")
    op.drop_column("user", "voice_sample")
