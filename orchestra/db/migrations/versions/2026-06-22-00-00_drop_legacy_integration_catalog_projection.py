"""Drop retired integration catalog projection tables.

Revision ID: drop_legacy_integration_catalog
Revises: partition_kernel_by_project
Create Date: 2026-06-22 00:00:00.000000
"""

from __future__ import annotations

import time

from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

revision = "drop_legacy_integration_catalog"
down_revision = "partition_kernel_by_project"
branch_labels = None
depends_on = None

# provider_tool_catalog references dynamic_provider_apps, so drop it first.
_RETIRED_TABLES = ("provider_tool_catalog", "dynamic_provider_apps")

_LOCK_NOT_AVAILABLE = "55P03"  # psycopg2 LockNotAvailable
_DROP_LOCK_TIMEOUT = "30s"
_ATTEMPTS = 6
_BACKOFF_SECONDS = 3.0


def _terminate_lockers(bind: Connection, table: str) -> None:
    """Terminate other sessions holding a lock on a retired table.

    The pre-deploy migrator runs while the previous app revision is still
    serving traffic; a persistent ACCESS SHARE lock (e.g. an idle-in-transaction
    connection) on these tables blocks the DROP's ACCESS EXCLUSIVE indefinitely,
    so even a generous lock_timeout fails. These projection tables are being
    retired and the new app revision no longer reads them, so cutting the old
    revision's lingering sessions on them during cutover is safe and lets the
    DROP through.
    """
    bind.execute(
        text(
            "SELECT pg_terminate_backend(l.pid) FROM pg_locks l "
            "JOIN pg_class c ON c.oid = l.relation "
            "WHERE c.relname = :t AND l.pid <> pg_backend_pid()",
        ),
        {"t": table},
    )


def _drop_retired(bind: Connection, table: str) -> None:
    bind.execute(text(f"SET lock_timeout = '{_DROP_LOCK_TIMEOUT}'"))
    last_exc: OperationalError | None = None
    for _ in range(_ATTEMPTS):
        savepoint = bind.begin_nested()
        try:
            _terminate_lockers(bind, table)
            bind.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
            savepoint.commit()
            return
        except OperationalError as exc:
            savepoint.rollback()
            if getattr(exc.orig, "pgcode", None) != _LOCK_NOT_AVAILABLE:
                raise
            last_exc = exc
            time.sleep(_BACKOFF_SECONDS)
    raise RuntimeError(
        f"Could not acquire ACCESS EXCLUSIVE to drop {table} after "
        f"{_ATTEMPTS} attempts.",
    ) from last_exc


def upgrade() -> None:
    bind = op.get_bind()
    for table in _RETIRED_TABLES:
        _drop_retired(bind, table)
    # Restore the migrator's default lock_timeout (see env.py) for the heavy
    # ALTER/DELETE migrations that follow.
    bind.execute(text("SET lock_timeout = '5min'"))


def downgrade() -> None:
    # The catalog source of truth is Builtins logs. This migration intentionally
    # does not recreate the retired compatibility projection tables.
    pass
