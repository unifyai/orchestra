"""Passthrough provider-event binding columns."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "provider_trigger_passthrough"
down_revision = "provider_trigger_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_trigger_bindings",
        sa.Column("provider_trigger_slug", sa.String(), nullable=True),
    )
    op.add_column(
        "event_trigger_bindings",
        sa.Column(
            "trigger_config_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE event_trigger_bindings
        SET provider_trigger_slug = CASE backend_id
            WHEN 'composio' THEN 'GITHUB_ISSUE_CREATED_TRIGGER'
            WHEN 'pipedream' THEN 'github-new-or-updated-issue'
            ELSE COALESCE(event_slug, 'unknown')
        END,
        trigger_config_json = COALESCE(
            (
                SELECT jsonb_build_object(
                    'owner', split_part(filters_json->0->>'value', '/', 1),
                    'repo', split_part(filters_json->0->>'value', '/', 2)
                )
                WHERE filters_json IS NOT NULL
                  AND jsonb_array_length(filters_json) > 0
                  AND filters_json->0->>'field' = 'repository'
            ),
            '{}'::jsonb
        )
        WHERE provider_trigger_slug IS NULL
        """,
    )
    op.alter_column("event_trigger_bindings", "provider_trigger_slug", nullable=False)
    op.alter_column(
        "event_trigger_bindings",
        "trigger_config_json",
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    op.drop_column("event_trigger_bindings", "filters_json")
    op.drop_column("event_trigger_bindings", "schema_version")
    op.drop_column("event_trigger_bindings", "event_slug")


def downgrade() -> None:
    op.add_column(
        "event_trigger_bindings",
        sa.Column("event_slug", sa.String(), nullable=True),
    )
    op.add_column(
        "event_trigger_bindings",
        sa.Column("schema_version", sa.String(), nullable=True),
    )
    op.add_column(
        "event_trigger_bindings",
        sa.Column(
            "filters_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE event_trigger_bindings
        SET event_slug = 'github.issue_created',
            schema_version = '1',
            filters_json = '[]'::jsonb
        """,
    )
    op.alter_column("event_trigger_bindings", "event_slug", nullable=False)
    op.alter_column("event_trigger_bindings", "schema_version", nullable=False)
    op.alter_column(
        "event_trigger_bindings",
        "filters_json",
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    )
    op.drop_column("event_trigger_bindings", "trigger_config_json")
    op.drop_column("event_trigger_bindings", "provider_trigger_slug")
