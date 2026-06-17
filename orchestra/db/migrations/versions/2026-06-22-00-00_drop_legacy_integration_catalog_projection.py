"""Drop retired integration catalog projection tables.

Revision ID: drop_legacy_integration_catalog_projection
Revises: partition_kernel_by_project
Create Date: 2026-06-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "drop_legacy_integration_catalog_projection"
down_revision = "partition_kernel_by_project"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("provider_tool_catalog")
    op.drop_table("dynamic_provider_apps")


def downgrade() -> None:
    # The catalog source of truth is Builtins logs. This migration intentionally
    # does not recreate the retired compatibility projection tables.
    pass
