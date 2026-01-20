"""Drop legacy log storage tables now that the current schema is in use.

Revision ID: drop_eav_tables
Revises: drop_legacy_tables
Create Date: 2026-01-16 12:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "drop_eav_tables"
down_revision = "drop_legacy_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Drop legacy log storage tables in dependency order.
    """
    # Association tables first
    op.drop_table("log_event_json_log_history")
    op.drop_table("log_event_json_log")
    op.drop_table("log_event_log")
    op.drop_table("log_event_derived_log")

    # History/detail tables
    op.drop_table("json_log_history")
    op.drop_table("json_log")

    # Legacy storage tables
    op.drop_table("log_version")
    op.drop_table("derived_log")
    op.drop_table("log")
    op.drop_table("param_version")


def downgrade() -> None:
    """
    No downgrade implemented - legacy tables are permanently removed.
    """
