"""Add auth_rate_limit_entry table for IP-based auth rate limiting.

Tracks request counts per IP+identifier in 5-minute time buckets for
throttling authentication endpoints (login, MFA verify, registration, etc.).

Revision ID: add_auth_rate_limit
Revises: credit_grant_link_org_support
Create Date: 2026-03-03 14:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "add_auth_rate_limit"
down_revision = "credit_grant_link_org_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_limit_entry",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(500), nullable=False, index=True),
        sa.Column("endpoint_category", sa.String(50), nullable=False),
        sa.Column(
            "time_bucket",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.UniqueConstraint(
            "key",
            "endpoint_category",
            "time_bucket",
            name="uq_auth_rate_limit_entry",
        ),
    )
    op.create_index(
        "ix_auth_rate_limit_key_category",
        "auth_rate_limit_entry",
        ["key", "endpoint_category", "time_bucket"],
    )
    op.create_index(
        "ix_auth_rate_limit_time_bucket",
        "auth_rate_limit_entry",
        ["time_bucket"],
    )


def downgrade() -> None:
    op.drop_table("auth_rate_limit_entry")
