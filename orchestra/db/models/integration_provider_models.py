"""Provider-backed integration control-plane models.

Builtins project contexts are the durable app/tool catalog. Orchestra keeps only
mutable operational state: backend config, bootstrap state, connections, auth,
overlays, policy, approvals, and audits.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import (
    TIMESTAMP,
    Column,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from orchestra.db.base import Base

JSON_EMPTY_ARRAY = sa.text("'[]'::jsonb")
JSON_EMPTY_OBJECT = sa.text("'{}'::jsonb")


class IntegrationBackend(Base):
    """Configured provider backend such as Composio, Pipedream, or custom SDK."""

    __tablename__ = "integration_backends"

    id = Column(Integer, primary_key=True)
    backend_id = Column(String, nullable=False, unique=True, index=True)
    kind = Column(String, nullable=False, index=True)
    environment = Column(String, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    status = Column(String, nullable=False, server_default="enabled", index=True)
    credentials_secret_ref = Column(String, nullable=True)
    webhook_secret_ref = Column(String, nullable=True)
    allowed_orgs_or_tenants = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_ARRAY,
    )
    default_priority = Column(Integer, nullable=False, server_default="100")
    config_json = Column(JSONB, nullable=False, server_default=JSON_EMPTY_OBJECT)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class IntegrationBootstrapState(Base):
    """Last applied cloud bootstrap manifest state for a provider backend."""

    __tablename__ = "integration_bootstrap_state"

    id = Column(Integer, primary_key=True)
    environment = Column(String, nullable=False, index=True)
    backend_id = Column(String, nullable=False, index=True)
    desired_hash = Column(String, nullable=False)
    desired_config_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_OBJECT,
    )
    last_status = Column(String, nullable=False, server_default="pending", index=True)
    last_error = Column(Text, nullable=True)
    apps_upserted = Column(Integer, nullable=False, server_default="0")
    tools_upserted = Column(Integer, nullable=False, server_default="0")
    last_sync_diagnostics_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_OBJECT,
    )
    last_synced_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "environment",
            "backend_id",
            name="uq_integration_bootstrap_state_env_backend",
        ),
    )


class IntegrationOverlay(Base):
    """Declarative Unify overlay metadata for provider-backed apps."""

    __tablename__ = "integration_overlays"

    id = Column(Integer, primary_key=True)
    canonical_app_slug = Column(String, nullable=False, index=True)
    owner_scope = Column(String, nullable=False, server_default="platform", index=True)
    owner_id = Column(String, nullable=True, index=True)
    provider_preference = Column(String, nullable=True)
    display_overrides_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_OBJECT,
    )
    recommended_scopes_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_ARRAY,
    )
    action_policy_json = Column(JSONB, nullable=False, server_default=JSON_EMPTY_OBJECT)
    capability_groups_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_ARRAY,
    )
    actor_guidance_json = Column(JSONB, nullable=False, server_default=JSON_EMPTY_ARRAY)
    data_categories_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_ARRAY,
    )
    sync_strategy = Column(String, nullable=False, server_default="provider_live")
    quality_tier = Column(String, nullable=False, server_default="standard")
    owner = Column(String, nullable=False, server_default="platform")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "canonical_app_slug",
            "owner_scope",
            "owner_id",
            name="uq_integration_overlay_scope",
        ),
    )


class IntegrationConnection(Base):
    """Persisted org/team/user/assistant connection state."""

    __tablename__ = "integration_connections"

    id = Column(Integer, primary_key=True)
    connection_id = Column(String, nullable=False, unique=True, index=True)
    owner_scope = Column(String, nullable=False, index=True)
    org_id = Column(Integer, nullable=True, index=True)
    team_id = Column(Integer, nullable=True, index=True)
    user_id = Column(String, nullable=True, index=True)
    assistant_id = Column(Integer, nullable=True, index=True)
    canonical_app_slug = Column(String, nullable=False, index=True)
    backend_id = Column(String, nullable=False, index=True)
    provider_app_id = Column(String, nullable=False)
    provider_connection_id = Column(String, nullable=True, index=True)
    status = Column(String, nullable=False, server_default="pending", index=True)
    external_account_label = Column(String, nullable=True)
    granted_scopes_json = Column(JSONB, nullable=False, server_default=JSON_EMPTY_ARRAY)
    enabled_capabilities_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_ARRAY,
    )
    disabled_actions_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_ARRAY,
    )
    credential_storage = Column(String, nullable=False, server_default="provider_vault")
    secret_refs_json = Column(JSONB, nullable=False, server_default=JSON_EMPTY_OBJECT)
    last_health_check_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_health_check_status = Column(String, nullable=True)
    reconnect_reason = Column(String, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "ix_integration_connections_effective_owner",
            "owner_scope",
            "org_id",
            "team_id",
            "user_id",
            "assistant_id",
        ),
        Index(
            "ix_integration_connections_app_status",
            "canonical_app_slug",
            "status",
        ),
    )


class ProviderActionAudit(Base):
    """Redacted audit log for every provider-backed runtime invocation."""

    __tablename__ = "provider_action_audits"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, nullable=True, index=True)
    team_id = Column(Integer, nullable=True, index=True)
    user_id = Column(String, nullable=True, index=True)
    assistant_id = Column(Integer, nullable=True, index=True)
    conversation_id = Column(String, nullable=True, index=True)
    connection_id = Column(String, nullable=True, index=True)
    provider_connection_id = Column(String, nullable=True, index=True)
    backend_id = Column(String, nullable=False, index=True)
    canonical_app_slug = Column(String, nullable=False, index=True)
    tool_id = Column(String, nullable=True, index=True)
    provider_action_id = Column(String, nullable=False)
    provider_tool_id = Column(String, nullable=False)
    unify_tool_id = Column(String, nullable=False, index=True)
    action_class = Column(String, nullable=False, index=True)
    behavior_hints_json = Column(JSONB, nullable=False, server_default=JSON_EMPTY_ARRAY)
    status = Column(String, nullable=False, index=True)
    latency_ms = Column(Integer, nullable=True)
    arguments_hash = Column(String, nullable=True, index=True)
    arguments_summary_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_OBJECT,
    )
    approval_scope = Column(String, nullable=True, index=True)
    approval_level = Column(String, nullable=True)
    approved_by = Column(String, nullable=True)
    denied_by = Column(String, nullable=True)
    approved_at = Column(TIMESTAMP(timezone=True), nullable=True)
    denied_at = Column(TIMESTAMP(timezone=True), nullable=True)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)
    redacted_input_summary = Column(Text, nullable=True)
    redacted_output_summary = Column(Text, nullable=True)
    error_code = Column(String, nullable=True)
    provider_status_code = Column(Integer, nullable=True)
    provider_response_body = Column(Text, nullable=True)
    provider_request_summary_json = Column(
        JSONB,
        nullable=False,
        server_default=JSON_EMPTY_OBJECT,
    )
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
