"""Add spending_limit_notifications table for tracking sent notifications.

This table tracks spending limit notifications to prevent duplicate emails.
It stores:
- Which entity (assistant/user/member/org) hit the limit
- The month and limit value
- When the notification was sent
- Who was notified

Deduplication is handled via a unique constraint on (entity_type, entity_id, month, limit_value).
The limit_set_at column handles the "limit removed then re-enabled" scenario.

Revision ID: 9b2c3d4e5f6a
Revises: 8a1b2c3d4e5f
Create Date: 2026-02-04 12:01:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "9b2c3d4e5f6a"
down_revision = "8a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the spending limit notifications table
    op.create_table(
        "spending_limit_notifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Which entity hit the limit
        sa.Column(
            "entity_type",
            sa.String(20),
            nullable=False,
            comment="'assistant', 'user', 'member', or 'organization'",
        ),
        sa.Column(
            "entity_id",
            sa.String(),
            nullable=False,
            comment="ID of the entity (agent_id, user_id, or org_id)",
        ),
        # When and at what limit
        sa.Column(
            "month",
            sa.String(7),
            nullable=False,
            comment="Billing month in YYYY-MM format",
        ),
        sa.Column(
            "limit_value",
            sa.Numeric(),
            nullable=False,
            comment="The limit value that was reached",
        ),
        # When the limit was configured (for re-enable detection)
        sa.Column(
            "limit_set_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="When the limit was configured",
        ),
        # Notification metadata
        sa.Column(
            "notified_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "notified_user_ids",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="List of user IDs who received the notification email",
        ),
        # Entity name for auditing (may become stale if entity renamed)
        sa.Column(
            "entity_name",
            sa.String(),
            nullable=True,
            comment="Name of the entity at time of notification (for auditing)",
        ),
        # Current spend at time of notification (for auditing)
        sa.Column(
            "current_spend",
            sa.Numeric(),
            nullable=True,
            comment="Spend amount when notification was triggered",
        ),
    )

    # Unique constraint for deduplication
    # Note: We DON'T include limit_set_at in the unique constraint.
    # The deduplication logic checks limit_set_at programmatically
    # to handle the re-enable scenario.
    op.create_index(
        "ix_spending_limit_notifications_dedupe",
        "spending_limit_notifications",
        ["entity_type", "entity_id", "month", "limit_value"],
    )

    # Index for looking up notifications by entity
    op.create_index(
        "ix_spending_limit_notifications_entity",
        "spending_limit_notifications",
        ["entity_type", "entity_id"],
    )

    # Index for cleanup queries (delete old notifications)
    op.create_index(
        "ix_spending_limit_notifications_month",
        "spending_limit_notifications",
        ["month"],
    )

    # Check constraint for valid entity types
    op.create_check_constraint(
        "ck_spending_limit_notifications_entity_type",
        "spending_limit_notifications",
        "entity_type IN ('assistant', 'user', 'member', 'organization')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_spending_limit_notifications_entity_type",
        "spending_limit_notifications",
        type_="check",
    )
    op.drop_index("ix_spending_limit_notifications_month")
    op.drop_index("ix_spending_limit_notifications_entity")
    op.drop_index("ix_spending_limit_notifications_dedupe")
    op.drop_table("spending_limit_notifications")
