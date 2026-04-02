"""Shared-pool conflict resolution: rename tables, platform column, decommissioned routes, conflict events.

Renames:
- ``whatsapp_pool_numbers`` → ``shared_pool_numbers``
- ``whatsapp_routes`` → ``shared_platform_routes``

Adds:
- ``platform`` column to ``shared_pool_numbers`` (default 'whatsapp')
- ``decommissioned_routes`` table for stale-number auto-reply detection
- ``conflict_events`` table for conflict resolution audit + notification tracking

Revision ID: shared_pool_conflict_resolution
Revises: whatsapp_route_last_inbound
Create Date: 2026-04-02 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "shared_pool_conflict_resolution"
down_revision = "whatsapp_route_last_inbound"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Rename tables
    op.rename_table("whatsapp_pool_numbers", "shared_pool_numbers")
    op.rename_table("whatsapp_routes", "shared_platform_routes")

    # 2. Rename constraints and indexes to match new table names
    op.execute(
        "ALTER TABLE shared_pool_numbers "
        "RENAME CONSTRAINT ck_whatsapp_pool_number_status "
        "TO ck_shared_pool_number_status",
    )
    op.execute(
        "ALTER INDEX ix_whatsapp_routes_assistant RENAME TO ix_shared_routes_assistant",
    )
    op.execute(
        "ALTER INDEX ix_whatsapp_routes_contact RENAME TO ix_shared_routes_contact",
    )

    # 3. Add platform column
    op.add_column(
        "shared_pool_numbers",
        sa.Column(
            "platform",
            sa.String(),
            nullable=False,
            server_default="whatsapp",
        ),
    )

    # 3. Create decommissioned_routes table
    op.create_table(
        "decommissioned_routes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column(
            "pool_number_id",
            sa.Integer(),
            sa.ForeignKey("shared_pool_numbers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contact_identifier", sa.String(), nullable=False),
        sa.Column(
            "old_assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "new_pool_number_id",
            sa.Integer(),
            sa.ForeignKey("shared_pool_numbers.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "decommissioned_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_decommissioned_routes_lookup",
        "decommissioned_routes",
        ["pool_number_id", "contact_identifier"],
    )

    # 4. Create conflict_events table
    op.create_table(
        "conflict_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("conflict_type", sa.String(), nullable=False),
        sa.Column(
            "trigger_assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "affected_assistant_ids",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "old_pool_assignments",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "new_pool_assignments",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "notification_recipients",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "notification_status",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="notifying",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "resolved_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "conflict_type IN ('contact_overlap', 'user_to_user', 'org_membership')",
            name="ck_conflict_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('notifying', 'resolved', 'notification_failed', 'failed')",
            name="ck_conflict_event_status",
        ),
    )
    op.create_index(
        "ix_conflict_events_status",
        "conflict_events",
        ["status"],
    )
    op.create_index(
        "ix_conflict_events_trigger_assistant",
        "conflict_events",
        ["trigger_assistant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conflict_events_trigger_assistant", table_name="conflict_events")
    op.drop_index("ix_conflict_events_status", table_name="conflict_events")
    op.drop_table("conflict_events")

    op.drop_index(
        "ix_decommissioned_routes_lookup",
        table_name="decommissioned_routes",
    )
    op.drop_table("decommissioned_routes")

    op.drop_column("shared_pool_numbers", "platform")

    op.execute(
        "ALTER INDEX ix_shared_routes_contact RENAME TO ix_whatsapp_routes_contact",
    )
    op.execute(
        "ALTER INDEX ix_shared_routes_assistant RENAME TO ix_whatsapp_routes_assistant",
    )
    op.execute(
        "ALTER TABLE shared_pool_numbers "
        "RENAME CONSTRAINT ck_shared_pool_number_status "
        "TO ck_whatsapp_pool_number_status",
    )

    op.rename_table("shared_platform_routes", "whatsapp_routes")
    op.rename_table("shared_pool_numbers", "whatsapp_pool_numbers")
