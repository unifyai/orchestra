"""Provider-event context retention and unavailable reason columns.

Revision ID: provider_event_context_lifecycle
Revises: drop_demo_assistants
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "provider_event_context_lifecycle"
down_revision = "drop_demo_assistants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_event_receipts",
        sa.Column(
            "event_context_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "provider_event_receipts",
        sa.Column("event_context_unavailable_reason", sa.String(), nullable=True),
    )
    op.execute(
        """
        UPDATE provider_event_receipts
        SET event_context_expires_at = NOW() AT TIME ZONE 'UTC' + INTERVAL '30 days'
        WHERE event_context_ref IS NOT NULL
          AND event_context_expires_at IS NULL
        """,
    )
    op.create_index(
        "ix_provider_event_receipt_context_expiry",
        "provider_event_receipts",
        ["event_context_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_event_receipt_context_expiry",
        table_name="provider_event_receipts",
    )
    op.drop_column("provider_event_receipts", "event_context_unavailable_reason")
    op.drop_column("provider_event_receipts", "event_context_expires_at")
