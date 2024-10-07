"""archive query -> query_old

Revision ID: 4251682afa02
Revises: 69fda243ec6f
Create Date: 2024-09-03 12:27:15.827936

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "4251682afa02"
down_revision = "69fda243ec6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("query", "query_old")


def downgrade() -> None:
    op.rename_table("query_old", "query")
