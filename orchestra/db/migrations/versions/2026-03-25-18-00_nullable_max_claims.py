"""Make max_claims nullable (NULL = unlimited).

Revision ID: nullable_max_claims
Revises: multi_claim_credit_grant_links
Create Date: 2026-03-25 18:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "nullable_max_claims"
down_revision = "multi_claim_credit_grant_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "one_time_credit_grant_link",
        "max_claims",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE one_time_credit_grant_link SET max_claims = 2147483647 "
        "WHERE max_claims IS NULL",
    )
    op.alter_column(
        "one_time_credit_grant_link",
        "max_claims",
        existing_type=sa.Integer(),
        nullable=False,
    )
