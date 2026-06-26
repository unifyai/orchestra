"""Track WhatsApp call permission request and provider metadata.

Revision ID: whatsapp_call_permission_state
Revises: realign_universal_unity_idx
Create Date: 2026-07-02 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "whatsapp_call_permission_state"
down_revision = "realign_universal_unity_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shared_platform_routes",
        sa.Column(
            "call_permission_requested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "shared_platform_routes",
        sa.Column(
            "call_permission_last_provider_event_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "shared_platform_routes",
        sa.Column("call_permission_source", sa.String(), nullable=True),
    )
    op.add_column(
        "shared_platform_routes",
        sa.Column("pending_whatsapp_call_context", sa.Text(), nullable=True),
    )
    op.add_column(
        "shared_platform_routes",
        sa.Column(
            "pending_whatsapp_call_context_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("shared_platform_routes", "pending_whatsapp_call_context_at")
    op.drop_column("shared_platform_routes", "pending_whatsapp_call_context")
    op.drop_column("shared_platform_routes", "call_permission_source")
    op.drop_column("shared_platform_routes", "call_permission_last_provider_event_at")
    op.drop_column("shared_platform_routes", "call_permission_requested_at")
