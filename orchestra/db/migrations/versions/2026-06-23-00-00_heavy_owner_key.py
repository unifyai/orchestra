"""Denormalize owning scope (owner_key) onto the heavy kernel tables.

Adds a nullable ``owner_key`` to ``log_event`` / ``log_event_context`` /
``embedding`` and backfills it from each log's *owning* context (the
assistant/team one, not the aggregation views it is referenced into). This is
the single-column LIST sub-partition key the shared ``Assistants`` project will
be divided by, enabling per-assistant / per-team O(1) deletion.

``owner_key`` stays nullable here: the write paths do not populate it yet (a
later increment), so it cannot be made NOT NULL / part of the PK until then.

Idempotent across fresh (columns built by create_all) and existing DBs; the
backfill only touches NULL rows.

Revision ID: heavy_owner_key
Revises: context_ownership_scope
Create Date: 2026-06-23 00:00:00.000000
"""

from alembic import op

from orchestra.db.scope import backfill_heavy_owner_keys

revision = "heavy_owner_key"
down_revision = "context_ownership_scope"
branch_labels = None
depends_on = None

_TABLES = ("log_event", "log_event_context", "embedding")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS owner_key varchar")
    backfill_heavy_owner_keys(op.get_bind())


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS owner_key")
