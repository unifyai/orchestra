"""Schemas for provider-backed integration APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

BackendKind = Literal["composio", "pipedream", "first_party", "custom"]
BackendStatus = Literal["enabled", "disabled"]
ConnectionStatus = Literal[
    "connected",
    "pending",
    "missing_secrets",
    "expired",
    "revoked",
    "disconnected",
    "error",
]
ActivationState = Literal[
    "connected_ready",
    "not_connected",
    "missing_scope",
    "disabled_by_policy",
    "expired",
    "error",
]
ActionClass = Literal["read", "write", "destructive", "bulk_export", "sensitive_read"]
OwnerScope = Literal["org", "team", "user", "assistant"]
ToolApprovalLevel = Literal["auto", "specific_approval", "forbidden"]
IntegrationSourceType = Literal["native", "third_party"]


class IntegrationBackendCreate(BaseModel):
    """Admin-managed provider backend row.

    Provider credentials and endpoints come from Orchestra deployment
    environment variables. ``config_json`` is for non-secret operational knobs
    such as timeout and pagination limits.
    """

    backend_id: str
    kind: BackendKind
    environment: str = "prod"
    display_name: str
    status: BackendStatus = "enabled"
    credentials_secret_ref: Optional[str] = None
    webhook_secret_ref: Optional[str] = None
    allowed_orgs_or_tenants: list[str] = Field(default_factory=list)
    default_priority: int = 100
    config_json: dict[str, Any] = Field(default_factory=dict)


class IntegrationBackendResponse(IntegrationBackendCreate):
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class IntegrationBackendPatchRequest(BaseModel):
    """Partial backend update for enable/disable and operational config changes."""

    status: Optional[BackendStatus] = None
    credentials_secret_ref: Optional[str] = None
    webhook_secret_ref: Optional[str] = None
    allowed_orgs_or_tenants: Optional[list[str]] = None
    default_priority: Optional[int] = None
    config_json: Optional[dict[str, Any]] = None


class DynamicIntegrationAppResponse(BaseModel):
    backend_id: str
    provider_app_id: str
    canonical_app_slug: str
    display_name: str
    source_type: IntegrationSourceType = "third_party"
    source_label: str = "Third-party"
    description: Optional[str] = None
    category: Optional[str] = None
    icon_url: Optional[str] = None
    auth_modes: list[str] = Field(default_factory=list)
    available_scopes: list[dict[str, Any]] = Field(default_factory=list)
    available_actions: list[Any] = Field(default_factory=list)
    tool_count: int = 0
    api_key_schema: Optional[dict[str, Any]] = None
    connection_status: Optional[ConnectionStatus] = None
    connection_id: Optional[str] = None
    external_account_label: Optional[str] = None
    overlay: dict[str, Any] = Field(default_factory=dict)
    native_metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderAppGetRequest(BaseModel):
    query: str = ""
    source_type: Optional[IntegrationSourceType] = None
    owner_scope: OwnerScope = "assistant"
    org_id: Optional[int] = None
    team_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    limit: int = Field(100, ge=1, le=500)
    offset: int = Field(0, ge=0)


class ProviderAppGetResponse(BaseModel):
    items: list[DynamicIntegrationAppResponse] = Field(default_factory=list)
    total: int = 0
    limit: int
    offset: int


class ProviderAppSearchRequest(BaseModel):
    query: str = ""
    source_type: Optional[IntegrationSourceType] = None
    owner_scope: OwnerScope = "assistant"
    org_id: Optional[int] = None
    team_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    limit: int = Field(10, ge=1, le=100)
    offset: int = Field(0, ge=0)


class ProviderAppSearchResult(DynamicIntegrationAppResponse):
    supported: bool = True
    score: float = 0.0
    match_reason: str = ""


class IntegrationConnectionResponse(BaseModel):
    connection_id: str
    owner_scope: OwnerScope
    org_id: Optional[int] = None
    team_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    canonical_app_slug: str
    backend_id: str
    provider_app_id: str
    provider_connection_id: Optional[str] = None
    status: ConnectionStatus
    external_account_label: Optional[str] = None
    granted_scopes: list[str] = Field(default_factory=list)
    enabled_capabilities: list[str] = Field(default_factory=list)
    disabled_actions: list[str] = Field(default_factory=list)
    tool_policy: dict[str, ToolApprovalLevel] = Field(default_factory=dict)
    credential_storage: str = "provider_vault"
    secret_refs: dict[str, Any] = Field(default_factory=dict)
    last_health_check_at: Optional[datetime] = None
    last_health_check_status: Optional[str] = None
    reconnect_reason: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class IntegrationConnectionPatchRequest(BaseModel):
    account_label: Optional[str] = None


class IntegrationConnectStartRequest(BaseModel):
    owner_scope: OwnerScope = "assistant"
    org_id: Optional[int] = None
    team_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    canonical_app_slug: str
    backend_id: Optional[str] = None
    requested_scopes: list[str] = Field(default_factory=list)
    auth_mode: Optional[str] = None
    api_key_fields: dict[str, str] = Field(default_factory=dict)
    created_by: Optional[str] = None
    redirect_url: Optional[str] = None
    account_label: Optional[str] = None


class IntegrationConnectStartResponse(BaseModel):
    connection: IntegrationConnectionResponse
    connect_url: Optional[str] = None
    auth_mode: str
    requires_browser_redirect: bool
    requested_scopes: list[str] = Field(default_factory=list)


class IntegrationConnectCompleteRequest(BaseModel):
    provider_connection_id: Optional[str] = None
    granted_scopes: list[str] = Field(default_factory=list)
    external_account_label: Optional[str] = None
    status: ConnectionStatus = "connected"
    reconnect_reason: Optional[str] = None


class IntegrationConnectCompleteByProviderRequest(BaseModel):
    provider_connection_id: str
    owner_scope: OwnerScope = "assistant"
    org_id: Optional[int] = None
    team_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    granted_scopes: list[str] = Field(default_factory=list)
    external_account_label: Optional[str] = None
    status: ConnectionStatus = "connected"
    reconnect_reason: Optional[str] = None


class IntegrationAppDetailResponse(DynamicIntegrationAppResponse):
    tools: list[dict[str, Any]] = Field(default_factory=list)
    derived_scopes: list[dict[str, Any]] = Field(default_factory=list)


class IntegrationHealthResponse(BaseModel):
    connection_id: str
    status: ConnectionStatus
    health: str
    reconnect_reason: Optional[str] = None


class ProviderCatalogSyncRequest(BaseModel):
    backend_id: str
    cache_version: str = "provider-sync-v1"
    source_type: IntegrationSourceType = "third_party"
    apps: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)


class ProviderCatalogSyncResponse(BaseModel):
    apps_upserted: int
    tools_upserted: int


class ComposioCatalogSyncRequest(BaseModel):
    app_slugs: list[str] = Field(default_factory=list)
    tool_limit_per_app: int = Field(0, ge=0, le=1000)
    include_all_managed_apps: bool = False
    create_auth_configs: bool = True
    cache_version: str = "composio-live-v1"


class ComposioCatalogSyncResponse(BaseModel):
    apps_upserted: int
    tools_upserted: int
    skipped_apps: list[dict[str, str]] = Field(default_factory=list)
    auth_configs_created: int = 0
    auth_configs_reused: int = 0
    cache_version: str = "composio-live-v1"


class PipedreamCatalogSyncRequest(BaseModel):
    app_slugs: list[str] = Field(default_factory=list)
    component_limit_per_app: int = Field(0, ge=0, le=1000)
    include_all_apps: bool = True
    cache_version: str = "pipedream-live-v1"


class PipedreamCatalogSyncResponse(BaseModel):
    apps_upserted: int
    tools_upserted: int
    skipped_apps: list[dict[str, str]] = Field(default_factory=list)
    cache_version: str = "pipedream-live-v1"


class ProviderToolSearchRequest(BaseModel):
    query: str = ""
    owner_scope: OwnerScope = "assistant"
    org_id: Optional[int] = None
    team_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    canonical_app_slug: Optional[str] = None
    include_unconnected: bool = False
    limit: int = Field(100, ge=1, le=500)
    offset: int = Field(0, ge=0)


class ProviderToolGetRequest(BaseModel):
    owner_scope: OwnerScope = "assistant"
    org_id: Optional[int] = None
    team_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    canonical_app_slug: Optional[str] = None
    activation_state: Optional[ActivationState] = None
    include_unconnected: bool = False
    limit: int = Field(100, ge=1, le=500)
    offset: int = Field(0, ge=0)


class ProviderToolSearchResult(BaseModel):
    tool_id: str
    backend_id: str
    provider_app_id: str
    provider_tool_id: str
    canonical_name: str
    function_manager_name: str
    app_slug: str
    app_display_name: str
    app_icon_url: Optional[str] = None
    tool_display_name: str
    description: str
    match_reason: str
    activation_state: ActivationState
    action_class: ActionClass
    required_scopes: list[str] = Field(default_factory=list)
    connection_id: Optional[str] = None
    confirmation_required: bool = False
    approval_level: ToolApprovalLevel = "auto"
    schema_available: bool = True
    score: float = 0.0


class ProviderToolGetResponse(BaseModel):
    items: list[ProviderToolSearchResult] = Field(default_factory=list)
    total: int = 0
    limit: int
    offset: int


class ProviderToolSchemaResponse(BaseModel):
    tool_id: str
    backend_id: str
    provider_app_id: str
    provider_tool_id: str
    canonical_name: str
    function_manager_name: str
    app_slug: str
    app_display_name: str
    app_icon_url: Optional[str] = None
    tool_display_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    required_scopes: list[str] = Field(default_factory=list)
    action_class: ActionClass
    confirmation_required: bool = False
    approval_level: ToolApprovalLevel = "auto"
    examples: list[dict[str, Any]] = Field(default_factory=list)
    activation_state: ActivationState
    connection_id: Optional[str] = None


class IntegrationToolPolicyItem(BaseModel):
    tool_id: str
    provider_tool_id: str
    canonical_name: str
    display_name: str
    action_class: ActionClass
    default_approval_level: ToolApprovalLevel
    approval_level: ToolApprovalLevel
    activation_state: ActivationState
    confirmation_required: bool = False


class IntegrationToolPolicyResponse(BaseModel):
    connection_id: str
    canonical_app_slug: str
    policies: list[IntegrationToolPolicyItem] = Field(default_factory=list)


class IntegrationToolPolicyPatchRequest(BaseModel):
    tool_policies: dict[str, ToolApprovalLevel] = Field(default_factory=dict)
    bulk_approval_level: Optional[ToolApprovalLevel] = None
    action_classes: list[ActionClass] = Field(default_factory=list)
    reset_to_defaults: bool = False


class ProviderToolRunRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    owner_scope: OwnerScope = "assistant"
    org_id: Optional[int] = None
    team_id: Optional[int] = None
    user_id: Optional[str] = None
    assistant_id: Optional[int] = None
    connection_id: Optional[str] = None
    conversation_id: Optional[str] = None
    confirmation_token: Optional[str] = None


class ProviderToolRunResponse(BaseModel):
    status: str
    activation_state: ActivationState
    tool_id: str
    connection_id: Optional[str] = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: Optional[dict[str, Any]] = None
    audit_id: Optional[int] = None
