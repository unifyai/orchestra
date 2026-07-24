"""MS Teams bot per-conversation welcome claims.

Adds ``ms_teams_bot_welcomes``: a one-shot record that a given Teams
conversation has already received the install welcome. The install welcome
must fire exactly once per conversation, but Teams and the Bot Connector's
at-least-once retries can redeliver the bot-add ``conversationUpdate`` for the
same conversation. Claiming the welcome with an idempotent insert keyed on
``(install_id, conversation_id)`` lets the adapter greet a brand-new
conversation once and stay silent on every redelivery — satisfying the Store
"does not spam users with repeating welcome messages" rule while still
greeting each distinct personal chat / team channel.

Constraint / index names match what ``meta.create_all`` produces from the ORM
model so fresh test DBs and migrated production DBs agree.

Revision ID: ms_teams_bot_welcomes
Revises: knowledge_ledger_auto_count
Create Date: 2026-08-12 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ms_teams_bot_welcomes"
down_revision = "knowledge_ledger_auto_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ms_teams_bot_welcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "install_id",
            sa.Integer(),
            sa.ForeignKey("ms_teams_bot_installs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column(
            "welcomed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "install_id",
            "conversation_id",
            name="uq_ms_teams_bot_welcome",
        ),
    )
    op.create_index(
        "ix_ms_teams_bot_welcomes_install_id",
        "ms_teams_bot_welcomes",
        ["install_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ms_teams_bot_welcomes_install_id",
        table_name="ms_teams_bot_welcomes",
    )
    op.drop_table("ms_teams_bot_welcomes")
