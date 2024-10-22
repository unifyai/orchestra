"""add unique constraint for logs

Revision ID: d0ebdc45de67
Revises: bb2e75a19b27
Create Date: 2024-10-18 15:49:05.324734

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "d0ebdc45de67"
down_revision = "bb2e75a19b27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_log_event_id_key", "log", ["log_event_id", "key"])


def downgrade() -> None:
    op.drop_constraint("uq_log_event_id_key", "log", type_="unique")
