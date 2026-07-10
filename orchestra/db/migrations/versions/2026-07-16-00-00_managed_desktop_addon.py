"""Managed Computer Use paid add-on billing fields and pricing seed.

Revision ID: managed_desktop_addon
Revises: assistant_owner_team
Create Date: 2026-07-16 00:00:00

Nullable ADD COLUMN on ``assistants`` still needs ACCESS EXCLUSIVE. A long
``lock_timeout`` while waiting for that lock queues behind the waiter and
blocks ordinary ACCESS SHARE readers (list assistants, OAuth complete, etc.).
Use a short per-attempt timeout + retry so failed attempts release the queue
quickly; IF NOT EXISTS keeps partial re-runs safe.
"""

from __future__ import annotations

import time

from alembic import op
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

revision = "managed_desktop_addon"
down_revision = "assistant_owner_team"
branch_labels = None
depends_on = None

_LOCK_NOT_AVAILABLE = "55P03"
_DDL_LOCK_TIMEOUT = "15s"
_ATTEMPTS = 20
_BACKOFF_SECONDS = 3.0

_ASSISTANT_COLUMNS = (
    ("managed_desktop_status", "VARCHAR"),
    ("managed_desktop_monthly_cost", "NUMERIC"),
    ("managed_desktop_last_billed_month", "VARCHAR"),
    (
        "managed_desktop_grace_period_started_at",
        "TIMESTAMP WITH TIME ZONE",
    ),
    (
        "managed_desktop_enabled_at",
        "TIMESTAMP WITH TIME ZONE",
    ),
)


def _is_lock_timeout(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    if getattr(orig, "pgcode", None) == _LOCK_NOT_AVAILABLE:
        return True
    return "lock timeout" in str(exc).lower()


def _execute_with_lock_retry(bind, sql: str) -> None:
    """Run DDL with a short lock_timeout, retrying instead of queue-blocking."""
    last_exc: OperationalError | None = None
    for attempt in range(_ATTEMPTS):
        bind.execute(text(f"SET lock_timeout = '{_DDL_LOCK_TIMEOUT}'"))
        savepoint = bind.begin_nested()
        try:
            bind.execute(text(sql))
            savepoint.commit()
            return
        except OperationalError as exc:
            savepoint.rollback()
            if not _is_lock_timeout(exc):
                raise
            last_exc = exc
            time.sleep(_BACKOFF_SECONDS * (1 + attempt // 5))
    raise RuntimeError(
        f"Could not acquire lock for DDL after {_ATTEMPTS} attempts: {sql}",
    ) from last_exc


def _ensure_contact_type_check(bind) -> None:
    """Replace the contact_type check so managed_desktop is allowed."""
    _execute_with_lock_retry(
        bind,
        "ALTER TABLE contact_type_costs DROP CONSTRAINT IF EXISTS "
        "ck_contact_type_cost_type",
    )
    bind.execute(text(f"SET lock_timeout = '{_DDL_LOCK_TIMEOUT}'"))
    savepoint = bind.begin_nested()
    try:
        bind.execute(
            text(
                "ALTER TABLE contact_type_costs ADD CONSTRAINT "
                "ck_contact_type_cost_type CHECK (contact_type IN "
                "('phone', 'email', 'whatsapp', 'discord', 'managed_desktop'))",
            ),
        )
        savepoint.commit()
    except OperationalError as exc:
        savepoint.rollback()
        if _is_lock_timeout(exc):
            _execute_with_lock_retry(
                bind,
                "ALTER TABLE contact_type_costs ADD CONSTRAINT "
                "ck_contact_type_cost_type CHECK (contact_type IN "
                "('phone', 'email', 'whatsapp', 'discord', 'managed_desktop'))",
            )
            return
        if getattr(getattr(exc, "orig", None), "pgcode", None) == "42710":
            return
        if "already exists" in str(exc).lower():
            return
        raise


def upgrade() -> None:
    bind = op.get_bind()
    for column_name, column_type in _ASSISTANT_COLUMNS:
        _execute_with_lock_retry(
            bind,
            f"ALTER TABLE assistants ADD COLUMN IF NOT EXISTS "
            f"{column_name} {column_type}",
        )

    _ensure_contact_type_check(bind)

    bind.execute(
        text(
            """
            INSERT INTO contact_type_costs (
                contact_type, provider, country_code, monthly_cost, one_time_cost
            )
            VALUES
                ('managed_desktop', 'ubuntu', NULL, 50.00, 0.00),
                ('managed_desktop', 'windows', NULL, 75.00, 0.00)
            ON CONFLICT (contact_type, provider, country_code) DO NOTHING
            """,
        ),
    )

    # Restore the migrator session default for any later revisions in the same run.
    bind.execute(text("SET lock_timeout = '5min'"))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            """
            DELETE FROM contact_type_costs
            WHERE contact_type = 'managed_desktop'
            """,
        ),
    )
    _execute_with_lock_retry(
        bind,
        "ALTER TABLE contact_type_costs DROP CONSTRAINT IF EXISTS "
        "ck_contact_type_cost_type",
    )
    _execute_with_lock_retry(
        bind,
        "ALTER TABLE contact_type_costs ADD CONSTRAINT ck_contact_type_cost_type "
        "CHECK (contact_type IN ('phone', 'email', 'whatsapp', 'discord'))",
    )
    for column_name, _column_type in reversed(_ASSISTANT_COLUMNS):
        _execute_with_lock_retry(
            bind,
            f"ALTER TABLE assistants DROP COLUMN IF EXISTS {column_name}",
        )
    bind.execute(text("SET lock_timeout = '5min'"))
