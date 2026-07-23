"""One-time backfill for typed Knowledge ledger auto-count schema.

Unity's typed Knowledge manager expects contexts named ``Knowledge`` (or ending
in ``/Knowledge``) to auto-count ``knowledge_id``. Contexts created before that
contract may lack ``unique_keys`` / ``auto_counting``; create/exist_ok does not
upgrade them. This backfill patches those rows additively.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Exact leaf name ``Knowledge`` — excludes ``Knowledge/Meta`` and other children.
_KNOWLEDGE_LEDGER_NAME_SQL = (
    "(name = 'Knowledge' OR name LIKE '%/Knowledge') "
    "AND name NOT LIKE '%/Knowledge/%'"
)


def backfill_knowledge_ledger_schema(conn: Connection) -> None:
    """Add missing ``knowledge_id`` unique/auto-count schema on Knowledge ledgers.

    Idempotent. Does not rewrite existing log payloads that omit ``knowledge_id``.
    """
    conn.execute(
        text(
            f"""
            UPDATE context AS c
            SET
                unique_key_names = CASE
                    WHEN NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(
                            COALESCE(c.unique_key_names, '[]'::jsonb)
                        ) AS elem(value)
                        WHERE elem.value = 'knowledge_id'
                    )
                    THEN COALESCE(c.unique_key_names, '[]'::jsonb)
                        || '["knowledge_id"]'::jsonb
                    ELSE c.unique_key_names
                END,
                unique_key_types = CASE
                    WHEN NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(
                            COALESCE(c.unique_key_names, '[]'::jsonb)
                        ) AS elem(value)
                        WHERE elem.value = 'knowledge_id'
                    )
                    THEN COALESCE(c.unique_key_types, '[]'::jsonb)
                        || '["int"]'::jsonb
                    ELSE c.unique_key_types
                END,
                auto_counting = CASE
                    WHEN NOT (
                        COALESCE(c.auto_counting, '{{}}'::jsonb) ? 'knowledge_id'
                    )
                    THEN COALESCE(c.auto_counting, '{{}}'::jsonb)
                        || '{{"knowledge_id": null}}'::jsonb
                    ELSE c.auto_counting
                END,
                updated_at = NOW()
            WHERE {_KNOWLEDGE_LEDGER_NAME_SQL}
              AND (
                    NOT EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(
                            COALESCE(c.unique_key_names, '[]'::jsonb)
                        ) AS elem(value)
                        WHERE elem.value = 'knowledge_id'
                    )
                    OR NOT (
                        COALESCE(c.auto_counting, '{{}}'::jsonb) ? 'knowledge_id'
                    )
              )
            """,
        ),
    )
    conn.execute(
        text(
            f"""
            INSERT INTO field_type (
                project_id,
                context_id,
                field_name,
                field_type,
                field_category,
                mutable,
                "unique",
                description
            )
            SELECT
                c.project_id,
                c.id,
                'knowledge_id',
                'int',
                'entry',
                FALSE,
                TRUE,
                'Unique key component (int).'
            FROM context AS c
            WHERE {_KNOWLEDGE_LEDGER_NAME_SQL}
              AND NOT EXISTS (
                    SELECT 1
                    FROM field_type AS ft
                    WHERE ft.project_id = c.project_id
                      AND ft.context_id = c.id
                      AND ft.field_name = 'knowledge_id'
              )
            """,
        ),
    )
