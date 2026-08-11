"""Drop legacy Tasks ``instance_id`` auto-count schema from pre-collapse contexts.

Revision ID: drop_legacy_tasks_instance_id
Revises: comp_recharges_are_promo
Create Date: 2026-08-20 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

from orchestra.db.legacy_tasks_schema import drop_legacy_tasks_instance_id_schema

revision = "drop_legacy_tasks_instance_id"
down_revision = "comp_recharges_are_promo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    drop_legacy_tasks_instance_id_schema(op.get_bind())


def downgrade() -> None:
    # Converges legacy contexts onto the current declared schema; the legacy
    # registration is not meaningfully reconstructible (and never desirable).
    pass
