"""Add the coordinator multiplayer flag.

``assistants.is_multiplayer`` marks a Coordinator that has flipped to
multiplayer mode: hire-like outward identity (own name/voice/avatar and
dedicated contact details) instead of the private boss-only surface. The
flip is one-way and coordinator-only; both invariants are enforced at the
model and service layers, with a check constraint guarding the
coordinator-only half at the database level.

Revision ID: coordinator_multiplayer_flag
Revises: founder_interview_ask
Create Date: 2026-07-30 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "coordinator_multiplayer_flag"
down_revision = "founder_interview_ask"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column(
            "is_multiplayer",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.create_check_constraint(
        "ck_assistants_multiplayer_is_coordinator",
        "assistants",
        "NOT is_multiplayer OR is_coordinator",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_assistants_multiplayer_is_coordinator",
        "assistants",
        type_="check",
    )
    op.drop_column("assistants", "is_multiplayer")
