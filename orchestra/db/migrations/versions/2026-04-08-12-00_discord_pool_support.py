"""Add Discord pool support.

Revision ID: discord_pool_support
Revises: backfill_credit_ledger
Create Date: 2026-04-08 12:00:00.000000

Adds User.discord_id column with partial unique index, extends CHECK
constraints on assistant_contacts and contact_type_costs to include
'discord', and updates the uq_active_contact_value index to exclude
Discord (pool bot IDs are shared across assistants like WhatsApp numbers).
"""

import sqlalchemy as sa
from alembic import op

revision = "discord_pool_support"
down_revision = "backfill_credit_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- User.discord_id ---
    op.add_column("user", sa.Column("discord_id", sa.String(), nullable=True))
    op.create_index(
        "uq_user_discord_id",
        "user",
        ["discord_id"],
        unique=True,
        postgresql_where=sa.text("discord_id IS NOT NULL"),
    )

    # --- AssistantContact CHECK constraint ---
    op.drop_constraint("ck_assistant_contact_type", "assistant_contacts")
    op.create_check_constraint(
        "ck_assistant_contact_type",
        "assistant_contacts",
        "contact_type IN ('phone', 'email', 'whatsapp', 'discord')",
    )

    # --- uq_active_contact_value index (exclude discord like whatsapp) ---
    op.drop_index("uq_active_contact_value", "assistant_contacts")
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(
            "status != 'deleted' AND contact_type NOT IN ('whatsapp', 'discord')",
        ),
    )

    # --- AssistantContactCost CHECK constraint ---
    op.drop_constraint("ck_contact_type_cost_type", "contact_type_costs")
    op.create_check_constraint(
        "ck_contact_type_cost_type",
        "contact_type_costs",
        "contact_type IN ('phone', 'email', 'whatsapp', 'discord')",
    )

    # --- Seed Discord cost row ---
    op.execute(
        """
        INSERT INTO contact_type_costs (contact_type, provider, country_code, monthly_cost, one_time_cost)
        VALUES ('discord', 'discord', NULL, 0, 0)
        ON CONFLICT (contact_type, provider, country_code) DO NOTHING
        """,
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM contact_type_costs WHERE contact_type = 'discord' AND provider = 'discord'",
    )

    op.drop_constraint("ck_contact_type_cost_type", "contact_type_costs")
    op.create_check_constraint(
        "ck_contact_type_cost_type",
        "contact_type_costs",
        "contact_type IN ('phone', 'email', 'whatsapp')",
    )

    op.drop_index("uq_active_contact_value", "assistant_contacts")
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(
            "status != 'deleted' AND contact_type != 'whatsapp'",
        ),
    )

    op.drop_constraint("ck_assistant_contact_type", "assistant_contacts")
    op.create_check_constraint(
        "ck_assistant_contact_type",
        "assistant_contacts",
        "contact_type IN ('phone', 'email', 'whatsapp')",
    )

    op.drop_index("uq_user_discord_id", "user")
    op.drop_column("user", "discord_id")
