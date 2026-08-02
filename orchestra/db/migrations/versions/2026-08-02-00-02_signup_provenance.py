"""Record where a signup came from, so burner clusters are detectable.

Once free credits are Console-only, the existing abuse fingerprint stops
working: it keys on LLM spend flowing through the raw-API channel
(``assistant_id IS NULL``), and free accounts can no longer reach that
channel at all. Farming that remains moves to the Console path, where
``assistant_id`` is populated and nothing distinguishes a farmer from a
genuine evaluator on any single-account signal — burn velocity flags an
enthusiastic first session just as readily as a script.

What does separate them is correlation across accounts, and that needs
provenance we were not keeping. The referral table already records
``signup_ip`` for exactly this purpose; ordinary signups recorded
nothing.

The user agent is stored as a salted hash rather than in the clear: the
sweep only ever compares it for equality, so the raw string buys nothing
and is more identifying than we need to retain.

Revision ID: signup_provenance
Revises: api_access_grandfather
Create Date: 2026-08-02 00:02:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "signup_provenance"
down_revision = "api_access_grandfather"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user", sa.Column("signup_ip", sa.String(), nullable=True))
    op.add_column(
        "user",
        sa.Column("signup_user_agent_hash", sa.String(), nullable=True),
    )
    # Cluster detection groups never-paid accounts by origin, so both
    # columns are looked up by value and never by user.
    op.create_index("ix_user_signup_ip", "user", ["signup_ip"])
    op.create_index(
        "ix_user_signup_user_agent_hash",
        "user",
        ["signup_user_agent_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_signup_user_agent_hash", table_name="user")
    op.drop_index("ix_user_signup_ip", table_name="user")
    op.drop_column("user", "signup_user_agent_hash")
    op.drop_column("user", "signup_ip")
