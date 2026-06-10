"""Integration provider platform tables.

Revision ID: 2026_provider_integrations
Revises: 2026_plot_table_context_fks
Create Date: 2026-06-04 18:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "2026_provider_integrations"
down_revision = "2026_plot_table_context_fks"
branch_labels = None
depends_on = None

JSON_EMPTY_ARRAY = sa.text("'[]'::jsonb")
JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "integration_backends",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="enabled"),
        sa.Column("credentials_secret_ref", sa.String(), nullable=True),
        sa.Column("webhook_secret_ref", sa.String(), nullable=True),
        sa.Column(
            "allowed_orgs_or_tenants",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "default_priority",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
        sa.Column(
            "config_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_integration_backends_backend_id",
        "integration_backends",
        ["backend_id"],
        unique=True,
    )
    op.create_index("ix_integration_backends_kind", "integration_backends", ["kind"])
    op.create_index(
        "ix_integration_backends_environment",
        "integration_backends",
        ["environment"],
    )
    op.create_index(
        "ix_integration_backends_status",
        "integration_backends",
        ["status"],
    )

    op.create_table(
        "dynamic_provider_apps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("provider_app_id", sa.String(), nullable=False),
        sa.Column("canonical_app_slug", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("icon_url", sa.String(), nullable=True),
        sa.Column(
            "auth_modes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "available_scopes_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "available_actions_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "raw_provider_metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column(
            "cache_version",
            sa.String(),
            nullable=False,
            server_default="local-v0",
        ),
        sa.Column(
            "last_synced_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "backend_id",
            "provider_app_id",
            name="uq_dynamic_provider_app_backend_provider_app",
        ),
        sa.UniqueConstraint(
            "backend_id",
            "canonical_app_slug",
            name="uq_dynamic_provider_app_backend_slug",
        ),
    )
    op.create_index(
        "ix_dynamic_provider_apps_backend_id",
        "dynamic_provider_apps",
        ["backend_id"],
    )
    op.create_index(
        "ix_dynamic_provider_apps_canonical_app_slug",
        "dynamic_provider_apps",
        ["canonical_app_slug"],
    )
    op.create_index(
        "ix_dynamic_provider_apps_category",
        "dynamic_provider_apps",
        ["category"],
    )
    op.create_index(
        "ix_dynamic_provider_apps_slug_display",
        "dynamic_provider_apps",
        ["canonical_app_slug", "display_name"],
    )

    op.create_table(
        "provider_tool_catalog",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tool_id", sa.String(), nullable=False),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("provider_app_id", sa.String(), nullable=False),
        sa.Column("canonical_app_slug", sa.String(), nullable=False),
        sa.Column("provider_tool_id", sa.String(), nullable=False),
        sa.Column("unify_tool_id", sa.String(), nullable=False),
        sa.Column("canonical_name", sa.String(), nullable=False),
        sa.Column("function_manager_name", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "tags_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column(
            "input_schema_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column(
            "output_schema_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column(
            "required_scopes_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column("action_class", sa.String(), nullable=False, server_default="read"),
        sa.Column(
            "data_categories_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "examples_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "provider_raw_metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("embedding_ref", sa.String(), nullable=True),
        sa.Column(
            "overlay_rank_boost",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "enabled_by_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "confirmation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "last_synced_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "backend_id",
            "provider_tool_id",
            name="uq_provider_tool_catalog_backend_provider_tool",
        ),
    )
    op.create_index(
        "ix_provider_tool_catalog_tool_id",
        "provider_tool_catalog",
        ["tool_id"],
        unique=True,
    )
    op.create_index(
        "ix_provider_tool_catalog_backend_id",
        "provider_tool_catalog",
        ["backend_id"],
    )
    op.create_index(
        "ix_provider_tool_catalog_provider_app_id",
        "provider_tool_catalog",
        ["provider_app_id"],
    )
    op.create_index(
        "ix_provider_tool_catalog_canonical_app_slug",
        "provider_tool_catalog",
        ["canonical_app_slug"],
    )
    op.create_index(
        "ix_provider_tool_catalog_unify_tool_id",
        "provider_tool_catalog",
        ["unify_tool_id"],
        unique=True,
    )
    op.create_index(
        "ix_provider_tool_catalog_canonical_name",
        "provider_tool_catalog",
        ["canonical_name"],
        unique=True,
    )
    op.create_index(
        "ix_provider_tool_catalog_function_manager_name",
        "provider_tool_catalog",
        ["function_manager_name"],
        unique=True,
    )
    op.create_index(
        "ix_provider_tool_catalog_category",
        "provider_tool_catalog",
        ["category"],
    )
    op.create_index(
        "ix_provider_tool_catalog_action_class",
        "provider_tool_catalog",
        ["action_class"],
    )
    op.create_index(
        "ix_provider_tool_catalog_embedding_ref",
        "provider_tool_catalog",
        ["embedding_ref"],
    )
    op.create_index(
        "ix_provider_tool_catalog_slug_name",
        "provider_tool_catalog",
        ["canonical_app_slug", "name"],
    )

    op.create_table(
        "integration_overlays",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("canonical_app_slug", sa.String(), nullable=False),
        sa.Column(
            "owner_scope",
            sa.String(),
            nullable=False,
            server_default="platform",
        ),
        sa.Column("owner_id", sa.String(), nullable=True),
        sa.Column("provider_preference", sa.String(), nullable=True),
        sa.Column(
            "display_overrides_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column(
            "recommended_scopes_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "action_policy_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column(
            "capability_groups_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "actor_guidance_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "data_categories_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "sync_strategy",
            sa.String(),
            nullable=False,
            server_default="provider_live",
        ),
        sa.Column(
            "quality_tier",
            sa.String(),
            nullable=False,
            server_default="standard",
        ),
        sa.Column("owner", sa.String(), nullable=False, server_default="platform"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "canonical_app_slug",
            "owner_scope",
            "owner_id",
            name="uq_integration_overlay_scope",
        ),
    )
    op.create_index(
        "ix_integration_overlays_canonical_app_slug",
        "integration_overlays",
        ["canonical_app_slug"],
    )
    op.create_index(
        "ix_integration_overlays_owner_scope",
        "integration_overlays",
        ["owner_scope"],
    )
    op.create_index(
        "ix_integration_overlays_owner_id",
        "integration_overlays",
        ["owner_id"],
    )

    op.create_table(
        "integration_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("owner_scope", sa.String(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("assistant_id", sa.Integer(), nullable=True),
        sa.Column("canonical_app_slug", sa.String(), nullable=False),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("provider_app_id", sa.String(), nullable=False),
        sa.Column("provider_connection_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("external_account_label", sa.String(), nullable=True),
        sa.Column(
            "granted_scopes_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "enabled_capabilities_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "disabled_actions_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_ARRAY,
        ),
        sa.Column(
            "credential_storage",
            sa.String(),
            nullable=False,
            server_default="provider_vault",
        ),
        sa.Column(
            "secret_refs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=JSON_EMPTY_OBJECT,
        ),
        sa.Column("last_health_check_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_health_check_status", sa.String(), nullable=True),
        sa.Column("reconnect_reason", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_integration_connections_connection_id",
        "integration_connections",
        ["connection_id"],
        unique=True,
    )
    op.create_index(
        "ix_integration_connections_owner_scope",
        "integration_connections",
        ["owner_scope"],
    )
    op.create_index(
        "ix_integration_connections_org_id",
        "integration_connections",
        ["org_id"],
    )
    op.create_index(
        "ix_integration_connections_team_id",
        "integration_connections",
        ["team_id"],
    )
    op.create_index(
        "ix_integration_connections_user_id",
        "integration_connections",
        ["user_id"],
    )
    op.create_index(
        "ix_integration_connections_assistant_id",
        "integration_connections",
        ["assistant_id"],
    )
    op.create_index(
        "ix_integration_connections_canonical_app_slug",
        "integration_connections",
        ["canonical_app_slug"],
    )
    op.create_index(
        "ix_integration_connections_backend_id",
        "integration_connections",
        ["backend_id"],
    )
    op.create_index(
        "ix_integration_connections_provider_connection_id",
        "integration_connections",
        ["provider_connection_id"],
    )
    op.create_index(
        "ix_integration_connections_status",
        "integration_connections",
        ["status"],
    )
    op.create_index(
        "ix_integration_connections_effective_owner",
        "integration_connections",
        ["owner_scope", "org_id", "team_id", "user_id", "assistant_id"],
    )
    op.create_index(
        "ix_integration_connections_app_status",
        "integration_connections",
        ["canonical_app_slug", "status"],
    )

    op.create_table(
        "provider_action_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("assistant_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("connection_id", sa.String(), nullable=True),
        sa.Column("backend_id", sa.String(), nullable=False),
        sa.Column("canonical_app_slug", sa.String(), nullable=False),
        sa.Column("provider_action_id", sa.String(), nullable=False),
        sa.Column("provider_tool_id", sa.String(), nullable=False),
        sa.Column("unify_tool_id", sa.String(), nullable=False),
        sa.Column("action_class", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("redacted_input_summary", sa.Text(), nullable=True),
        sa.Column("redacted_output_summary", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    for column_name in [
        "org_id",
        "team_id",
        "user_id",
        "assistant_id",
        "conversation_id",
        "connection_id",
        "backend_id",
        "canonical_app_slug",
        "unify_tool_id",
        "action_class",
        "status",
    ]:
        op.create_index(
            f"ix_provider_action_audits_{column_name}",
            "provider_action_audits",
            [column_name],
        )


def downgrade() -> None:
    for column_name in [
        "status",
        "action_class",
        "unify_tool_id",
        "canonical_app_slug",
        "backend_id",
        "connection_id",
        "conversation_id",
        "assistant_id",
        "user_id",
        "team_id",
        "org_id",
    ]:
        op.drop_index(
            f"ix_provider_action_audits_{column_name}",
            table_name="provider_action_audits",
        )
    op.drop_table("provider_action_audits")

    op.drop_index(
        "ix_integration_connections_app_status",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_effective_owner",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_status",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_provider_connection_id",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_backend_id",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_canonical_app_slug",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_assistant_id",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_user_id",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_team_id",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_org_id",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_owner_scope",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_connection_id",
        table_name="integration_connections",
    )
    op.drop_table("integration_connections")

    op.drop_index("ix_integration_overlays_owner_id", table_name="integration_overlays")
    op.drop_index(
        "ix_integration_overlays_owner_scope",
        table_name="integration_overlays",
    )
    op.drop_index(
        "ix_integration_overlays_canonical_app_slug",
        table_name="integration_overlays",
    )
    op.drop_table("integration_overlays")

    op.drop_index(
        "ix_provider_tool_catalog_slug_name",
        table_name="provider_tool_catalog",
    )
    op.drop_index(
        "ix_provider_tool_catalog_embedding_ref",
        table_name="provider_tool_catalog",
    )
    op.drop_index(
        "ix_provider_tool_catalog_action_class",
        table_name="provider_tool_catalog",
    )
    op.drop_index(
        "ix_provider_tool_catalog_category",
        table_name="provider_tool_catalog",
    )
    op.drop_index(
        "ix_provider_tool_catalog_function_manager_name",
        table_name="provider_tool_catalog",
    )
    op.drop_index(
        "ix_provider_tool_catalog_canonical_name",
        table_name="provider_tool_catalog",
    )
    op.drop_index(
        "ix_provider_tool_catalog_unify_tool_id",
        table_name="provider_tool_catalog",
    )
    op.drop_index(
        "ix_provider_tool_catalog_canonical_app_slug",
        table_name="provider_tool_catalog",
    )
    op.drop_index(
        "ix_provider_tool_catalog_provider_app_id",
        table_name="provider_tool_catalog",
    )
    op.drop_index(
        "ix_provider_tool_catalog_backend_id",
        table_name="provider_tool_catalog",
    )
    op.drop_index(
        "ix_provider_tool_catalog_tool_id",
        table_name="provider_tool_catalog",
    )
    op.drop_table("provider_tool_catalog")

    op.drop_index(
        "ix_dynamic_provider_apps_slug_display",
        table_name="dynamic_provider_apps",
    )
    op.drop_index(
        "ix_dynamic_provider_apps_category",
        table_name="dynamic_provider_apps",
    )
    op.drop_index(
        "ix_dynamic_provider_apps_canonical_app_slug",
        table_name="dynamic_provider_apps",
    )
    op.drop_index(
        "ix_dynamic_provider_apps_backend_id",
        table_name="dynamic_provider_apps",
    )
    op.drop_table("dynamic_provider_apps")

    op.drop_index("ix_integration_backends_status", table_name="integration_backends")
    op.drop_index(
        "ix_integration_backends_environment",
        table_name="integration_backends",
    )
    op.drop_index("ix_integration_backends_kind", table_name="integration_backends")
    op.drop_index(
        "ix_integration_backends_backend_id",
        table_name="integration_backends",
    )
    op.drop_table("integration_backends")
