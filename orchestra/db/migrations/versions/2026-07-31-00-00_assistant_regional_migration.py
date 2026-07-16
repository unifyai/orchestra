"""Persist assistant regional migration state and immutable routing.

Revision ID: assistant_regional_migration
Revises: assistant_external_ip_rotations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "assistant_regional_migration"
down_revision = "assistant_external_ip_rotations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assistant_external_ips",
        sa.Column("pool_location", sa.String(), nullable=True),
    )
    op.add_column(
        "assistant_external_ips",
        sa.Column("desired_pool_location", sa.String(), nullable=True),
    )
    for column in (
        sa.Column("pool_location", sa.String(), nullable=True),
        sa.Column("region", sa.String(), nullable=True),
        sa.Column("binding_zone", sa.String(), nullable=True),
    ):
        op.add_column("assistant_external_ip_rotations", column)
    op.create_table(
        "assistant_external_ip_regional_migrations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("external_ip_id", sa.BigInteger(), nullable=False),
        sa.Column("assistant_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="requested"),
        sa.Column("source_pool_location", sa.String(), nullable=True),
        sa.Column("source_region", sa.String(), nullable=True),
        sa.Column("desired_pool_location", sa.String(), nullable=True),
        sa.Column("requested_timezone", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column(
            "requested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["external_ip_id"],
            ["assistant_external_ips.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["assistants.agent_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_assistant_external_ip_regional_migrations_external_ip_id",
        "assistant_external_ip_regional_migrations",
        ["external_ip_id"],
    )
    op.create_index(
        "ix_assistant_external_ip_regional_migrations_assistant_id",
        "assistant_external_ip_regional_migrations",
        ["assistant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_external_ip_regional_migrations_assistant_id",
        table_name="assistant_external_ip_regional_migrations",
    )
    op.drop_index(
        "ix_assistant_external_ip_regional_migrations_external_ip_id",
        table_name="assistant_external_ip_regional_migrations",
    )
    op.drop_table("assistant_external_ip_regional_migrations")
    for column in ("binding_zone", "region", "pool_location"):
        op.drop_column("assistant_external_ip_rotations", column)
    op.drop_column("assistant_external_ips", "desired_pool_location")
    op.drop_column("assistant_external_ips", "pool_location")
