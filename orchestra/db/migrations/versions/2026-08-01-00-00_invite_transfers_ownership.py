"""Add organization_invite.transfers_ownership for pending-owner invites.

Revision ID: invite_transfers_ownership
Revises: billing_trial_fields
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "invite_transfers_ownership"
down_revision = "billing_trial_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organization_invite",
        sa.Column(
            "transfers_ownership",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # One outstanding hand-over per organization: a second acceptance would
    # silently demote the owner installed by the first.
    op.create_index(
        "uq_organization_invite_pending_owner",
        "organization_invite",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("transfers_ownership"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_organization_invite_pending_owner",
        table_name="organization_invite",
    )
    op.drop_column("organization_invite", "transfers_ownership")
