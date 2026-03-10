"""Drop assistant name uniqueness constraints.

Context paths now use assistant IDs (agent_id) and user IDs instead of
assistant names, so the unique-name-per-context constraints are no longer
needed.

Drops:
- ``uq_user_assistant_name`` – partial unique index on
  (user_id, first_name, surname) WHERE organization_id IS NULL
- ``uq_org_assistant_name`` – unique constraint on
  (organization_id, first_name, surname)

Revision ID: drop_asst_name_uq
Revises: drop_assistant_secrets
Create Date: 2026-03-09 12:00:00.000000
"""

from alembic import op

revision = "drop_asst_name_uq"
down_revision = "drop_assistant_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the partial unique index for personal assistants
    op.drop_index(
        "uq_user_assistant_name",
        table_name="assistants",
    )

    # Drop the unique constraint for org assistants
    op.drop_constraint(
        "uq_org_assistant_name",
        "assistants",
        type_="unique",
    )


def downgrade() -> None:
    # Re-create the partial unique index for personal assistants
    op.create_index(
        "uq_user_assistant_name",
        "assistants",
        ["user_id", "first_name", "surname"],
        unique=True,
        postgresql_where="organization_id IS NULL",
    )

    # Re-create the unique constraint for org assistants
    op.create_unique_constraint(
        "uq_org_assistant_name",
        "assistants",
        ["organization_id", "first_name", "surname"],
    )
