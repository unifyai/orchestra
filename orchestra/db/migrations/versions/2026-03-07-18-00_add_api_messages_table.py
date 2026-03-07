"""Add api_messages table for programmatic REST API messaging.

Stores message state for the send/poll pattern: developers send a message
via POST /messages, then poll GET /messages/{id} for the assistant's response.

Revision ID: add_api_messages
Revises: add_assistant_contacts
Create Date: 2026-03-07 18:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_api_messages"
down_revision = "add_assistant_contacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_messages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="processing",
        ),
        sa.Column("response", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.TIMESTAMP(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("api_messages")
