"""Let a credit grant link have no expiry.

Revision ID: grant_link_optional_expiry
Revises: founder_interview_thread
Create Date: 2026-08-26 00:00:00.000000

A link that dies before it is claimed takes the credit with it, and the
person it was minted for is told there is nothing there. Expiry that starts
before activation only punishes someone for opening the email late. NULL now
means the link keeps working until it is claimed or revoked.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "grant_link_optional_expiry"
down_revision = "founder_interview_thread"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "one_time_credit_grant_link",
        "expires_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        nullable=True,
    )


def downgrade() -> None:
    # Links minted without an expiry have no honest timestamp to restore, so
    # give them one far enough out that the downgrade does not silently void
    # credits somebody is still holding.
    op.execute(
        "UPDATE one_time_credit_grant_link "
        "SET expires_at = now() + interval '10 years' "
        "WHERE expires_at IS NULL",
    )
    op.alter_column(
        "one_time_credit_grant_link",
        "expires_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        nullable=False,
    )
