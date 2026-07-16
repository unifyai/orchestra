"""Per-owner integration app preference for multi-account selection.

Revision ID: integration_app_preferences
Revises: provider_event_dispatch_adoption
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "integration_app_preferences"
down_revision = "provider_event_dispatch_adoption"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_app_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_scope", sa.String(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("assistant_id", sa.Integer(), nullable=True),
        sa.Column("canonical_app_slug", sa.String(), nullable=False),
        sa.Column(
            "usage_mode",
            sa.String(),
            nullable=False,
            server_default="primary",
        ),
        sa.Column(
            "pool_cursor",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "owner_scope",
            "org_id",
            "team_id",
            "user_id",
            "assistant_id",
            "canonical_app_slug",
            name="uq_integration_app_preference_owner_app",
        ),
    )
    op.create_index(
        "ix_integration_app_preferences_owner_scope",
        "integration_app_preferences",
        ["owner_scope"],
    )
    op.create_index(
        "ix_integration_app_preferences_canonical_app_slug",
        "integration_app_preferences",
        ["canonical_app_slug"],
    )
    op.create_index(
        "ix_integration_app_preferences_effective_owner",
        "integration_app_preferences",
        ["owner_scope", "org_id", "team_id", "user_id", "assistant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_integration_app_preferences_effective_owner",
        table_name="integration_app_preferences",
    )
    op.drop_index(
        "ix_integration_app_preferences_canonical_app_slug",
        table_name="integration_app_preferences",
    )
    op.drop_index(
        "ix_integration_app_preferences_owner_scope",
        table_name="integration_app_preferences",
    )
    op.drop_table("integration_app_preferences")
