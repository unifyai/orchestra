"""Re-price microsoft_365 email to reflect Business Premium license.

Revision ID: ms365_business_premium_pricing
Revises: assistant_secret_pk_drop_user_id
Create Date: 2026-04-17 13:00:00.000000

The Communication service now provisions Outlook mailboxes with the Microsoft
365 Business Premium SKU (GUID cbdc14ab-d96c-4c30-b9f4-6ada7cdc1d46) instead
of Exchange Online Plan 2 (19ec0d23-8335-4cbd-94ac-6050e30712fa). Business
Premium lists at $22/user/mo vs. $8 for EXO P2, so we raise the passthrough
charge to $25/mo to preserve a comparable margin. The one-time setup fee is
unchanged.
"""

from alembic import op

revision = "ms365_business_premium_pricing"
down_revision = "assistant_secret_pk_drop_user_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE contact_type_costs
        SET monthly_cost = 25.00
        WHERE contact_type = 'email' AND provider = 'microsoft_365'
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE contact_type_costs
        SET monthly_cost = 12.50
        WHERE contact_type = 'email' AND provider = 'microsoft_365'
        """,
    )
