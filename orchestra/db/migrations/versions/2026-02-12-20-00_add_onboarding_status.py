"""Add onboarding_status table.

Tracks user onboarding progress with flexible step data storage.
The table is intentionally freeform in the database to allow
for easy modifications as onboarding flow evolves. The API schema
enforces valid values.

current_step represents WHERE TO RESUME:
- account_setup: User needs to complete account setup (initial)
- billing_setup: Account done, needs billing
- completed: All done

step_data accumulates information from completed steps.

Revision ID: add_onboarding_status
Revises: add_org_verification
Create Date: 2026-02-12 20:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "add_onboarding_status"
down_revision = "add_org_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create onboarding_status table."""
    op.create_table(
        "onboarding_status",
        sa.Column(
            "id",
            sa.String(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()::text"),
        ),
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("auth_user.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column(
            "current_step",
            sa.String(50),
            nullable=False,
            comment="Next step to resume at (freeform in DB, enforced by API)",
        ),
        sa.Column(
            "step_data",
            JSONB,
            nullable=False,
            server_default="{}",
            comment="Accumulated data from completed steps (freeform JSON)",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop onboarding_status table."""
    op.drop_table("onboarding_status")
