"""Drop retired integration catalog projection tables.

Revision ID: drop_legacy_integration_catalog
Revises: partition_kernel_by_project
Create Date: 2026-06-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "drop_legacy_integration_catalog"
down_revision = "partition_kernel_by_project"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF EXISTS so a re-run after a partially-applied attempt is idempotent.
    # provider_tool_catalog references dynamic_provider_apps, so drop it first.
    # The migrator's generous lock_timeout (see env.py) lets these DROPs queue
    # for ACCESS EXCLUSIVE behind the still-running app's ACCESS SHARE locks.
    op.execute("DROP TABLE IF EXISTS provider_tool_catalog")
    op.execute("DROP TABLE IF EXISTS dynamic_provider_apps")


def downgrade() -> None:
    # The catalog source of truth is Builtins logs. This migration intentionally
    # does not recreate the retired compatibility projection tables.
    pass
