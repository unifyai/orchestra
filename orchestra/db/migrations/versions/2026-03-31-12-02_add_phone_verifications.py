"""Add phone_verifications table for server-side phone/WhatsApp verification.

Revision ID: add_phone_verifications
Revises: centralize_user_contacts
Create Date: 2026-03-31 12:02:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_phone_verifications"
down_revision = "centralize_user_contacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "phone_verifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("phone_type", sa.String(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column(
            "expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "verified_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "phone_type IN ('phone', 'whatsapp')",
            name="ck_phone_verifications_type",
        ),
    )


def downgrade() -> None:
    op.drop_table("phone_verifications")
