"""Drop has_claimed_credit_grant_link from user table.

The claim-tracking is now derived at read time by querying the
one_time_credit_grant_link table (user_id column) instead of storing a
denormalized boolean flag on the user row.

Revision ID: drop_has_claimed_credit_grant
Revises: add_billing_account
Create Date: 2026-02-15 14:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "drop_has_claimed_credit_grant"
down_revision = "add_billing_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("user", "has_claimed_credit_grant_link")


def downgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "has_claimed_credit_grant_link",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Back-fill the flag from claimed links
    op.execute(
        """
        UPDATE "user" u
        SET has_claimed_credit_grant_link = true
        FROM one_time_credit_grant_link l
        WHERE l.user_id = u.id
        """,
    )
