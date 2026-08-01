"""Add organization_member staff-access marker and expiry.

Marks a Unify person sitting in a customer org for onboarding or setup,
so the customer can tell the seat apart from their own people and see
when the arrangement lapses. NULL expiry means the grant never lapses.

Revision ID: member_staff_access
Revises: invite_transfers_ownership
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "member_staff_access"
down_revision = "invite_transfers_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organization_member",
        sa.Column(
            "is_staff_access",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "organization_member",
        sa.Column(
            "staff_access_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    # Existing rows keep the default: nothing is retro-badged. Staff seats
    # already in customer orgs are marked deliberately via the admin
    # endpoint, so the migration never silently expires live access.
    op.create_index(
        "ix_org_member_staff_access_expiry",
        "organization_member",
        ["staff_access_expires_at"],
        postgresql_where=sa.text("is_staff_access"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_org_member_staff_access_expiry",
        table_name="organization_member",
    )
    op.drop_column("organization_member", "staff_access_expires_at")
    op.drop_column("organization_member", "is_staff_access")
