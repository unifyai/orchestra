"""Distinguish console-session API keys from programmatic ones.

Orchestra has had exactly one kind of credential: the Console forwards the
user's own key on every server-side call, and that same key is printed in
Profile under "used for programmatic integration". Nothing in a request
therefore says where it came from, so "free credits are spendable only
through the Console" is not expressible.

``kind`` makes it expressible. Existing keys become ``programmatic``
because they are already in users' hands and printed in the UI; a
``console`` key is minted separately and never displayed.

Revision ID: api_key_kind
Revises: member_staff_access
Create Date: 2026-08-02 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "api_key_kind"
down_revision = "member_staff_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_key",
        sa.Column(
            "kind",
            sa.String(),
            nullable=False,
            server_default="programmatic",
        ),
    )
    # Console-key lookup happens on every authenticated Console request.
    op.create_index(
        "ix_api_key_user_kind",
        "api_key",
        ["user_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_key_user_kind", table_name="api_key")
    op.drop_column("api_key", "kind")
