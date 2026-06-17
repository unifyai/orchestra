"""Add ownership scope to context and backfill from the naming convention.

First schema step of the scope-based refactor: give every ``context`` a
first-class owner (``owner_scope`` + ``owner_id``) instead of leaving ownership
implicit in the Unity context-name convention. Later increments denormalize
this owner onto the heavy tables and sub-partition the shared ``Assistants``
project by it so an assistant or team can be deleted as an O(1) partition drop.

Idempotent across both starting states:

* Fresh DB -- ``0001_core_initial``'s ``meta.create_all`` already built the
  columns/index from the current model, so the ``IF NOT EXISTS`` DDL is a no-op
  and the (empty) context table needs no backfill.
* Existing DB -- adds the columns/index and backfills ``owner_scope`` /
  ``owner_id`` for every existing context from its name.

Revision ID: context_ownership_scope
Revises: partition_kernel_by_project
Create Date: 2026-06-22 00:00:00.000000
"""

from alembic import op

from orchestra.db.scope import backfill_context_owners

revision = "context_ownership_scope"
down_revision = "partition_kernel_by_project"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE context ADD COLUMN IF NOT EXISTS owner_scope varchar")
    op.execute("ALTER TABLE context ADD COLUMN IF NOT EXISTS owner_id integer")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_context_owner ON context "
        "(project_id, owner_scope, owner_id) WHERE owner_id IS NOT NULL",
    )
    backfill_context_owners(op.get_bind())


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_context_owner")
    op.execute("ALTER TABLE context DROP COLUMN IF EXISTS owner_id")
    op.execute("ALTER TABLE context DROP COLUMN IF EXISTS owner_scope")
