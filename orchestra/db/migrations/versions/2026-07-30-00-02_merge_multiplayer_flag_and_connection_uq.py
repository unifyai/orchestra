"""Merge coordinator-multiplayer-flag and connection-uniqueness branches.

Revision ID: merge_multiplayer_conn_uq
Revises: coordinator_multiplayer_flag, integration_conn_owner_app_uq
"""

from __future__ import annotations

revision = "merge_multiplayer_conn_uq"
down_revision = (
    "coordinator_multiplayer_flag",
    "integration_conn_owner_app_uq",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join both schema branches without changing database objects."""


def downgrade() -> None:
    """Split the migration graph without changing database objects."""
