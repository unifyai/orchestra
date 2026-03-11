"""Add free_trial boolean to organization table.

Revision ID: add_org_free_trial
Revises: drop_ccf_table
Create Date: 2026-03-11 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_org_free_trial"
down_revision = "drop_ccf_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organization",
        sa.Column(
            "free_trial",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("organization", "free_trial")
