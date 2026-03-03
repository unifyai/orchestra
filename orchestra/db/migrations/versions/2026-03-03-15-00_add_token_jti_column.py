"""Add token_jti column to email_verification for single-use token enforcement.

Revision ID: add_token_jti
Revises: add_auth_rate_limit
Create Date: 2026-03-03 15:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_token_jti"
down_revision = "add_auth_rate_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "email_verification",
        sa.Column("token_jti", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("email_verification", "token_jti")
