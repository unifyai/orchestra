"""Add suspension_reason to billing_account.

Tracks why an account was suspended (dispute, admin_freeze).
NULL means no suspension reason — expected for ACTIVE/CLOSED accounts
and legacy SUSPENDED accounts from before this migration.

Revision ID: add_suspension_reason
Revises: remove_past_due_status
Create Date: 2026-03-29 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_suspension_reason"
down_revision = "remove_past_due_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "billing_account",
        sa.Column("suspension_reason", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("billing_account", "suspension_reason")
