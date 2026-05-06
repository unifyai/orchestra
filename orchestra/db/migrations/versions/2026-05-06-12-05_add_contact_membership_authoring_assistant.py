"""Add contact membership authoring assistant.

Revision ID: contact_cm_authoring
Revises: seed_personal_cm
Create Date: 2026-05-06 12:05:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "contact_cm_authoring"
down_revision = "seed_personal_cm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contact_memberships",
        sa.Column("authoring_assistant_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_contact_memberships_authoring_assistant_id",
        "contact_memberships",
        "assistants",
        ["authoring_assistant_id"],
        ["agent_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_contact_memberships_authoring_assistant_id",
        "contact_memberships",
        ["authoring_assistant_id"],
        unique=False,
        postgresql_where=sa.text("authoring_assistant_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_contact_memberships_authoring_assistant_id",
        table_name="contact_memberships",
        postgresql_where=sa.text("authoring_assistant_id IS NOT NULL"),
    )
    op.drop_constraint(
        "fk_contact_memberships_authoring_assistant_id",
        "contact_memberships",
        type_="foreignkey",
    )
    op.drop_column("contact_memberships", "authoring_assistant_id")
