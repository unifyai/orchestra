"""Backfill typed Knowledge ledger ``knowledge_id`` auto-count schema.

Revision ID: knowledge_ledger_auto_count
Revises: external_write_intent
Create Date: 2026-08-11 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

from orchestra.db.knowledge_ledger_schema import backfill_knowledge_ledger_schema

revision = "knowledge_ledger_auto_count"
down_revision = "external_write_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    backfill_knowledge_ledger_schema(op.get_bind())


def downgrade() -> None:
    # Additive fleet heal; cannot distinguish migrated rows from natively typed ones.
    pass
