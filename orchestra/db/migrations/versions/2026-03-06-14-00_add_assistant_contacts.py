"""Add assistant_contacts and contact_type_costs tables.

Creates two tables for the contact details billing feature:

- ``assistant_contacts``  – tracks provisioned contact details per assistant
  with lifecycle status, billing metadata, and a partial unique index on
  contact_value for active contacts.
- ``contact_type_costs``  – configuration table for monthly/one-time costs
  per contact type, provider, and country.

Also backfills ``assistant_contacts`` rows from existing ``assistants`` columns
(phone, email, assistant_whatsapp_number) so the new table is in sync from
the start.

Revision ID: add_assistant_contacts
Revises: drop_desktop_url
Create Date: 2026-03-06 14:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "add_assistant_contacts"
down_revision = "drop_desktop_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. assistant_contacts table
    # ------------------------------------------------------------------
    op.create_table(
        "assistant_contacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("contact_type", sa.String(), nullable=False),
        sa.Column("contact_value", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column(
            "provisioned_by",
            sa.String(),
            nullable=False,
            server_default="platform",
        ),
        sa.Column("country_code", sa.String(), nullable=True),
        sa.Column("user_value", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "grace_period_started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("last_billed_month", sa.String(), nullable=True),
        sa.Column("monthly_cost", sa.Numeric(), nullable=True),
        # Check constraints
        sa.CheckConstraint(
            "contact_type IN ('phone', 'email', 'whatsapp')",
            name="ck_assistant_contact_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'grace_period', 'deleted')",
            name="ck_assistant_contact_status",
        ),
        sa.CheckConstraint(
            "provisioned_by IN ('platform', 'user')",
            name="ck_assistant_contact_provisioned_by",
        ),
    )

    # Partial unique indexes (only consider non-deleted rows)
    op.create_index(
        "uq_assistant_contact_type_active",
        "assistant_contacts",
        ["assistant_id", "contact_type"],
        unique=True,
        postgresql_where=sa.text("status != 'deleted'"),
    )
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text("status != 'deleted'"),
    )

    # ------------------------------------------------------------------
    # 2. contact_type_costs table
    # ------------------------------------------------------------------
    op.create_table(
        "contact_type_costs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("contact_type", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("country_code", sa.String(), nullable=True),
        sa.Column("monthly_cost", sa.Numeric(), nullable=False),
        sa.Column(
            "one_time_cost",
            sa.Numeric(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "effective_from",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "contact_type",
            "provider",
            "country_code",
            name="uq_contact_cost",
        ),
        sa.CheckConstraint(
            "contact_type IN ('phone', 'email', 'whatsapp')",
            name="ck_contact_type_cost_type",
        ),
    )

    # ------------------------------------------------------------------
    # 3. Seed initial cost data
    # ------------------------------------------------------------------
    contact_type_costs = sa.table(
        "contact_type_costs",
        sa.column("contact_type", sa.String),
        sa.column("provider", sa.String),
        sa.column("country_code", sa.String),
        sa.column("monthly_cost", sa.Numeric),
        sa.column("one_time_cost", sa.Numeric),
    )
    op.bulk_insert(
        contact_type_costs,
        [
            {
                "contact_type": "phone",
                "provider": "twilio",
                "country_code": "US",
                "monthly_cost": 1.50,
                "one_time_cost": 5.00,
            },
            {
                "contact_type": "phone",
                "provider": "twilio",
                "country_code": "GB",
                "monthly_cost": 1.50,
                "one_time_cost": 5.00,
            },
            {
                "contact_type": "email",
                "provider": "google_workspace",
                "country_code": None,
                "monthly_cost": 14.00,
                "one_time_cost": 5.00,
            },
            {
                "contact_type": "whatsapp",
                "provider": "twilio",
                "country_code": None,
                "monthly_cost": 5.00,
                "one_time_cost": 5.00,
            },
        ],
    )

    # ------------------------------------------------------------------
    # 4. Backfill assistant_contacts from existing assistants columns
    # ------------------------------------------------------------------
    conn = op.get_bind()

    # Phone contacts
    conn.execute(
        sa.text(
            """
            INSERT INTO assistant_contacts
                (assistant_id, contact_type, contact_value, provider,
                 provisioned_by, country_code, user_value, status, created_at, updated_at)
            SELECT
                agent_id, 'phone', phone, 'twilio',
                'platform', phone_country, user_phone, 'active', created_at, NOW()
            FROM assistants
            WHERE phone IS NOT NULL AND phone != ''
        """,
        ),
    )

    # Email contacts
    conn.execute(
        sa.text(
            """
            INSERT INTO assistant_contacts
                (assistant_id, contact_type, contact_value, provider,
                 provisioned_by, status, created_at, updated_at)
            SELECT
                agent_id, 'email', email, 'google_workspace',
                'platform', 'active', created_at, NOW()
            FROM assistants
            WHERE email IS NOT NULL AND email != ''
        """,
        ),
    )

    # WhatsApp contacts
    conn.execute(
        sa.text(
            """
            INSERT INTO assistant_contacts
                (assistant_id, contact_type, contact_value, provider,
                 provisioned_by, user_value, status, created_at, updated_at)
            SELECT
                agent_id, 'whatsapp', assistant_whatsapp_number, 'twilio',
                'platform', user_whatsapp_number, 'active', created_at, NOW()
            FROM assistants
            WHERE assistant_whatsapp_number IS NOT NULL
              AND assistant_whatsapp_number != ''
        """,
        ),
    )


def downgrade() -> None:
    op.drop_table("assistant_contacts")
    op.drop_table("contact_type_costs")
