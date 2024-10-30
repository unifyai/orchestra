"""change log version to be integer

Revision ID: 24929ee65f80
Revises: ac41d77761f5
Create Date: 2024-10-30 14:05:53.632322

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "24929ee65f80"
down_revision = "ac41d77761f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("log", "version")
    op.add_column("log", sa.Column("version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("log", "version")
    op.add_column("log", sa.Column("version", sa.String(), nullable=True))
