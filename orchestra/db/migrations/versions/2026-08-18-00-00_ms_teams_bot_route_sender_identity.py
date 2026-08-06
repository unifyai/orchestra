"""Sender identity on Microsoft Teams bot conversation routes.

A route already stores everything an outbound proactive reply needs
(``conversation_id`` plus the install's tenant), but it was only ever
reachable by ``(install_id, conversation_id)`` — you had to know the
answer to look it up. An assistant with no inbound Teams activity in
context, such as a scheduled offline task run, therefore could not
target a Teams conversation at all.

These columns record *who* the conversation is with, using the identity
the Bot Framework activity actually carries, so a route can be found
from the assistant side. ``sender_is_owner`` distinguishes the boss's
own 1:1 chat from a third party's, and ``conversation_type`` separates
personal chats from group/channel threads.

Nullable throughout: existing rows predate the capture, and Teams does
not supply a sender email on the first dispatch pass of an org install.

Revision ID: ms_teams_route_sender
Revises: drop_plan_pricing_factors
Create Date: 2026-08-18 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ms_teams_route_sender"
down_revision = "drop_plan_pricing_factors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ms_teams_bot_conversation_routes",
        sa.Column("conversation_type", sa.String(), nullable=True),
    )
    op.add_column(
        "ms_teams_bot_conversation_routes",
        sa.Column("sender_aad_object_id", sa.String(), nullable=True),
    )
    op.add_column(
        "ms_teams_bot_conversation_routes",
        sa.Column("sender_email", sa.String(), nullable=True),
    )
    op.add_column(
        "ms_teams_bot_conversation_routes",
        sa.Column(
            "sender_is_owner",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.create_index(
        "ix_ms_teams_bot_conversation_routes_assistant_lookup",
        "ms_teams_bot_conversation_routes",
        ["assistant_id", "conversation_type"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ms_teams_bot_conversation_routes_assistant_lookup",
        table_name="ms_teams_bot_conversation_routes",
    )
    op.drop_column("ms_teams_bot_conversation_routes", "sender_is_owner")
    op.drop_column("ms_teams_bot_conversation_routes", "sender_email")
    op.drop_column("ms_teams_bot_conversation_routes", "sender_aad_object_id")
    op.drop_column("ms_teams_bot_conversation_routes", "conversation_type")
