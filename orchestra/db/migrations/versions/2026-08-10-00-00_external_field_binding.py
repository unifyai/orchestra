"""Add external_field_binding for REST-bound columns.

Revision ID: external_field_binding
Revises: call_session_thread_idx
Create Date: 2026-08-10 00:00:00.000000
"""

from alembic import op

revision = "external_field_binding"
down_revision = "call_session_thread_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS external_field_binding (
            id SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
            context_id INTEGER NOT NULL REFERENCES context(id) ON DELETE CASCADE,
            field_name VARCHAR NOT NULL,
            connector_id VARCHAR NOT NULL,
            binding JSONB NOT NULL DEFAULT '{}'::jsonb,
            binding_version INTEGER NOT NULL DEFAULT 1,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITHOUT TIME ZONE,
            CONSTRAINT uq_external_field_binding_project_context_field
                UNIQUE (project_id, context_id, field_name)
        )
        """,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_external_field_binding_project_id
        ON external_field_binding (project_id)
        """,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_external_field_binding_context_id
        ON external_field_binding (context_id)
        """,
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS external_field_binding")
