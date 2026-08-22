"""Record the Gmail thread an interview ask created.

Revision ID: founder_interview_thread
Revises: chat_group_icon
Create Date: 2026-08-25 00:00:00.000000

The ask is sent from a founder's own mailbox, which holds years of unrelated
correspondence. Anything that later answers replies to it must know which
threads are ours by record rather than by matching subjects, so the set it can
act on is one we wrote down at send time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "founder_interview_thread"
down_revision = "chat_group_icon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "founder_interview_thread_id",
            sa.String(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("assistants", "founder_interview_thread_id")
