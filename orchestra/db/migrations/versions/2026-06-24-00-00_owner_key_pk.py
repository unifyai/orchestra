"""Promote owner_key to NOT NULL and into the PK + unique constraints.

Once every heavy-table write path populates ``owner_key`` (the previous
increment), it can become a real key column. This migration sweeps any rows
written during the transition window, makes ``owner_key`` NOT NULL, and adds it
to the primary key and every UNIQUE constraint of ``log_event`` /
``log_event_context`` / ``embedding``.

That is the precondition for sub-partitioning the shared ``Assistants``
project's partition by owner (every partition-key column must appear in each
unique constraint). The actual per-project sub-partitioning is an operational
step (``sub_partition_project_by_owner``), not part of this schema migration.

Idempotent: on a fresh DB ``create_all`` already builds owner_key into the PK,
so each table is skipped; on an existing DB the keys are rebuilt in place.

Revision ID: owner_key_pk
Revises: heavy_owner_key
Create Date: 2026-06-24 00:00:00.000000
"""

from alembic import op

from orchestra.db.partitioning import (
    OWNER_SUB_TABLES,
    add_owner_key_to_keys,
    owner_key_in_pk,
    remove_owner_key_from_keys,
)
from orchestra.db.scope import backfill_heavy_owner_keys

revision = "owner_key_pk"
down_revision = "heavy_owner_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Catch any rows written between heavy_owner_key's backfill and the deploy
    # of the owner_key write paths.
    backfill_heavy_owner_keys(bind)
    for table in OWNER_SUB_TABLES:
        if owner_key_in_pk(bind, table):
            continue
        add_owner_key_to_keys(bind, table)


def downgrade() -> None:
    bind = op.get_bind()
    for table in OWNER_SUB_TABLES:
        if owner_key_in_pk(bind, table):
            remove_owner_key_from_keys(bind, table)
