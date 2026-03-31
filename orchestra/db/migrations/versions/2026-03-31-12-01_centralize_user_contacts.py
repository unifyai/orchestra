"""Centralize user contact info on the User table.

Backfills ``User.phone_number`` and ``User.whatsapp_number`` from
``AssistantContact.user_value`` for phone/whatsapp contacts where the
user doesn't already have the corresponding number set, then drops the
now-redundant ``user_value`` column from ``assistant_contacts``.

After this migration, user contact info (phone, whatsapp) is read
exclusively from the ``user`` table and no longer stored per-contact.

Revision ID: centralize_user_contacts
Revises: drop_legacy_contact_cols
Create Date: 2026-03-31 12:01:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "centralize_user_contacts"
down_revision = "drop_legacy_contact_cols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Backfill User.phone_number from phone-type contacts
    op.execute(
        """
        UPDATE "user" u
        SET phone_number = sub.user_value
        FROM (
            SELECT DISTINCT ON (a.user_id)
                   a.user_id,
                   ac.user_value
            FROM assistant_contacts ac
            JOIN assistants a ON a.agent_id = ac.assistant_id
            WHERE ac.contact_type = 'phone'
              AND ac.user_value IS NOT NULL
              AND ac.status != 'deleted'
            ORDER BY a.user_id, ac.updated_at DESC
        ) sub
        WHERE u.id = sub.user_id
          AND u.phone_number IS NULL
        """
    )

    # Backfill User.whatsapp_number from whatsapp-type contacts
    op.execute(
        """
        UPDATE "user" u
        SET whatsapp_number = sub.user_value
        FROM (
            SELECT DISTINCT ON (a.user_id)
                   a.user_id,
                   ac.user_value
            FROM assistant_contacts ac
            JOIN assistants a ON a.agent_id = ac.assistant_id
            WHERE ac.contact_type = 'whatsapp'
              AND ac.user_value IS NOT NULL
              AND ac.status != 'deleted'
            ORDER BY a.user_id, ac.updated_at DESC
        ) sub
        WHERE u.id = sub.user_id
          AND u.whatsapp_number IS NULL
        """
    )

    op.drop_column("assistant_contacts", "user_value")


def downgrade() -> None:
    op.add_column(
        "assistant_contacts",
        sa.Column("user_value", sa.String(), nullable=True),
    )
