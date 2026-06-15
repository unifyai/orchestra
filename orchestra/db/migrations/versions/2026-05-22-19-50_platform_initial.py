"""Squashed platform-initial migration.

Replaces the 259-revision platform alembic chain with a single migration
that creates the full platform schema (everything outside the 13 kernel
tables that orchestra's `0001_core_initial` already created). The
schema body is generated verbatim from a `pg_dump` of a fresh database
that ran the historical 259-chain to its head, so every constraint name,
index, function, and partial-unique definition matches production
exactly.

`down_revision` points at orchestra's `0001_core_initial` so the
two chains formally converge: a fresh database now runs core then
platform sequentially with no `DuplicateTable` conflicts.

Production cutover for existing databases is handled by
``orchestra.db.migrations.reconcile.reconcile_to_new_chain`` which the
platform's `env.py` invokes before any upgrade. That function detects
DBs stamped at any pre-squash revision (e.g. `phase3_core_bridge`) and,
provided the expected schema is present, stamps forward to
`_platform_initial` (the new chain's leaf head).

Revision ID: _platform_initial
Revises: 0001_core_initial
Create Date: 2026-05-22 19:50:00.000000
"""

from pathlib import Path

from alembic import op

revision = "_platform_initial"
down_revision = "0001_core_initial"
branch_labels = None
depends_on = None

_SCHEMA_FILE = Path(__file__).parent / "_platform_initial_schema.sql"

# Tables this migration owns (everything that's NOT a kernel table).
# Used by downgrade() to do a clean reverse.
_KERNEL_TABLES = frozenset(
    {
        "project",
        "project_version",
        "context",
        "context_counter",
        "context_version",
        "log_event",
        "log_event_context",
        "log_event_version",
        "active_derived_log_template",
        "log_unique_constraint",
        "field_type",
        "embedding",
        "embedding_queue",
    }
)


def upgrade() -> None:
    """Apply the squashed platform schema.

    Reads the sibling SQL file and executes it as a single block via the
    psycopg2 driver, which handles multi-statement input + dollar-quoted
    function bodies natively (unlike `op.execute` which expects a single
    prepared statement).
    """
    sql = _SCHEMA_FILE.read_text()
    bind = op.get_bind()
    bind.exec_driver_sql(sql)


def downgrade() -> None:
    """Drop every platform table, leaving only the kernel tables intact."""
    bind = op.get_bind()
    rows = bind.exec_driver_sql(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    # `alembic_version` is owned by alembic itself, not by this migration.
    # Dropping it here would prevent alembic from updating its own state
    # row and the downgrade would fail with `relation "alembic_version"
    # does not exist`.
    _RESERVED = _KERNEL_TABLES | {"alembic_version"}
    platform_tables = [r[0] for r in rows if r[0] not in _RESERVED]
    if platform_tables:
        # CASCADE handles all FK dependencies between platform tables.
        quoted = ", ".join(f'public."{t}"' for t in platform_tables)
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {quoted} CASCADE")

    # Drop the platform's helper functions too (the kernel doesn't ship them).
    for fn in (
        "safe_cast_to_date(text)",
        "safe_cast_to_interval(text)",
        "safe_cast_to_time(text)",
        "safe_cast_to_timestamptz(text)",
    ):
        bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS public.{fn}")
