"""Add verification fields to Organization.

Adds fields to track organization verification status:
- verified: Whether the org has been manually verified by an admin
- verified_at: Timestamp of when verification occurred

Verified organizations receive higher rate limits.

Revision ID: add_org_verification
Revises: credit_grant_links_001
Create Date: 2026-02-12 19:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "add_org_verification"
down_revision = "credit_grant_links_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add verification fields to organization table."""
    op.add_column(
        "organization",
        sa.Column(
            "verified",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment="Whether org has been manually verified by admin",
        ),
    )
    op.add_column(
        "organization",
        sa.Column(
            "verified_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="When the org was verified",
        ),
    )

    # Add index for efficient lookup of verified orgs
    op.create_index(
        "ix_organization_verified",
        "organization",
        ["verified"],
        unique=False,
    )


def downgrade() -> None:
    """Remove verification fields."""
    op.drop_index("ix_organization_verified", table_name="organization")
    op.drop_column("organization", "verified_at")
    op.drop_column("organization", "verified")
