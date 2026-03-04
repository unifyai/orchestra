"""Add organization_id to one_time_credit_grant_link.

Allows credit grant links to be claimed for an organization's billing
account (when the claimer uses an org API key) in addition to the
existing personal claim flow.

Revision ID: credit_grant_link_org_support
Revises: onboard_existing_users
Create Date: 2026-03-03 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "credit_grant_link_org_support"
down_revision = "onboard_existing_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "one_time_credit_grant_link",
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id"),
            nullable=True,
            comment="Organization that received the credits (NULL = personal claim)",
        ),
    )
    op.create_index(
        "ix_one_time_credit_grant_link_organization_id",
        "one_time_credit_grant_link",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_one_time_credit_grant_link_organization_id",
        table_name="one_time_credit_grant_link",
    )
    op.drop_column("one_time_credit_grant_link", "organization_id")
