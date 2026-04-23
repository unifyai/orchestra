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

import sqlalchemy as sa
from alembic import op

revision = "add_field_type_backfilled_at"
down_revision = "add_assistant_job_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "field_type",
        sa.Column(
            "backfilled_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_field_type_needs_backfill",
        "field_type",
        ["project_id", "context_id"],
        postgresql_where=sa.text("backfilled_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_field_type_needs_backfill",
        table_name="field_type",
    )
    op.drop_column("field_type", "backfilled_at")
