"""Add MFA enforcement column to organization table

Adds require_mfa (boolean) column to the organization table
for Phase 3: Organization-Controlled MFA Enforcement.

Revision ID: add_org_mfa_enforcement
Revises: add_mfa_tables
Create Date: 2026-02-26 14:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_org_mfa_enforcement"
down_revision = "add_mfa_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organization",
        sa.Column(
            "require_mfa",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("organization", "require_mfa")
