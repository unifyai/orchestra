"""Managed Computer Use paid add-on billing fields and pricing seed.

Revision ID: managed_desktop_addon
Revises: assistant_owner_team
Create Date: 2026-07-16 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "managed_desktop_addon"
down_revision = "assistant_owner_team"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistants",
        sa.Column("managed_desktop_status", sa.String(), nullable=True),
    )
    op.add_column(
        "assistants",
        sa.Column("managed_desktop_monthly_cost", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "assistants",
        sa.Column("managed_desktop_last_billed_month", sa.String(), nullable=True),
    )
    op.add_column(
        "assistants",
        sa.Column(
            "managed_desktop_grace_period_started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "assistants",
        sa.Column(
            "managed_desktop_enabled_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )

    op.drop_constraint("ck_contact_type_cost_type", "contact_type_costs", type_="check")
    op.create_check_constraint(
        "ck_contact_type_cost_type",
        "contact_type_costs",
        "contact_type IN ('phone', 'email', 'whatsapp', 'discord', 'managed_desktop')",
    )

    op.execute(
        """
        INSERT INTO contact_type_costs (contact_type, provider, country_code, monthly_cost, one_time_cost)
        VALUES
            ('managed_desktop', 'ubuntu', NULL, 50.00, 0.00),
            ('managed_desktop', 'windows', NULL, 75.00, 0.00)
        ON CONFLICT (contact_type, provider, country_code) DO NOTHING
        """,
    )

    op.execute(
        """
        UPDATE assistants
        SET
            managed_desktop_status = 'active',
            managed_desktop_enabled_at = NOW()
        WHERE desktop_mode IN ('ubuntu', 'windows')
          AND managed_desktop_status IS NULL
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM contact_type_costs
        WHERE contact_type = 'managed_desktop'
        """,
    )

    op.drop_constraint("ck_contact_type_cost_type", "contact_type_costs", type_="check")
    op.create_check_constraint(
        "ck_contact_type_cost_type",
        "contact_type_costs",
        "contact_type IN ('phone', 'email', 'whatsapp', 'discord')",
    )

    op.drop_column("assistants", "managed_desktop_enabled_at")
    op.drop_column("assistants", "managed_desktop_grace_period_started_at")
    op.drop_column("assistants", "managed_desktop_last_billed_month")
    op.drop_column("assistants", "managed_desktop_monthly_cost")
    op.drop_column("assistants", "managed_desktop_status")
