"""WhatsApp pool routing: pool numbers, routes, and user.whatsapp_number.

Adds:
- ``user.whatsapp_number`` column with unique partial index
- ``whatsapp_pool_numbers`` table seeded with the 2 current numbers
- ``whatsapp_routes`` table for external-contact → assistant routing
- Modifies ``uq_active_contact_value`` to exclude WhatsApp contacts
  (pool numbers are shared across assistants)

Revision ID: whatsapp_pool_routing
Revises: nullable_max_claims
Create Date: 2026-03-31 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "whatsapp_pool_routing"
down_revision = "add_suspension_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add whatsapp_number to user table
    op.add_column("user", sa.Column("whatsapp_number", sa.String(), nullable=True))
    op.create_index(
        "uq_user_whatsapp_number",
        "user",
        ["whatsapp_number"],
        unique=True,
        postgresql_where=sa.text("whatsapp_number IS NOT NULL"),
    )

    # 2. Create whatsapp_pool_numbers table
    op.create_table(
        "whatsapp_pool_numbers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("number", sa.String(), nullable=False, unique=True),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="active",
        ),
        sa.Column("twilio_sender_sid", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_whatsapp_pool_number_status",
        ),
    )

    # Seed the 2 current WhatsApp numbers
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO whatsapp_pool_numbers (number) VALUES (:n1), (:n2)",
        ),
        {"n1": "+18507877970", "n2": "+17343611691"},
    )

    # 3. Create whatsapp_routes table
    op.create_table(
        "whatsapp_routes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "pool_number_id",
            sa.Integer(),
            sa.ForeignKey("whatsapp_pool_numbers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contact_number", sa.String(), nullable=False),
        sa.Column(
            "assistant_id",
            sa.Integer(),
            sa.ForeignKey("assistants.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "pool_number_id",
            "contact_number",
            name="uq_pool_contact",
        ),
    )
    op.create_index(
        "ix_whatsapp_routes_assistant",
        "whatsapp_routes",
        ["assistant_id", "contact_number"],
    )
    op.create_index(
        "ix_whatsapp_routes_contact",
        "whatsapp_routes",
        ["contact_number"],
    )

    # 4. Modify uq_active_contact_value to exclude WhatsApp
    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text(
            "status != 'deleted' AND contact_type != 'whatsapp'",
        ),
    )


def downgrade() -> None:
    # 4. Restore original uq_active_contact_value
    op.drop_index("uq_active_contact_value", table_name="assistant_contacts")
    op.create_index(
        "uq_active_contact_value",
        "assistant_contacts",
        ["contact_value"],
        unique=True,
        postgresql_where=sa.text("status != 'deleted'"),
    )

    # 3. Drop whatsapp_routes
    op.drop_index("ix_whatsapp_routes_contact", table_name="whatsapp_routes")
    op.drop_index("ix_whatsapp_routes_assistant", table_name="whatsapp_routes")
    op.drop_table("whatsapp_routes")

    # 2. Drop whatsapp_pool_numbers
    op.drop_table("whatsapp_pool_numbers")

    # 1. Drop user.whatsapp_number
    op.drop_index("uq_user_whatsapp_number", table_name="user")
    op.drop_column("user", "whatsapp_number")
