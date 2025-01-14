"""updated value type to be JSONB for Log table.

Revision ID: e6ff41e900e1
Revises: d805d43c0bde
Create Date: 2025-01-10 11:03:43.893614

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "e6ff41e900e1"
down_revision = "d805d43c0bde"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "log",
        "value",
        existing_type=sa.VARCHAR(),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "log",
        "value",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=sa.VARCHAR(),
        existing_nullable=True,
    )
