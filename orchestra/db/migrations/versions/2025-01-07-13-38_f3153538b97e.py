"""empty message

Revision ID: f3153538b97e
Revises: 7e634b76aeaf
Create Date: 2025-01-07 13:38:37.541595

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "f3153538b97e"
down_revision = "e81f65ef8f53"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename columns in place
    op.alter_column(
        "latest_benchmark",
        "time_to_first_token",
        new_column_name="ttft",
    )
    op.alter_column(
        "latest_benchmark",
        "inter_token_latency",
        new_column_name="itl",
    )


def downgrade() -> None:
    # Revert the column names
    op.alter_column(
        "latest_benchmark",
        "ttft",
        new_column_name="time_to_first_token",
    )
    op.alter_column(
        "latest_benchmark",
        "itl",
        new_column_name="inter_token_latency",
    )
