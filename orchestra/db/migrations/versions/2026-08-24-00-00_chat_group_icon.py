"""Add the emoji icon a chat group displays instead of its member faces.

Revision ID: chat_group_icon
Revises: merge_dashboard_referral
Create Date: 2026-08-24 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "chat_group_icon"
down_revision = "merge_dashboard_referral"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_group", sa.Column("icon", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_group", "icon")
