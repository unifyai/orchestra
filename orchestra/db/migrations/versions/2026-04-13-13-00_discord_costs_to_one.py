"""Update Discord contact costs from 0 to 1.

Revision ID: discord_costs_to_one
Revises: ms365_email_provider
Create Date: 2026-04-13 13:00:00.000000
"""

from alembic import op

revision = "discord_costs_to_one"
down_revision = "shared_pool_auth_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE contact_type_costs
        SET monthly_cost = 1, one_time_cost = 1
        WHERE contact_type = 'discord' AND provider = 'discord'
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE contact_type_costs
        SET monthly_cost = 0, one_time_cost = 0
        WHERE contact_type = 'discord' AND provider = 'discord'
        """,
    )
