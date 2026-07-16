"""Merge provider-trigger and assistant-region migration branches.

Revision ID: merge_trigger_regional
Revises: assistant_regional_migration, provider_trigger_passthrough
"""

from __future__ import annotations

revision = "merge_trigger_regional"
down_revision = (
    "assistant_regional_migration",
    "provider_trigger_passthrough",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join both schema branches without changing database objects."""


def downgrade() -> None:
    """Split the migration graph without changing database objects."""
