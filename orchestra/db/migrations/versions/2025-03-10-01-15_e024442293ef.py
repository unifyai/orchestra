"""Update constraints on field_type and log_event_context tables.

Revision ID: e024442293ef
Revises: e8f76de792cd
Create Date: 2025-03-10 01:15:22.781047

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e024442293ef"
down_revision = "e8f76de792cd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Update the unique constraint on field_type
    op.drop_constraint("uq_project_context_field_name", "field_type", type_="unique")
    op.create_unique_constraint(
        "uq_project_field_name_context_id",
        "field_type",
        ["project_id", "field_name", "context_id"],
    )

    # Update the foreign key constraint on log_event_context for log_event_id
    op.drop_constraint(
        "log_event_context_log_event_id_fkey",
        "log_event_context",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "log_event_context_log_event_id_fkey",
        "log_event_context",
        "log_event",
        ["log_event_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Update the foreign key constraint on log_event_context for context_id
    op.drop_constraint(
        "log_event_context_context_id_fkey",
        "log_event_context",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "log_event_context_context_id_fkey",
        "log_event_context",
        "context",
        ["context_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # Revert the foreign key changes on log_event_context
    op.drop_constraint(
        "log_event_context_context_id_fkey",
        "log_event_context",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "log_event_context_context_id_fkey",
        "log_event_context",
        "context",
        ["context_id"],
        ["id"],
        # Downgrade: remove ondelete cascade (defaults to RESTRICT)
    )
    op.drop_constraint(
        "log_event_context_log_event_id_fkey",
        "log_event_context",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "log_event_context_log_event_id_fkey",
        "log_event_context",
        "log_event",
        ["log_event_id"],
        ["id"],
        # Downgrade: remove ondelete cascade
    )

    # Revert the unique constraint change on field_type
    op.drop_constraint("uq_project_field_name_context_id", "field_type", type_="unique")
    op.create_unique_constraint(
        "uq_project_context_field_name",
        "field_type",
        ["project_id", "context_id", "field_name"],
    )
