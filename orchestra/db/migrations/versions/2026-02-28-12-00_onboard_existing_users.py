"""Migrate onboarding to OnboardingStatus table, drop User.onboarded column

All existing users who haven't completed onboarding get their
onboarding_status.current_step set to 'completed' so they are not
forced through the new onboarding flow.

Then the redundant 'onboarded' boolean column is dropped from the
'user' table — OnboardingStatus is now the single source of truth.

Revision ID: onboard_existing_users
Revises: add_org_mfa_enforcement
Create Date: 2026-02-28 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "onboard_existing_users"
down_revision = "add_org_mfa_enforcement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. For every existing user who does NOT yet have an onboarding_status
    #    row, create one with current_step='completed'.  This covers users
    #    created before the onboarding_status table existed.
    op.execute(
        """
        INSERT INTO onboarding_status (id, user_id, current_step, step_data, created_at)
        SELECT
            gen_random_uuid()::text,
            u.id,
            'completed',
            '{}',
            NOW()
        FROM "user" u
        LEFT JOIN onboarding_status os ON os.user_id = u.id
        WHERE os.id IS NULL
        """,
    )

    # 2. Mark any existing incomplete onboarding_status rows as completed.
    op.execute(
        "UPDATE onboarding_status SET current_step = 'completed' "
        "WHERE current_step != 'completed'",
    )

    # 3. Drop the redundant column.
    op.drop_column("user", "onboarded")


def downgrade() -> None:
    # Re-add the column with a default of false.
    op.add_column(
        "user",
        sa.Column(
            "onboarded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Backfill: users whose onboarding_status is 'completed' → onboarded=true
    op.execute(
        """
        UPDATE "user" u
        SET onboarded = true
        FROM onboarding_status os
        WHERE os.user_id = u.id AND os.current_step = 'completed'
        """,
    )
