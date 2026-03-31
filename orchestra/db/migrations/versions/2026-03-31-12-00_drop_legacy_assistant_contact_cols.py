"""Drop legacy contact columns from assistants table.

The ``assistant_contacts`` table (added in ``add_assistant_contacts``) is now
the single source of truth for contact details.  These legacy columns on the
``assistants`` table are no longer read or written by application code and
can be safely removed.

Dropped columns: phone, email, user_phone, user_whatsapp_number,
assistant_whatsapp_number, phone_country.

Revision ID: drop_legacy_contact_cols
Revises: whatsapp_pool_routing
Create Date: 2026-03-31 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "drop_legacy_contact_cols"
down_revision = "whatsapp_pool_routing"
branch_labels = None
depends_on = None

_COLUMNS = [
    "phone",
    "email",
    "user_phone",
    "user_whatsapp_number",
    "assistant_whatsapp_number",
    "phone_country",
]


def upgrade() -> None:
    for col in _COLUMNS:
        op.drop_column("assistants", col)


def downgrade() -> None:
    for col in _COLUMNS:
        op.add_column(
            "assistants",
            sa.Column(col, sa.String(), nullable=True),
        )
