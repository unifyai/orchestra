"""Add rate_limit_counter table for API rate limiting.

This table tracks request counts in 5-minute time buckets for rate limiting.
It replaces the previous approval-based gating with a more flexible
rate limiting system.

Features:
- 5-minute time buckets for precision while keeping table small
- Category-based limits with optional endpoint-specific overrides
- Supports both user-level and organization-level limits
- Rolling 24-hour window for limit calculation

Revision ID: rate_limit_counter_001
Revises: prospect_fields_001
Create Date: 2026-02-12 16:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "rate_limit_counter_001"
down_revision = "prospect_fields_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the rate limit counter table
    op.create_table(
        "rate_limit_counter",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Who made the request
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("auth_user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            comment="User who made the request",
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
            comment="Organization context (null for personal workspace)",
        ),
        # What endpoint/category
        sa.Column(
            "endpoint_category",
            sa.String(50),
            nullable=False,
            comment="Rate limit category: 'hiring', 'media', 'crud', 'voice'",
        ),
        sa.Column(
            "endpoint_path",
            sa.String(200),
            nullable=True,
            comment="Specific endpoint path for per-endpoint overrides (null for category-level)",
        ),
        # When (5-minute buckets)
        sa.Column(
            "time_bucket",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            comment="Start of the 5-minute time bucket",
        ),
        # Request count
        sa.Column(
            "request_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Number of requests in this bucket",
        ),
    )

    # Unique constraint for upsert operations
    op.create_unique_constraint(
        "uq_rate_limit_counter",
        "rate_limit_counter",
        ["user_id", "endpoint_category", "endpoint_path", "time_bucket"],
    )

    # Index for user + category lookups (most common query)
    op.create_index(
        "ix_rate_limit_counter_user_category",
        "rate_limit_counter",
        ["user_id", "endpoint_category", "time_bucket"],
    )

    # Index for organization-level lookups (shared org limits)
    op.create_index(
        "ix_rate_limit_counter_org_category",
        "rate_limit_counter",
        ["organization_id", "endpoint_category", "time_bucket"],
    )

    # Index for endpoint-specific lookups
    op.create_index(
        "ix_rate_limit_counter_endpoint",
        "rate_limit_counter",
        ["user_id", "endpoint_path", "time_bucket"],
    )

    # Index for cleanup queries (delete old buckets)
    op.create_index(
        "ix_rate_limit_counter_time_bucket",
        "rate_limit_counter",
        ["time_bucket"],
    )

    # Check constraint for valid categories (assistant API specific)
    op.create_check_constraint(
        "ck_rate_limit_counter_category",
        "rate_limit_counter",
        "endpoint_category IN ('assistant_hiring', 'assistant_media', 'assistant_crud', 'assistant_voice')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_rate_limit_counter_category",
        "rate_limit_counter",
        type_="check",
    )
    op.drop_index("ix_rate_limit_counter_time_bucket")
    op.drop_index("ix_rate_limit_counter_endpoint")
    op.drop_index("ix_rate_limit_counter_org_category")
    op.drop_index("ix_rate_limit_counter_user_category")
    op.drop_constraint("uq_rate_limit_counter", "rate_limit_counter", type_="unique")
    op.drop_table("rate_limit_counter")
