"""Retire org-scoped coordinators and enforce personal-only scope.

Revision ID: hard_cut_personal_coordinator
Revises: seed_personal_cm
Create Date: 2026-05-20 16:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "hard_cut_personal_coordinator"
down_revision = "seed_personal_cm"
branch_labels = None
depends_on = None

ORG_COORDINATOR_INDEX_NAME = "ux_assistants_one_coordinator_per_org"
COORDINATOR_SCOPE_CHECK_NAME = "ck_assistants_coordinator_personal_scope"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _repoint_remaining_assistant_foreign_keys(connection: sa.Connection) -> None:
    """Repoint FK columns that still reference retired org coordinator ids."""
    rows = connection.execute(
        sa.text(
            """
            SELECT
                ns.nspname AS schema_name,
                cls.relname AS table_name,
                att.attname AS column_name
            FROM pg_constraint con
            JOIN pg_class cls
                ON cls.oid = con.conrelid
            JOIN pg_namespace ns
                ON ns.oid = cls.relnamespace
            JOIN unnest(con.conkey) AS col(attnum)
                ON TRUE
            JOIN pg_attribute att
                ON att.attrelid = con.conrelid
                AND att.attnum = col.attnum
            WHERE con.contype = 'f'
              AND con.confrelid = 'assistants'::regclass
            ORDER BY schema_name, table_name, column_name
            """,
        ),
    ).mappings()

    excluded_tables = {
        ("public", "assistant_console_config"),
        ("public", "assistant_contacts"),
        ("public", "assistant_secrets"),
        ("public", "assistant_space_memberships"),
        ("public", "contact_memberships"),
    }
    for row in rows:
        schema_name = row["schema_name"]
        table_name = row["table_name"]
        column_name = row["column_name"]
        if (schema_name, table_name) in excluded_tables:
            continue
        quoted_table = (
            f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"
        )
        quoted_column = _quote_identifier(column_name)
        connection.execute(
            sa.text(
                f"""
                UPDATE {quoted_table} AS target
                SET {quoted_column} = remap.new_id
                FROM coordinator_remap AS remap
                WHERE target.{quoted_column} = remap.old_id
                  AND target.{quoted_column} <> remap.new_id
                """,
            ),
        )


def _migrate_org_coordinator_rows(connection: sa.Connection) -> None:
    """Move org coordinator ownership and references onto personal coordinators."""
    connection.execute(
        sa.text(
            """
            CREATE TEMP TABLE coordinator_primary AS
            SELECT DISTINCT ON (assistant.user_id)
                assistant.user_id AS user_id,
                assistant.agent_id AS canonical_id
            FROM assistants AS assistant
            WHERE assistant.is_coordinator = TRUE
              AND assistant.user_id IS NOT NULL
            ORDER BY
                assistant.user_id,
                (assistant.organization_id IS NULL) DESC,
                assistant.created_at ASC NULLS LAST,
                assistant.agent_id ASC
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE assistants AS assistant
            SET organization_id = NULL
            FROM coordinator_primary AS primary_row
            WHERE assistant.agent_id = primary_row.canonical_id
              AND assistant.organization_id IS NOT NULL
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            CREATE TEMP TABLE coordinator_remap AS
            SELECT
                assistant.agent_id AS old_id,
                primary_row.canonical_id AS new_id,
                assistant.user_id AS user_id
            FROM assistants AS assistant
            JOIN coordinator_primary AS primary_row
                ON primary_row.user_id = assistant.user_id
            WHERE assistant.is_coordinator = TRUE
              AND assistant.organization_id IS NOT NULL
              AND assistant.agent_id <> primary_row.canonical_id
            """,
        ),
    )

    connection.execute(
        sa.text(
            """
            INSERT INTO project (
                user_id,
                organization_id,
                name,
                description,
                is_versioned
            )
            SELECT
                primary_row.user_id,
                NULL,
                'Assistants',
                'Project to manage and track all your assistants.',
                FALSE
            FROM coordinator_primary AS primary_row
            LEFT JOIN project AS existing
                ON existing.user_id = primary_row.user_id
               AND existing.organization_id IS NULL
               AND existing.name = 'Assistants'
            WHERE existing.id IS NULL
            """,
        ),
    )

    connection.execute(
        sa.text(
            """
            CREATE TEMP TABLE coordinator_context_move AS
            SELECT
                context.id AS source_context_id,
                target_project.id AS target_project_id,
                REPLACE(
                    context.name,
                    '/' || remap.old_id::text || '/',
                    '/' || remap.new_id::text || '/'
                ) AS target_context_name
            FROM coordinator_remap AS remap
            JOIN context
                ON context.name LIKE remap.user_id || '/' || remap.old_id::text || '/%'
            JOIN project AS source_project
                ON source_project.id = context.project_id
               AND source_project.name = 'Assistants'
            JOIN project AS target_project
                ON target_project.user_id = remap.user_id
               AND target_project.organization_id IS NULL
               AND target_project.name = 'Assistants'
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE context
            SET
                project_id = move.target_project_id,
                name = move.target_context_name
            FROM coordinator_context_move AS move
            LEFT JOIN context AS existing
                ON existing.project_id = move.target_project_id
               AND existing.name = move.target_context_name
            WHERE context.id = move.source_context_id
              AND existing.id IS NULL
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE log_event_context AS link
            SET context_id = existing.id
            FROM coordinator_context_move AS move
            JOIN context AS existing
                ON existing.project_id = move.target_project_id
               AND existing.name = move.target_context_name
            WHERE link.context_id = move.source_context_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM log_event_context AS duplicate
                  WHERE duplicate.log_event_id = link.log_event_id
                    AND duplicate.context_id = existing.id
              )
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM log_event_context AS link
            USING coordinator_context_move AS move
            JOIN context AS existing
                ON existing.project_id = move.target_project_id
               AND existing.name = move.target_context_name
            WHERE link.context_id = move.source_context_id
              AND existing.id <> move.source_context_id
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM context
            USING coordinator_context_move AS move
            JOIN context AS existing
                ON existing.project_id = move.target_project_id
               AND existing.name = move.target_context_name
            WHERE context.id = move.source_context_id
              AND existing.id <> move.source_context_id
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE log_event AS log_event_row
            SET project_id = context_project.project_id
            FROM (
                SELECT
                    link.log_event_id AS log_event_id,
                    MIN(context.project_id) AS project_id
                FROM log_event_context AS link
                JOIN context
                    ON context.id = link.context_id
                GROUP BY link.log_event_id
            ) AS context_project
            WHERE log_event_row.id = context_project.log_event_id
              AND log_event_row.project_id <> context_project.project_id
            """,
        ),
    )

    connection.execute(
        sa.text(
            """
            UPDATE assistant_space_memberships AS membership
            SET assistant_id = remap.new_id
            FROM coordinator_remap AS remap
            WHERE membership.assistant_id = remap.old_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM assistant_space_memberships AS existing
                  WHERE existing.assistant_id = remap.new_id
                    AND existing.space_id = membership.space_id
              )
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM assistant_space_memberships AS membership
            USING coordinator_remap AS remap
            WHERE membership.assistant_id = remap.old_id
            """,
        ),
    )

    connection.execute(
        sa.text(
            """
            DELETE FROM contact_memberships AS membership
            USING coordinator_remap AS remap,
                  contact_memberships AS existing
            WHERE membership.assistant_id = remap.old_id
              AND existing.assistant_id = remap.new_id
              AND existing.contact_id = membership.contact_id
              AND existing.target_scope = membership.target_scope
              AND (
                    existing.target_space_id = membership.target_space_id
                 OR (
                        existing.target_space_id IS NULL
                    AND membership.target_space_id IS NULL
                 )
              )
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE contact_memberships AS membership
            SET assistant_id = remap.new_id
            FROM coordinator_remap AS remap
            WHERE membership.assistant_id = remap.old_id
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE contact_memberships AS membership
            SET authoring_assistant_id = remap.new_id
            FROM coordinator_remap AS remap
            WHERE membership.authoring_assistant_id = remap.old_id
            """,
        ),
    )

    connection.execute(
        sa.text(
            """
            DELETE FROM resource_access AS access
            USING coordinator_remap AS remap,
                  resource_access AS existing
            WHERE access.resource_type = 'assistant'
              AND access.resource_id = remap.old_id
              AND existing.resource_type = 'assistant'
              AND existing.resource_id = remap.new_id
              AND existing.grantee_type = access.grantee_type
              AND existing.grantee_id = access.grantee_id
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE resource_access AS access
            SET resource_id = remap.new_id
            FROM coordinator_remap AS remap
            WHERE access.resource_type = 'assistant'
              AND access.resource_id = remap.old_id
            """,
        ),
    )

    connection.execute(
        sa.text(
            """
            DELETE FROM assistant_secrets AS secret
            USING coordinator_remap AS remap,
                  assistant_secrets AS existing
            WHERE secret.agent_id = remap.old_id
              AND existing.agent_id = remap.new_id
              AND existing.secret_name = secret.secret_name
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE assistant_secrets AS secret
            SET agent_id = remap.new_id
            FROM coordinator_remap AS remap
            WHERE secret.agent_id = remap.old_id
            """,
        ),
    )

    connection.execute(
        sa.text(
            """
            DELETE FROM assistant_console_config AS config
            USING coordinator_remap AS remap,
                  assistant_console_config AS existing
            WHERE config.assistant_id = remap.old_id
              AND existing.assistant_id = remap.new_id
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE assistant_console_config AS config
            SET assistant_id = remap.new_id
            FROM coordinator_remap AS remap
            WHERE config.assistant_id = remap.old_id
            """,
        ),
    )

    connection.execute(
        sa.text(
            """
            DELETE FROM assistant_contacts AS contact
            USING coordinator_remap AS remap
            WHERE contact.assistant_id = remap.old_id
            """,
        ),
    )

    _repoint_remaining_assistant_foreign_keys(connection)

    connection.execute(
        sa.text(
            """
            UPDATE log_event AS log_event_row
            SET data = jsonb_set(
                log_event_row.data,
                '{_assistant_id}',
                to_jsonb(remap.new_id::text),
                FALSE
            )
            FROM coordinator_remap AS remap
            WHERE log_event_row.data ->> '_assistant_id' = remap.old_id::text
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE log_event AS log_event_row
            SET data = jsonb_set(
                log_event_row.data,
                '{assistant_id}',
                to_jsonb(remap.new_id::text),
                FALSE
            )
            FROM coordinator_remap AS remap
            WHERE log_event_row.data ->> 'assistant_id' = remap.old_id::text
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE log_event AS log_event_row
            SET data = jsonb_set(
                log_event_row.data,
                '{authoring_assistant_id}',
                to_jsonb(remap.new_id),
                FALSE
            )
            FROM coordinator_remap AS remap
            WHERE log_event_row.data ->> 'authoring_assistant_id' = remap.old_id::text
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE log_event AS log_event_row
            SET data = jsonb_set(
                log_event_row.data,
                '{metadata,source_assistant_id}',
                to_jsonb(remap.new_id::text),
                FALSE
            )
            FROM coordinator_remap AS remap
            WHERE log_event_row.data #>> '{metadata,source_assistant_id}'
                  = remap.old_id::text
            """,
        ),
    )

    connection.execute(
        sa.text(
            """
            DELETE FROM assistants AS assistant
            USING coordinator_remap AS remap
            WHERE assistant.agent_id = remap.old_id
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            UPDATE assistants
            SET organization_id = NULL
            WHERE is_coordinator = TRUE
              AND organization_id IS NOT NULL
            """,
        ),
    )


def upgrade() -> None:
    connection = op.get_bind()
    _migrate_org_coordinator_rows(connection)
    op.drop_index(ORG_COORDINATOR_INDEX_NAME, table_name="assistants")
    op.create_check_constraint(
        COORDINATOR_SCOPE_CHECK_NAME,
        "assistants",
        "(NOT is_coordinator) OR organization_id IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint(COORDINATOR_SCOPE_CHECK_NAME, "assistants", type_="check")
    op.create_index(
        ORG_COORDINATOR_INDEX_NAME,
        "assistants",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text(
            "is_coordinator AND organization_id IS NOT NULL",
        ),
    )
