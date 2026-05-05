"""Ensure each organization has one Coordinator assistant.

Revision ID: add_org_coordinator_singleton
Revises: add_coordinator_columns
Create Date: 2026-04-30 14:10:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_org_coordinator_singleton"
down_revision = "add_coordinator_columns"
branch_labels = None
depends_on = None

ORG_COORDINATOR_INDEX_NAME = "ux_assistants_one_coordinator_per_org"


def upgrade() -> None:
    op.create_index(
        ORG_COORDINATOR_INDEX_NAME,
        "assistants",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text(
            "is_coordinator AND organization_id IS NOT NULL",
        ),
    )


def downgrade() -> None:
    op.drop_index(
        ORG_COORDINATOR_INDEX_NAME,
        table_name="assistants",
    )
