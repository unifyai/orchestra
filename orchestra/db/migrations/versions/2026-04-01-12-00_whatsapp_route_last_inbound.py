"""Add last_inbound_at to whatsapp_routes for 24h window tracking.

Revision ID: whatsapp_route_last_inbound
Revises: add_phone_verifications
Create Date: 2026-04-01 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "whatsapp_route_last_inbound"
down_revision = "add_phone_verifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_routes",
        sa.Column(
            "last_inbound_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_routes", "last_inbound_at")
