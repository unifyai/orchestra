"""Enforce one connection per owner+app_slug+account, closing a start_connection race.

``start_connection`` (see ``orchestra/web/api/integrations/operations.py``) now
looks up and reuses an existing row for the same owner+app_slug+account before
inserting, but that check-then-act is not race-safe under two truly concurrent
first-time connect calls -- both can see no existing row and both insert. This
adds the DB-level invariant the application logic already assumes.

Nullable columns (``org_id``, ``team_id``, ``user_id``, ``assistant_id``,
``external_account_label``) are folded through ``COALESCE`` so two rows that
are both NULL in the same column collide instead of Postgres treating NULLs as
distinct -- this is deliberately case-insensitive on the account label (see
``operations.py``'s ``_label_key`` helper) since callers may resend a
differently-cased rendering of the same account.

``CREATE UNIQUE INDEX CONCURRENTLY`` cannot run inside a transaction, so this
runs in an autocommit block. If duplicate rows already exist for the same key
(e.g. accumulated before this fix shipped), this step fails loudly rather than
silently deleting or merging any row -- resolving those duplicates is a data
decision this migration deliberately does not make.

Revision ID: integration_conn_owner_app_uq
Revises: founder_interview_ask
Create Date: 2026-07-30 00:00:01.000000
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "integration_conn_owner_app_uq"
down_revision = "founder_interview_ask"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_integration_connections_owner_app_account"
_TABLE = "integration_connections"
_COLS = (
    '"owner_scope", '
    "COALESCE(org_id, -1), "
    "COALESCE(team_id, -1), "
    "COALESCE(user_id, ''), "
    "COALESCE(assistant_id, -1), "
    '"canonical_app_slug", '
    "lower(COALESCE(external_account_label, ''))"
)


def upgrade() -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        bind.execute(
            text(
                f'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "{_INDEX_NAME}" '
                f'ON "{_TABLE}" ({_COLS})',
            ),
        )


def downgrade() -> None:
    op.execute(f'DROP INDEX IF EXISTS "{_INDEX_NAME}"')
