"""Add call permission columns to shared_platform_routes.

Revision ID: add_whatsapp_call_permission
Revises: add_assistant_cleanup_tasks
Create Date: 2026-04-06 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_whatsapp_call_permission"
down_revision = "add_assistant_cleanup_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shared_platform_routes",
        sa.Column("call_permission_status", sa.String(), nullable=True),
    )
    op.add_column(
        "shared_platform_routes",
        sa.Column(
            "call_permission_granted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "shared_platform_routes",
        sa.Column(
            "call_permission_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("shared_platform_routes", "call_permission_expires_at")
    op.drop_column("shared_platform_routes", "call_permission_granted_at")
    op.drop_column("shared_platform_routes", "call_permission_status")
