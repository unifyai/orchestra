"""Fix assistant name unique constraint to be scoped per context.

The old ``uq_user_assistant_name`` constraint on (user_id, first_name, surname)
prevented a user from having assistants with the same name across personal and
org contexts.  Replace it with a partial unique index that only applies to
personal assistants (organization_id IS NULL).

Revision ID: fix_asst_name_uq
Revises: add_token_jti
Create Date: 2026-03-04 12:00:00.000000
"""

from alembic import op

revision = "fix_asst_name_uq"
down_revision = "add_token_jti"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the old (too-broad) unique constraint
    op.drop_constraint(
        "uq_user_assistant_name",
        "assistants",
        type_="unique",
    )

    # Create a partial unique index scoped to personal assistants only
    op.create_index(
        "uq_user_assistant_name",
        "assistants",
        ["user_id", "first_name", "surname"],
        unique=True,
        postgresql_where="organization_id IS NULL",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_user_assistant_name",
        table_name="assistants",
    )

    op.create_unique_constraint(
        "uq_user_assistant_name",
        "assistants",
        ["user_id", "first_name", "surname"],
    )
