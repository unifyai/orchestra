"""One-time drop of the legacy Tasks ``instance_id`` auto-count schema.

Before task run state was collapsed onto Tasks definitions plus
``Tasks/Executions`` (unity ``ef49040db``, 2026-07-21), the per-assistant
``.../Tasks`` context declared ``unique_keys={task_id, instance_id}`` and
``auto_counting={task_id: None, instance_id: "task_id"}`` — each execution was
a row in the same context, counted per task. The collapse changed only the
*declared* schema; ``create_context(exist_ok)`` never upgrades existing
contexts, so pre-collapse contexts kept the stale ``instance_id`` registration.

That stale registration breaks the typed Tasks create path outright: it
pre-allocates ``task_id`` and provides it explicitly, so auto-counting
``instance_id`` demands the (brand-new) parent ``task_id`` to already exist and
every create fails with 400 ``Cannot generate auto-counting value for
'instance_id' …``.

This drop removes ``instance_id`` from ``auto_counting`` and the unique-key
arrays on task-surface contexts, and deletes the now-orphaned ``instance_id``
counter rows. Legacy ``__composite__`` rows in ``log_unique_constraint`` are
left in place: validation is driven by the current unique-key registration, so
they are inert. Row payloads keep their historical ``instance_id`` values;
readers already drop the field (unity ``_LEGACY_DEFINITION_FIELDS``).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Exact leaf name ``Tasks`` (``Tasks``, ``{user}/{assistant}/Tasks``,
# ``Teams/{team}/Tasks``) — excludes children such as ``Tasks/Executions``.
# The auto-count shape predicate is the real discriminator; only the legacy
# TaskScheduler surface ever counted ``instance_id`` under ``task_id``.
_LEGACY_TASKS_PREDICATE_SQL = (
    "c.auto_counting->>'instance_id' = 'task_id' "
    "AND (c.name = 'Tasks' OR c.name LIKE '%/Tasks') "
    "AND c.name NOT LIKE '%/Tasks/%'"
)


def drop_legacy_tasks_instance_id_schema(conn: Connection) -> None:
    """Remove the stale ``instance_id`` registration from legacy Tasks contexts.

    Idempotent: the predicate stops matching once ``auto_counting`` no longer
    carries ``instance_id``. Does not rewrite existing log payloads.
    """
    conn.execute(
        text(
            f"""
            DELETE FROM context_counter AS cc
            USING context AS c
            WHERE cc.context_id = c.id
              AND cc.column_name = 'instance_id'
              AND {_LEGACY_TASKS_PREDICATE_SQL}
            """,
        ),
    )
    conn.execute(
        text(
            f"""
            UPDATE context AS c
            SET
                auto_counting = c.auto_counting - 'instance_id',
                unique_key_names = COALESCE(
                    (
                        SELECT jsonb_agg(n.name_elem ORDER BY n.ord)
                        FROM jsonb_array_elements_text(
                            COALESCE(c.unique_key_names, '[]'::jsonb)
                        ) WITH ORDINALITY AS n(name_elem, ord)
                        WHERE n.name_elem <> 'instance_id'
                    ),
                    '[]'::jsonb
                ),
                unique_key_types = COALESCE(
                    (
                        SELECT jsonb_agg(t.type_elem ORDER BY t.ord)
                        FROM jsonb_array_elements_text(
                            COALESCE(c.unique_key_types, '[]'::jsonb)
                        ) WITH ORDINALITY AS t(type_elem, ord)
                        JOIN jsonb_array_elements_text(
                            COALESCE(c.unique_key_names, '[]'::jsonb)
                        ) WITH ORDINALITY AS n(name_elem, ord)
                            ON n.ord = t.ord
                        WHERE n.name_elem <> 'instance_id'
                    ),
                    '[]'::jsonb
                )
            WHERE {_LEGACY_TASKS_PREDICATE_SQL}
            """,
        ),
    )
