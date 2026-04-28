"""Drop platform-issued email cost rows from contact_type_costs.

Platform-issued ``@unify.ai`` mailboxes were retired alongside the wider
``provisioned_by='platform'`` email feature: the create endpoint returns
HTTP 410 for that combination, the backend code that called
Communication's ``gmail/create`` and ``outlook/create`` has been deleted,
and every existing platform mailbox in staging and production has been
torn down by ``orchestra.workers.teardown_platform_mailboxes`` (rows
soft-deleted, Workspace / MS365 users removed).

With nothing left to bill, the seeded cost rows in ``contact_type_costs``
for ``contact_type='email'`` are dead weight. Worse, they keep showing up
in the admin billing UI and the monthly levy still does cost lookups for
them. Deleting them here removes the last surface that says "the platform
charges for assistant email".

Email contacts are now BYOD-only (``provisioned_by='user'``) and BYOD
contacts are explicitly excluded from billing — see
``assistant_contact_levy.py`` ``provisioned_by == 'platform'`` filter.
The CHECK constraint on ``contact_type`` still allows ``email`` because
BYOD email rows continue to live in ``assistant_contacts``; only the
**pricing** rows are removed.

Revision ID: drop_platform_email_cost_rows
Revises: add_assistant_inactivity_columns
Create Date: 2026-04-28 17:30:00.000000
"""

from alembic import op

revision = "drop_platform_email_cost_rows"
down_revision = "add_assistant_inactivity_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM contact_type_costs
        WHERE contact_type = 'email'
        """,
    )


def downgrade() -> None:
    # Re-seed the two historical email pricing rows so a downgrade lands on
    # the same state as before this migration. ``ON CONFLICT DO NOTHING`` is
    # used in case a hand-edited row already exists in the target database.
    # Pricing values mirror the original seeds:
    #   - Google Workspace: 14.00 / 5.00 (2026-03-06 add_assistant_contacts)
    #   - Microsoft 365:    25.00 / 5.00 (2026-04-17 ms365_business_premium_pricing,
    #                                     which raised it from 12.50 set in
    #                                     2026-04-13 ms365_email_provider).
    op.execute(
        """
        INSERT INTO contact_type_costs
            (contact_type, provider, country_code, monthly_cost, one_time_cost)
        VALUES
            ('email', 'google_workspace', NULL, 14.00, 5.00),
            ('email', 'microsoft_365',    NULL, 25.00, 5.00)
        ON CONFLICT (contact_type, provider, country_code) DO NOTHING
        """,
    )
