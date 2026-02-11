"""Add prospect detail fields to demo_assistant_meta table.

This migration adds optional fields to store prospect details for demo assistants:
- prospect_first_name: Prospect's first name
- prospect_surname: Prospect's surname
- prospect_email: Prospect's email address
- prospect_phone: Prospect's phone number (E.164 format)

These fields are populated optionally at demo assistant creation time.
Unity fetches them from the meta endpoint to pre-populate the boss contact.

Revision ID: prospect_fields_001
Revises: demo_assistant_meta_001
Create Date: 2026-02-11 14:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "prospect_fields_001"
down_revision = "demo_assistant_meta_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add prospect detail columns to demo_assistant_meta
    op.add_column(
        "demo_assistant_meta",
        sa.Column(
            "prospect_first_name",
            sa.String(),
            nullable=True,
            comment="Prospect's first name (optional, for pre-populating boss contact)",
        ),
    )
    op.add_column(
        "demo_assistant_meta",
        sa.Column(
            "prospect_surname",
            sa.String(),
            nullable=True,
            comment="Prospect's surname (optional, for pre-populating boss contact)",
        ),
    )
    op.add_column(
        "demo_assistant_meta",
        sa.Column(
            "prospect_email",
            sa.String(),
            nullable=True,
            comment="Prospect's email address (optional, for pre-populating boss contact)",
        ),
    )
    op.add_column(
        "demo_assistant_meta",
        sa.Column(
            "prospect_phone",
            sa.String(),
            nullable=True,
            comment="Prospect's phone number in E.164 format (optional, for pre-populating boss contact)",
        ),
    )


def downgrade() -> None:
    op.drop_column("demo_assistant_meta", "prospect_phone")
    op.drop_column("demo_assistant_meta", "prospect_email")
    op.drop_column("demo_assistant_meta", "prospect_surname")
    op.drop_column("demo_assistant_meta", "prospect_first_name")
