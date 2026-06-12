"""Provider-backed integration APIs.

Console uses these endpoints for gallery, connect, reconnect, disconnect, and
health states. Unity runtime uses the same contract for effective connection
discovery, provider tool search, schema lookup, and governed invocation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.integrations.operations import (
    OwnerContext,
    approve_tool_execution,
    cancel_connection,
    complete_connection,
    complete_connection_by_provider_connection_id,
    deny_tool_execution,
    disconnect_connection,
    get_app_detail,
    get_apps,
    get_connection_tool_policy,
    get_tool_schema,
    get_tools,
    list_connections,
    patch_connection_tool_policy,
    reconnect_connection,
    run_tool,
    search_apps,
    search_tools,
    seed_default_provider_catalog,
    start_connection,
)
from orchestra.web.api.integrations.operations import (
    sync_integrations as sync_integrations_operation,
)
from orchestra.web.api.integrations.operations import test_connection, update_connection
from orchestra.web.api.integrations.schema import (
    IntegrationAppDetailResponse,
    IntegrationBackendCreate,
    IntegrationBackendPatchRequest,
    IntegrationBackendResponse,
    IntegrationBackendStatusResponse,
    IntegrationBootstrapStateRequest,
    IntegrationBootstrapStateResponse,
    IntegrationCatalogSyncRequest,
    IntegrationCatalogSyncResponse,
    IntegrationConnectCompleteByProviderRequest,
    IntegrationConnectCompleteRequest,
    IntegrationConnectionPatchRequest,
    IntegrationConnectionResponse,
    IntegrationConnectStartRequest,
    IntegrationConnectStartResponse,
    IntegrationHealthResponse,
    IntegrationToolExecutionApprovalRequest,
    IntegrationToolExecutionApprovalResponse,
    IntegrationToolPolicyPatchRequest,
    IntegrationToolPolicyResponse,
    ProviderAppDetailLevel,
    ProviderAppGetRequest,
    ProviderAppGetResponse,
    ProviderAppSearchRequest,
    ProviderAppSearchResult,
    ProviderAppStatus,
    ProviderAppStatusGroup,
    ProviderToolGetRequest,
    ProviderToolGetResponse,
    ProviderToolRunRequest,
    ProviderToolRunResponse,
    ProviderToolSchemaResponse,
    ProviderToolSearchRequest,
    ProviderToolSearchResult,
)

router = APIRouter(prefix="/integrations", tags=["Integrations"])
admin_router = APIRouter(prefix="/integrations", tags=["Integration Admin"])

_APP_STATUS_ADAPTER = TypeAdapter(list[ProviderAppStatus])
_APP_STATUS_GROUP_ADAPTER = TypeAdapter(list[ProviderAppStatusGroup])


def _split_query_list(values: list[str] | None) -> list[str]:
    if not values:
        return []
    parsed: list[str] = []
    for value in values:
        parsed.extend(part.strip() for part in value.split(",") if part.strip())
    return parsed


def _validate_app_statuses(values: list[str] | None) -> list[ProviderAppStatus]:
    try:
        return _APP_STATUS_ADAPTER.validate_python(_split_query_list(values))
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc


def _validate_app_status_groups(
    values: list[str] | None,
) -> list[ProviderAppStatusGroup]:
    try:
        return _APP_STATUS_GROUP_ADAPTER.validate_python(_split_query_list(values))
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc


def _owner_from_query(
    owner_scope: str = "assistant",
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> OwnerContext:
    return OwnerContext(
        owner_scope=owner_scope,
        org_id=org_id,
        team_id=team_id,
        user_id=user_id,
        assistant_id=assistant_id,
    )


def _optional_owner_from_query(
    owner_scope: str | None = None,
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> OwnerContext | None:
    if not any(
        [
            org_id is not None,
            team_id is not None,
            user_id,
            assistant_id is not None,
        ],
    ):
        return None
    return _owner_from_query(
        owner_scope or "assistant",
        org_id,
        team_id,
        user_id,
        assistant_id,
    )


def _bootstrap_state_response(state) -> IntegrationBootstrapStateResponse:
    desired_config = state.desired_config_json or {}
    sync_config = desired_config.get("sync") if isinstance(desired_config, dict) else {}
    if not isinstance(sync_config, dict):
        sync_config = {}
    diagnostics = state.last_sync_diagnostics_json or {}
    sync_mode = diagnostics.get("sync_mode") or sync_config.get("mode")
    requested_app_slugs = (
        diagnostics.get("requested_app_slugs") or sync_config.get("app_slugs") or []
    )
    return IntegrationBootstrapStateResponse(
        id=state.id,
        environment=state.environment,
        backend_id=state.backend_id,
        desired_hash=state.desired_hash,
        desired_config=desired_config,
        last_status=state.last_status,
        last_error=state.last_error,
        apps_upserted=state.apps_upserted,
        tools_upserted=state.tools_upserted,
        sync_mode=str(sync_mode) if sync_mode else None,
        requested_app_slugs=[str(slug) for slug in requested_app_slugs],
        matched_app_slugs=[
            str(slug) for slug in diagnostics.get("matched_app_slugs", [])
        ],
        skipped_apps=diagnostics.get("skipped_apps", []),
        auth_configs_created=int(diagnostics.get("auth_configs_created", 0) or 0),
        auth_configs_reused=int(diagnostics.get("auth_configs_reused", 0) or 0),
        cache_version=diagnostics.get("cache_version"),
        last_sync_warning=diagnostics.get("warning")
        or diagnostics.get("last_sync_warning"),
        last_sync_diagnostics=diagnostics,
        last_synced_at=state.last_synced_at,
        created_at=state.created_at,
        updated_at=state.updated_at,
    )


def _backend_status_response(
    *,
    backend,
    state,
    catalog_counts: dict[str, dict[str, int]],
) -> IntegrationBackendStatusResponse:
    bootstrap_state = _bootstrap_state_response(state) if state else None
    counts = catalog_counts.get(backend.backend_id, {})
    return IntegrationBackendStatusResponse(
        backend=IntegrationBackendResponse.model_validate(backend),
        bootstrap_state=bootstrap_state,
        catalog_app_count=int(counts.get("apps", 0)),
        catalog_tool_count=int(counts.get("tools", 0)),
        desired_hash=bootstrap_state.desired_hash if bootstrap_state else None,
        sync_mode=bootstrap_state.sync_mode if bootstrap_state else None,
        requested_app_slugs=(
            bootstrap_state.requested_app_slugs if bootstrap_state else []
        ),
        matched_app_slugs=bootstrap_state.matched_app_slugs if bootstrap_state else [],
        skipped_apps=bootstrap_state.skipped_apps if bootstrap_state else [],
        last_status=bootstrap_state.last_status if bootstrap_state else None,
        last_error=bootstrap_state.last_error if bootstrap_state else None,
        last_sync_warning=(
            bootstrap_state.last_sync_warning if bootstrap_state else None
        ),
        apps_upserted=bootstrap_state.apps_upserted if bootstrap_state else 0,
        tools_upserted=bootstrap_state.tools_upserted if bootstrap_state else 0,
        last_synced_at=bootstrap_state.last_synced_at if bootstrap_state else None,
    )


@admin_router.get("/backends")
def get_integration_backends(
    session: Session = Depends(get_db_session),
) -> list[IntegrationBackendResponse]:
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    return [
        IntegrationBackendResponse.model_validate(backend)
        for backend in dao.list_backends()
    ]


@admin_router.get("/backends/status")
def get_integration_backend_status(
    environment: str | None = None,
    session: Session = Depends(get_db_session),
) -> list[IntegrationBackendStatusResponse]:
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    states_by_backend: dict[object, object] = {}
    for state in dao.list_bootstrap_states(environment=environment):
        states_by_backend.setdefault(state.backend_id, state)
        if environment is None:
            states_by_backend[(state.backend_id, state.environment)] = state
    catalog_counts = dao.catalog_counts_by_backend()
    responses: list[IntegrationBackendStatusResponse] = []
    for backend in dao.list_backends():
        state = states_by_backend.get(backend.backend_id)
        if environment is None:
            state = (
                states_by_backend.get((backend.backend_id, backend.environment))
                or state
            )
        responses.append(
            _backend_status_response(
                backend=backend,
                state=state,
                catalog_counts=catalog_counts,
            ),
        )
    return responses


@admin_router.post("/backends")
def put_integration_backend(
    body: IntegrationBackendCreate,
    session: Session = Depends(get_db_session),
) -> IntegrationBackendResponse:
    dao = IntegrationProviderDAO(session)
    backend = dao.upsert_backend(body.model_dump())
    session.commit()
    return IntegrationBackendResponse.model_validate(backend)


@admin_router.patch("/backends/{backend_id}")
def patch_integration_backend(
    backend_id: str,
    body: IntegrationBackendPatchRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationBackendResponse:
    try:
        seed_default_provider_catalog(session)
        dao = IntegrationProviderDAO(session)
        backend = dao.patch_backend(backend_id, body.model_dump(exclude_none=True))
        session.commit()
        return IntegrationBackendResponse.model_validate(
            backend,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


@admin_router.get("/bootstrap-state")
def get_integration_bootstrap_state(
    environment: str,
    backend_id: str,
    session: Session = Depends(get_db_session),
) -> IntegrationBootstrapStateResponse:
    state = IntegrationProviderDAO(session).get_bootstrap_state(
        environment=environment,
        backend_id=backend_id,
    )
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Unknown integration bootstrap state: " f"{environment}/{backend_id}"
            ),
        )
    return _bootstrap_state_response(state)


@admin_router.put("/bootstrap-state")
def put_integration_bootstrap_state(
    body: IntegrationBootstrapStateRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationBootstrapStateResponse:
    payload = body.model_dump()
    environment = payload.pop("environment")
    backend_id = payload.pop("backend_id")
    desired_config = payload.pop("desired_config")
    diagnostics = payload.pop("last_sync_diagnostics", {}) or {}
    values = {
        **payload,
        "desired_config_json": desired_config,
        "last_sync_diagnostics_json": diagnostics,
    }
    if body.last_status == "success":
        values["last_synced_at"] = datetime.now(timezone.utc)
    state = IntegrationProviderDAO(session).upsert_bootstrap_state(
        environment=environment,
        backend_id=backend_id,
        values=values,
    )
    session.commit()
    return _bootstrap_state_response(state)


@admin_router.post("/sync")
def sync_integrations(
    body: IntegrationCatalogSyncRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationCatalogSyncResponse:
    """Sync integrations through one admin contract.

    Native/custom publishers pass normalized ``apps``/``tools`` directly.
    Provider live syncs pass ``backend_id`` plus bounded provider options; the
    provider-specific API pagination and normalization stays inside Orchestra.
    """

    return sync_integrations_operation(session, body)


@router.get("/apps")
def get_integration_apps(
    query: str | None = Query(None),
    source_type: str | None = None,
    status: list[str] | None = Query(None),
    status_group: list[str] | None = Query(None),
    detail_level: ProviderAppDetailLevel = Query("full"),
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
) -> ProviderAppGetResponse:
    return get_apps(
        session,
        ProviderAppGetRequest(
            query=query,
            source_type=source_type,
            status=_validate_app_statuses(status),
            status_group=_validate_app_status_groups(status_group),
            detail_level=detail_level,
            owner_scope=owner_scope,
            org_id=org_id,
            team_id=team_id,
            user_id=user_id,
            assistant_id=assistant_id,
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/apps/search")
def search_integration_apps(
    query: str | None = Query(None),
    source_type: str | None = None,
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
) -> list[ProviderAppSearchResult]:
    return search_apps(
        session,
        ProviderAppSearchRequest(
            query=query,
            source_type=source_type,
            owner_scope=owner_scope,
            org_id=org_id,
            team_id=team_id,
            user_id=user_id,
            assistant_id=assistant_id,
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/apps/{canonical_app_slug}")
def get_integration_app_detail(
    canonical_app_slug: str,
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    session: Session = Depends(get_db_session),
) -> IntegrationAppDetailResponse:
    try:
        return get_app_detail(
            session,
            canonical_app_slug=canonical_app_slug,
            owner=_owner_from_query(
                owner_scope,
                org_id,
                team_id,
                user_id,
                assistant_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


@router.get("/connections")
def get_integration_connections(
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    include_disconnected: bool = False,
    session: Session = Depends(get_db_session),
) -> list[IntegrationConnectionResponse]:
    return list_connections(
        session,
        _owner_from_query(owner_scope, org_id, team_id, user_id, assistant_id),
        include_disconnected=include_disconnected,
    )


@router.post("/connect/start")
def start_integration_connect(
    body: IntegrationConnectStartRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationConnectStartResponse:
    try:
        (
            connection,
            connect_url,
            auth_mode,
            requires_browser_redirect,
            requested_scopes,
        ) = start_connection(
            session,
            owner=OwnerContext(
                owner_scope=body.owner_scope,
                org_id=body.org_id,
                team_id=body.team_id,
                user_id=body.user_id,
                assistant_id=body.assistant_id,
            ),
            canonical_app_slug=body.canonical_app_slug,
            backend_id=body.backend_id,
            requested_scopes=body.requested_scopes,
            auth_mode=body.auth_mode,
            api_key_fields=body.api_key_fields,
            created_by=body.created_by,
            redirect_url=body.redirect_url,
            account_label=body.account_label,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return IntegrationConnectStartResponse(
        connection=connection,
        connect_url=connect_url,
        auth_mode=auth_mode,
        requires_browser_redirect=requires_browser_redirect,
        requested_scopes=requested_scopes,
    )


@router.post("/connections/complete-by-provider")
def complete_integration_connect_by_provider(
    body: IntegrationConnectCompleteByProviderRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationConnectionResponse:
    try:
        return complete_connection_by_provider_connection_id(
            session,
            provider_connection_id=body.provider_connection_id,
            owner=OwnerContext(
                owner_scope=body.owner_scope,
                org_id=body.org_id,
                team_id=body.team_id,
                user_id=body.user_id,
                assistant_id=body.assistant_id,
            ),
            granted_scopes=body.granted_scopes,
            external_account_label=body.external_account_label,
            status=body.status,
            reconnect_reason=body.reconnect_reason,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/connections/{connection_id}/complete")
def complete_integration_connect(
    connection_id: str,
    body: IntegrationConnectCompleteRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationConnectionResponse:
    try:
        return complete_connection(
            session,
            connection_id=connection_id,
            provider_connection_id=body.provider_connection_id,
            granted_scopes=body.granted_scopes,
            external_account_label=body.external_account_label,
            status=body.status,
            reconnect_reason=body.reconnect_reason,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/connections/{connection_id}/disconnect")
def disconnect_integration(
    connection_id: str,
    session: Session = Depends(get_db_session),
) -> IntegrationConnectionResponse:
    try:
        return disconnect_connection(session, connection_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/connections/{connection_id}/cancel")
def cancel_integration(
    connection_id: str,
    session: Session = Depends(get_db_session),
) -> IntegrationConnectionResponse:
    try:
        return cancel_connection(session, connection_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/connections/{connection_id}/reconnect")
def reconnect_integration(
    connection_id: str,
    session: Session = Depends(get_db_session),
) -> IntegrationConnectionResponse:
    try:
        return reconnect_connection(session, connection_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.patch("/connections/{connection_id}")
def patch_integration_connection(
    connection_id: str,
    body: IntegrationConnectionPatchRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationConnectionResponse:
    try:
        return update_connection(
            session,
            connection_id,
            account_label=body.account_label,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/connections/{connection_id}/test")
def test_integration_connection(
    connection_id: str,
    session: Session = Depends(get_db_session),
) -> IntegrationHealthResponse:
    try:
        connection, health = test_connection(session, connection_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return IntegrationHealthResponse(
        connection_id=connection.connection_id,
        status=connection.status,
        health=health,
        reconnect_reason=connection.reconnect_reason,
    )


@router.get("/connections/{connection_id}/tool-policy")
def get_integration_tool_policy(
    connection_id: str,
    owner_scope: str | None = None,
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    session: Session = Depends(get_db_session),
) -> IntegrationToolPolicyResponse:
    try:
        owner = _optional_owner_from_query(
            owner_scope,
            org_id,
            team_id,
            user_id,
            assistant_id,
        )
        return get_connection_tool_policy(session, connection_id, owner=owner)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


@router.patch("/connections/{connection_id}/tool-policy")
def patch_integration_tool_policy(
    connection_id: str,
    body: IntegrationToolPolicyPatchRequest,
    owner_scope: str | None = None,
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    session: Session = Depends(get_db_session),
) -> IntegrationToolPolicyResponse:
    try:
        owner = _optional_owner_from_query(
            owner_scope,
            org_id,
            team_id,
            user_id,
            assistant_id,
        )
        return patch_connection_tool_policy(session, connection_id, body, owner=owner)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


@router.post(
    "/tool-executions/{audit_id}/approve",
    response_model=IntegrationToolExecutionApprovalResponse,
)
def approve_integration_tool_execution(
    audit_id: int,
    body: IntegrationToolExecutionApprovalRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationToolExecutionApprovalResponse:
    try:
        return approve_tool_execution(session, audit_id, body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


@router.post(
    "/tool-executions/{audit_id}/deny",
    response_model=IntegrationToolExecutionApprovalResponse,
)
def deny_integration_tool_execution(
    audit_id: int,
    body: IntegrationToolExecutionApprovalRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationToolExecutionApprovalResponse:
    try:
        return deny_tool_execution(session, audit_id, body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


@router.get("/tools/search", response_model_exclude_none=True)
def search_provider_tools(
    query: str | None = Query(None),
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    canonical_app_slug: str | None = None,
    include_unconnected: bool = False,
    include_schema: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
) -> list[ProviderToolSearchResult]:
    return search_tools(
        session,
        ProviderToolSearchRequest(
            query=query,
            owner_scope=owner_scope,
            org_id=org_id,
            team_id=team_id,
            user_id=user_id,
            assistant_id=assistant_id,
            canonical_app_slug=canonical_app_slug,
            include_unconnected=include_unconnected,
            include_schema=include_schema,
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/tools", response_model_exclude_none=True)
def get_provider_tools(
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    canonical_app_slug: str | None = None,
    activation_state: str | None = None,
    include_unconnected: bool = False,
    include_schema: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
) -> ProviderToolGetResponse:
    return get_tools(
        session,
        ProviderToolGetRequest(
            owner_scope=owner_scope,
            org_id=org_id,
            team_id=team_id,
            user_id=user_id,
            assistant_id=assistant_id,
            canonical_app_slug=canonical_app_slug,
            activation_state=activation_state,
            include_unconnected=include_unconnected,
            include_schema=include_schema,
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/tools/{tool_id}/schema")
def read_provider_tool_schema(
    tool_id: str,
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    session: Session = Depends(get_db_session),
) -> ProviderToolSchemaResponse:
    try:
        return get_tool_schema(
            session,
            tool_id=tool_id,
            owner=_owner_from_query(
                owner_scope,
                org_id,
                team_id,
                user_id,
                assistant_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/tools/{tool_id}/run")
def run_provider_tool(
    tool_id: str,
    body: ProviderToolRunRequest,
    session: Session = Depends(get_db_session),
) -> ProviderToolRunResponse:
    try:
        return run_tool(session, tool_id=tool_id, body=body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
