"""Drop credit_card_fingerprint table.

The credit_card_fingerprint table is no longer used by any application code.
The DAO and all related endpoints have been removed.

Revision ID: drop_ccf_table
Revises: drop_asst_name_uq
Create Date: 2026-03-09 14:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "drop_ccf_table"
down_revision = "drop_asst_name_uq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("credit_card_fingerprint")


def downgrade() -> None:
    op.create_table(
        "credit_card_fingerprint",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("billing_account_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["billing_account_id"],
            ["billing_account.id"],
            name="fk_credit_card_fingerprint_billing_account_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_credit_card_fingerprint_billing_account_id",
        "credit_card_fingerprint",
        ["billing_account_id"],
    )
