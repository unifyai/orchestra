"""Remove legacy PAST_DUE account status.

Migrates all PAST_DUE accounts to ACTIVE and tightens the CHECK
constraint to only allow ACTIVE, SUSPENDED, CLOSED.

Revision ID: remove_past_due_status
Revises: nullable_max_claims
Create Date: 2026-03-27 12:00:00.000000
"""

from alembic import op

revision = "remove_past_due_status"
down_revision = "nullable_max_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE billing_account SET account_status = 'ACTIVE' "
        "WHERE account_status = 'PAST_DUE'",
    )

    op.drop_constraint("ck_billing_account_status", "billing_account", type_="check")

    op.create_check_constraint(
        "ck_billing_account_status",
        "billing_account",
        "account_status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_billing_account_status", "billing_account", type_="check")

    op.create_check_constraint(
        "ck_billing_account_status",
        "billing_account",
        "account_status IN ('ACTIVE', 'PAST_DUE', 'SUSPENDED', 'CLOSED')",
    )
