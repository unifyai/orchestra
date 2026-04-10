"""Add auth_token to shared_pool_numbers.

Revision ID: shared_pool_auth_token
Revises: discord_pool_support
Create Date: 2026-04-08 13:00:00.000000

Generic credential column for platform-specific authentication tokens
(e.g. Discord bot tokens). Nullable because WhatsApp numbers use
env-var Twilio credentials instead of per-row tokens.
"""

import sqlalchemy as sa
from alembic import op

revision = "shared_pool_auth_token"
down_revision = "discord_pool_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shared_pool_numbers",
        sa.Column("auth_token", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shared_pool_numbers", "auth_token")
