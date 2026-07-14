"""Add optional profile image to organization teams.

Revision ID: team_profile_image
Revises: provider_event_context_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "team_profile_image"
down_revision = "provider_event_context_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "team",
        sa.Column("image", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("team", "image")
