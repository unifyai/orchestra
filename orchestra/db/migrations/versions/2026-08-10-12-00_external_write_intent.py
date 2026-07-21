"""Add external_write_intent outbox table.

Revision ID: external_write_intent
Revises: external_field_binding
Create Date: 2026-08-10 12:00:00.000000
"""

from alembic import op

revision = "external_write_intent"
down_revision = "external_field_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS external_write_intent (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
            context_id INTEGER NOT NULL REFERENCES context(id) ON DELETE CASCADE,
            field_name VARCHAR,
            connector_id VARCHAR NOT NULL,
            binding JSONB NOT NULL DEFAULT '{}'::jsonb,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key VARCHAR NOT NULL,
            log_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            status VARCHAR NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            result JSONB,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITHOUT TIME ZONE,
            confirmed_at TIMESTAMP WITHOUT TIME ZONE,
            CONSTRAINT uq_external_write_intent_project_idempotency
                UNIQUE (project_id, idempotency_key)
        )
        """,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_external_write_intent_status
        ON external_write_intent (status, created_at)
        """,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_external_write_intent_project_context
        ON external_write_intent (project_id, context_id)
        """,
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS external_write_intent")
