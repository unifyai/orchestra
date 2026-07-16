"""Downstream adoption lease and fencing columns for provider-event dispatch.

Revision ID: provider_event_dispatch_adoption
Revises: merge_trigger_regional
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "provider_event_dispatch_adoption"
down_revision = "merge_trigger_regional"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_event_dispatches",
        sa.Column("downstream_adoption_lease_owner", sa.String(), nullable=True),
    )
    op.add_column(
        "provider_event_dispatches",
        sa.Column(
            "downstream_adoption_lease_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "provider_event_dispatches",
        sa.Column(
            "downstream_adoption_fencing_token",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "ix_provider_event_dispatch_adoption_lease",
        "provider_event_dispatches",
        [
            "downstream_adoption_lease_expires_at",
            "downstream_adoption_status",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_event_dispatch_adoption_lease",
        table_name="provider_event_dispatches",
    )
    op.drop_column("provider_event_dispatches", "downstream_adoption_fencing_token")
    op.drop_column("provider_event_dispatches", "downstream_adoption_lease_expires_at")
    op.drop_column("provider_event_dispatches", "downstream_adoption_lease_owner")
