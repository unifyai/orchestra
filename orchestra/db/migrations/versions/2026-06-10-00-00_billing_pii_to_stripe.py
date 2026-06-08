"""Stop storing billing PII locally; Stripe becomes the source of truth.

Adds the non-PII ``billing_account.is_business`` flag (backfilled from the
existing tax-id presence/verification) and then drops the mirrored profile
PII columns (``name``, ``billing_email``, ``billing_address``, ``tax_id``,
``tax_id_type``, ``tax_id_verification_status``).

After this migration the editable billing profile is read from / written to
the Stripe Customer on demand; only the derived ``is_business`` and
``billing_setup_complete`` flags remain locally. The drop is destructive —
the downgrade re-creates the columns but cannot restore the values (they
live in Stripe).

Independent of the self-serve subscription model — kept as its own
migration so it can be reasoned about / reverted on its own — but chained
after the ``tier`` drop to keep a single linear head.

Revision ID: billing_pii_to_stripe
Revises: drop_billing_account_tier
Create Date: 2026-06-10 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "billing_pii_to_stripe"
down_revision = "drop_billing_account_tier"
branch_labels = None
depends_on = None

_PII_COLUMNS = (
    "name",
    "billing_email",
    "billing_address",
    "tax_id",
    "tax_id_type",
    "tax_id_verification_status",
)


def upgrade() -> None:
    op.add_column(
        "billing_account",
        sa.Column(
            "is_business",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )

    # Backfill the derived flag from the soon-to-be-dropped tax-id columns.
    # A tax ID counts as a business unless Stripe explicitly marked it
    # ``unverified`` (mirrors the previous live derivation in
    # ``resolve_is_business``).
    op.execute(
        """
        UPDATE billing_account
        SET is_business = TRUE
        WHERE tax_id IS NOT NULL
          AND tax_id <> ''
          AND (
            tax_id_verification_status IS NULL
            OR tax_id_verification_status <> 'unverified'
          )
        """,
    )

    for column in _PII_COLUMNS:
        op.drop_column("billing_account", column)


def downgrade() -> None:
    # Re-create the columns (values are not recoverable — they live in
    # Stripe now).
    op.add_column(
        "billing_account",
        sa.Column("billing_email", sa.String(), nullable=True),
    )
    op.add_column(
        "billing_account",
        sa.Column("name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "billing_account",
        sa.Column("tax_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "billing_account",
        sa.Column("tax_id_type", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "billing_account",
        sa.Column("tax_id_verification_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "billing_account",
        sa.Column("billing_address", JSONB(), nullable=True),
    )
    op.drop_column("billing_account", "is_business")
