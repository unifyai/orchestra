"""Seed MS365 email provider cost row.

Revision ID: ms365_email_provider
Revises: shared_pool_auth_token
Create Date: 2026-04-13 12:00:00.000000

Adds a cost row for email contacts provisioned via Microsoft 365 (Exchange
Online) alongside the existing Google Workspace row.
"""

from alembic import op

revision = "ms365_email_provider"
down_revision = "discord_costs_to_one"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO contact_type_costs
            (contact_type, provider, country_code, monthly_cost, one_time_cost)
        VALUES
            ('email', 'microsoft_365', NULL, 12.50, 5.00)
        ON CONFLICT (contact_type, provider, country_code) DO NOTHING
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM contact_type_costs
        WHERE contact_type = 'email' AND provider = 'microsoft_365'
        """,
    )
