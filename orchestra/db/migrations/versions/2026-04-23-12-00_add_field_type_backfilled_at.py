"""Add backfilled_at column to field_type.

Introduces ``field_type.backfilled_at`` (nullable TIMESTAMPTZ) as the gate
used by ``POST /v0/logs/fields`` to decide whether the expensive
``UPDATE log_event ... WHERE NOT (data ?& :field_names)`` backfill needs to
run. Before this column, every call to the endpoint scanned the entire
context's log_event rows to evaluate the ``?&`` guard even when no row
needed updating; the guard suppressed writes but not the scan. At
ingestion load this turned into the top-1 CPU consumer in Cloud SQL
Query Insights.

Semantics:

- ``backfilled_at IS NULL`` -> the field has **never** been null-merged
  into every existing row of its context. This is the initial state for
  both newly inserted rows (via ``create_fields``) and rows inserted as a
  side effect of log creation (via ``bulk_create_field_types``), because
  the log-creation path only writes the field into the single log whose
  entries carried it, not into sibling logs.
- ``backfilled_at = <timestamp>`` -> a ``/v0/logs/fields`` call with
  ``backfill_logs=True`` has already null-merged this field into every
  row in the context. Subsequent idempotent re-POSTs can short-circuit
  the expensive UPDATE.

The partial index ``idx_field_type_needs_backfill`` makes the
"which of these fields still need backfill" lookup index-only and
trivially cheap, since the common steady-state after ingestion warmup
is that **no** row matches the partial predicate.

Revision ID: add_field_type_backfilled_at
Revises: add_assistant_job_title
Create Date: 2026-04-23 12:00:00.000000
"""

from alembic import op

revision = "add_field_type_backfilled_at"
down_revision = "add_assistant_job_title"
branch_labels = None
depends_on = None


# `field_type` is a hot table -- ingestion pods constantly upsert into it via
# `bulk_create_field_types`. A naive `ALTER TABLE ... ADD COLUMN` needs
# ACCESS EXCLUSIVE and gets starved out by the session `lock_timeout` under
# production load. The ADD itself is metadata-only (nullable, no default, no
# table rewrite in PG 11+), so we just need to *eventually* catch a window
# where no long-running transaction holds a blocking lock. We wrap the DDL in
# a PL/pgSQL retry loop with a short `lock_timeout` so each attempt yields
# quickly; ~60s of wall-time headroom is plenty in practice.
_ADD_COLUMN_WITH_RETRY = """
DO $$
DECLARE
    attempt int := 0;
    max_attempts int := 60;
BEGIN
    LOOP
        BEGIN
            SET LOCAL lock_timeout = '2s';
            ALTER TABLE field_type
                ADD COLUMN IF NOT EXISTS backfilled_at TIMESTAMP WITH TIME ZONE;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            attempt := attempt + 1;
            IF attempt >= max_attempts THEN
                RAISE EXCEPTION
                    'Could not acquire ACCESS EXCLUSIVE on field_type after % attempts',
                    attempt;
            END IF;
            PERFORM pg_sleep(1);
        END;
    END LOOP;
END
$$;
"""

_DROP_COLUMN_WITH_RETRY = """
DO $$
DECLARE
    attempt int := 0;
    max_attempts int := 60;
BEGIN
    LOOP
        BEGIN
            SET LOCAL lock_timeout = '2s';
            ALTER TABLE field_type DROP COLUMN IF EXISTS backfilled_at;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            attempt := attempt + 1;
            IF attempt >= max_attempts THEN
                RAISE EXCEPTION
                    'Could not acquire ACCESS EXCLUSIVE on field_type after % attempts',
                    attempt;
            END IF;
            PERFORM pg_sleep(1);
        END;
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    op.execute(_ADD_COLUMN_WITH_RETRY)

    # Partial index built CONCURRENTLY so it doesn't block field_type writes
    # during the build. CONCURRENTLY requires no surrounding transaction, so
    # we drop into alembic's autocommit_block. The matching IF NOT EXISTS
    # guard makes the migration safely re-runnable if a prior attempt failed
    # mid-build (left an INVALID index behind).
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_field_type_needs_backfill
            ON field_type (project_id, context_id)
            WHERE backfilled_at IS NULL;
            """,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_field_type_needs_backfill;",
        )
    op.execute(_DROP_COLUMN_WITH_RETRY)
