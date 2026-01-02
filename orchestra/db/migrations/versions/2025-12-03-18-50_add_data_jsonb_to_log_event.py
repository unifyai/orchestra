"""Add data JSONB column to log_event table

Revision ID: a2299418c4c9
Revises: add_ref_keys_derived_tpl
Create Date: 2025-12-03 18:50:32.363751

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a2299418c4c9"
down_revision = "add_ref_keys_derived_tpl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add JSONB data column to log_event for storing log entries
    op.add_column(
        "log_event",
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )

    # Create GIN index for JSON field queries
    op.create_index(
        "idx_log_event_data",
        "log_event",
        ["data"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("idx_log_event_data", table_name="log_event", postgresql_using="gin")
    op.drop_column("log_event", "data")
