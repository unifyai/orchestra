"""Tighten shared space descriptions.

Revision ID: tighten_space_description
Revises: add_contact_memberships
Create Date: 2026-05-02 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "tighten_space_description"
down_revision = "add_contact_memberships"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE spaces
        SET description = (
            'Shared workspace named "' || name ||
            '" for collaboration, memory routing, and team context. Update this description with the space-specific domain and scope.'
        )
        WHERE description IS NULL
           OR length(description) NOT BETWEEN 20 AND 1000
        """,
    )
    op.alter_column(
        "spaces",
        "description",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_spaces_description_length",
        "spaces",
        "length(description) BETWEEN 20 AND 1000",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_spaces_description_length",
        "spaces",
        type_="check",
    )
    op.alter_column(
        "spaces",
        "description",
        existing_type=sa.Text(),
        nullable=True,
    )
