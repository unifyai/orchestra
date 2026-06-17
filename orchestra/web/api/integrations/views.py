"""Provider-backed integration APIs.

Console uses these endpoints for gallery, connect, reconnect, disconnect, and
health states. Unity runtime uses the same contract for effective connection
discovery, provider tool search, schema lookup, and governed invocation.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.integration_provider_models import DynamicProviderApp
from orchestra.web.api.integrations.operations import (
    OwnerContext,
    _activation_state,
    _best_connection,
    _tool_search_result,
    approve_tool_execution,
    cancel_connection,
    complete_connection,
    complete_connection_by_provider_connection_id,
    deny_tool_execution,
    disconnect_connection,
    get_connection_tool_policy,
    list_connections,
    patch_connection_tool_policy,
    reconnect_connection,
    run_tool,
    seed_default_provider_catalog,
    start_connection,
)
from orchestra.web.api.integrations.operations import (
    sync_integrations as sync_integrations_operation,
)
from orchestra.web.api.integrations.operations import test_connection, update_connection
from orchestra.web.api.integrations.schema import (
    BuiltinsIntegrationSyncRequest,
    BuiltinsIntegrationSyncResponse,
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
    ProviderToolRunRequest,
    ProviderToolRunResponse,
    ProviderToolSearchResult,
)

router = APIRouter(prefix="/integrations", tags=["Integrations"])
admin_router = APIRouter(prefix="/integrations", tags=["Integration Admin"])


def _tool_result_payload(result: ProviderToolSearchResult) -> dict:
    return result.model_dump(exclude_none=True)


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


def _app_status(conn, tools) -> str:
    if conn is None:
        return "not_connected"
    if conn.status == "pending":
        return "pending"
    if conn.status != "connected":
        return conn.status
    if conn.credential_storage == "secret_manager":
        return "configured"
    granted = set(conn.granted_scopes_json or [])
    for tool in tools:
        required = set(tool.required_scopes_json or [])
        if required and not required.issubset(granted):
            return "missing_scope"
    return "connected"


def _app_status_group(status_value: str) -> str:
    if status_value in {"connected", "configured"}:
        return "connected"
    if status_value in {"pending", "missing_scope", "expired", "error"}:
        return "needs_attention"
    return "not_connected"


def _app_payload(
    dao: IntegrationProviderDAO,
    app: DynamicProviderApp,
    conn,
    *,
    detail_level: str,
) -> dict:
    tools = dao.list_tools(
        canonical_app_slug=app.canonical_app_slug,
        backend_id=app.backend_id,
    )
    status_value = _app_status(conn, tools)
    raw_metadata = app.raw_provider_metadata_json or {}
    is_native = app.backend_id == "unity_native" or (
        isinstance(raw_metadata, dict) and raw_metadata.get("source_type") == "native"
    )
    payload = {
        "backend_id": app.backend_id,
        "provider_app_id": app.provider_app_id,
        "canonical_app_slug": app.canonical_app_slug,
        "display_name": app.display_name,
        "source_type": "native" if is_native else "third_party",
        "source_label": "Native" if app.backend_id == "unity_native" else "Third-party",
        "description": app.description,
        "category": app.category,
        "icon_url": app.icon_url,
        "auth_modes": app.auth_modes or [],
        "tool_count": len(tools),
        "connection_status": status_value,
        "connection_id": conn.connection_id if conn else None,
        "external_account_label": conn.external_account_label if conn else None,
        "overlay": {},
        "native_metadata": (
            raw_metadata.get("native_metadata", {})
            if is_native and isinstance(raw_metadata, dict)
            else {}
        ),
    }
    if detail_level != "summary":
        payload["available_scopes"] = []
        payload["available_actions"] = [
            {
                "tool_id": tool.tool_id,
                "name": tool.name,
                "display_name": tool.display_name,
                "activation_state": _activation_state(tool, conn),
                "action_class": tool.action_class,
            }
            for tool in tools
        ]
    else:
        payload["available_scopes"] = []
        payload["available_actions"] = []
    return payload


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


@admin_router.post("/builtins-sync/start")
def start_builtins_integration_sync(
    request: Request,
    body: BuiltinsIntegrationSyncRequest,
) -> BuiltinsIntegrationSyncResponse:
    """Run the Builtins-context catalog materializer.

    This inline API path is for local/admin fallback only. Hosted production
    deployments should execute the standalone worker in a Cloud Run Job. Set
    ``ORCHESTRA_BUILTINS_SYNC_INLINE_ENABLED=false`` to fail this endpoint
    loudly instead of routing heavy sync work into the API process.
    """

    try:
        if os.getenv("ORCHESTRA_BUILTINS_SYNC_INLINE_ENABLED", "true").lower() in {
            "0",
            "false",
            "no",
        }:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Inline Builtins integrations artifact seeding is disabled. Select and run "
                    "the configured deployment executor instead."
                ),
            )
        from orchestra.services.builtins_integration_sync import (
            BuiltinsSyncRequest,
            run_builtins_sync,
        )

        sync_request = BuiltinsSyncRequest.from_payload(body.model_dump())
        result = run_builtins_sync(
            request.app.state.db_session_factory,
            sync_request,
        )
        return BuiltinsIntegrationSyncResponse(**result.to_payload())
    except HTTPException:
        raise
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


@router.get("/apps")
def list_integration_apps(
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    query: str = "",
    source_type: str | None = None,
    status: list[str] | None = Query(None),
    status_group: str | None = None,
    detail_level: str = "full",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict:
    from fastapi import status as http_status

    valid_statuses = {
        "connected",
        "configured",
        "pending",
        "missing_scope",
        "not_connected",
        "expired",
        "error",
    }
    valid_groups = {"connected", "needs_attention", "not_connected"}
    requested_statuses: set[str] = set()
    for raw_status in status or []:
        requested_statuses.update(
            part.strip() for part in raw_status.split(",") if part.strip()
        )
    if requested_statuses.difference(valid_statuses):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_status",
        )
    if status_group is not None and status_group not in valid_groups:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_status_group",
        )

    owner = _owner_from_query(owner_scope, org_id, team_id, user_id, assistant_id)
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    enabled_backend_ids = dao.active_backend_ids()
    apps = (
        session.query(DynamicProviderApp)
        .order_by(DynamicProviderApp.display_name.asc())
        .all()
    )
    query_norm = query.strip().lower()
    all_items: list[dict] = []
    for app in apps:
        if app.backend_id not in enabled_backend_ids:
            continue
        conn = _best_connection(
            session,
            owner=owner,
            canonical_app_slug=app.canonical_app_slug,
            backend_id=app.backend_id,
        )
        item = _app_payload(dao, app, conn, detail_level=detail_level)
        all_items.append(item)

    base_filtered = []
    for item in all_items:
        if source_type and item["source_type"] != source_type:
            continue
        if (
            query_norm
            and query_norm
            not in f"{item['display_name']} {item['canonical_app_slug']}".lower()
        ):
            continue
        base_filtered.append(item)

    facets = {
        "total": len(base_filtered),
        "source_type": {
            "native": sum(
                1 for item in base_filtered if item["source_type"] == "native"
            ),
            "third_party": sum(
                1 for item in base_filtered if item["source_type"] == "third_party"
            ),
        },
        "status": {status_name: 0 for status_name in valid_statuses},
        "status_group": {group_name: 0 for group_name in valid_groups},
    }
    for item in base_filtered:
        facets["status"][item["connection_status"]] = (
            facets["status"].get(item["connection_status"], 0) + 1
        )
        group = _app_status_group(item["connection_status"])
        facets["status_group"][group] = facets["status_group"].get(group, 0) + 1

    filtered = []
    for item in base_filtered:
        if requested_statuses and item["connection_status"] not in requested_statuses:
            continue
        if (
            status_group
            and _app_status_group(item["connection_status"]) != status_group
        ):
            continue
        filtered.append(item)

    return {
        "items": filtered[offset : offset + limit],
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "facets": facets,
    }


@router.get("/apps/search")
def search_integration_apps(
    query: str = Query(""),
    source_type: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_db_session),
) -> list[dict]:
    page = list_integration_apps(
        owner_scope="assistant",
        org_id=None,
        team_id=None,
        user_id=None,
        assistant_id=None,
        query=query,
        source_type=source_type,
        status=None,
        status_group=None,
        detail_level="summary",
        limit=limit,
        offset=0,
        session=session,
    )
    return page["items"]


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


@router.get("/tools")
def list_provider_tools(
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    canonical_app_slug: str | None = None,
    activation_state: str | None = None,
    include_schema: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict:
    owner = _owner_from_query(owner_scope, org_id, team_id, user_id, assistant_id)
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    enabled_backend_ids = dao.active_backend_ids()
    tools = dao.list_tools(canonical_app_slug=canonical_app_slug)
    items: list[ProviderToolSearchResult] = []
    for tool in tools:
        if tool.backend_id not in enabled_backend_ids:
            continue
        app = dao.get_app_by_slug(tool.canonical_app_slug, backend_id=tool.backend_id)
        conn = _best_connection(
            session,
            owner=owner,
            canonical_app_slug=tool.canonical_app_slug,
            backend_id=tool.backend_id,
        )
        item = _tool_search_result(
            tool=tool,
            app=app,
            conn=conn,
            activation_state=_activation_state(tool, conn),
            match_reason="list",
            score=1.0,
            include_schema=include_schema,
        )
        if activation_state and item.activation_state != activation_state:
            continue
        items.append(item)
    total = len(items)
    return {
        "items": [
            _tool_result_payload(item) for item in items[offset : offset + limit]
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/tools/search")
def search_provider_tools(
    query: str = Query(""),
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    include_unconnected: bool = False,
    include_schema: bool = False,
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_db_session),
) -> list[dict]:
    owner = _owner_from_query(owner_scope, org_id, team_id, user_id, assistant_id)
    seed_default_provider_catalog(session)
    dao = IntegrationProviderDAO(session)
    enabled_backend_ids = dao.active_backend_ids()
    query_norm = query.strip().lower()
    results: list[ProviderToolSearchResult] = []
    for tool in dao.list_tools():
        if tool.backend_id not in enabled_backend_ids:
            continue
        app = dao.get_app_by_slug(tool.canonical_app_slug, backend_id=tool.backend_id)
        haystack = " ".join(
            [
                tool.name,
                tool.display_name,
                tool.description,
                tool.canonical_app_slug,
                app.display_name if app else "",
            ],
        ).lower()
        if query_norm and query_norm not in haystack:
            continue
        conn = _best_connection(
            session,
            owner=owner,
            canonical_app_slug=tool.canonical_app_slug,
            backend_id=tool.backend_id,
        )
        item = _tool_search_result(
            tool=tool,
            app=app,
            conn=conn,
            activation_state=_activation_state(tool, conn),
            match_reason="search",
            score=1.0,
            include_schema=include_schema,
        )
        if not include_unconnected and item.activation_state == "not_connected":
            continue
        results.append(item)
        if len(results) >= limit:
            break
    return [_tool_result_payload(result) for result in results]


@router.get("/tools/{tool_id}/schema")
def get_provider_tool_schema(
    tool_id: str,
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    session: Session = Depends(get_db_session),
) -> dict:
    _owner_from_query(owner_scope, org_id, team_id, user_id, assistant_id)
    seed_default_provider_catalog(session)
    tool = IntegrationProviderDAO(session).get_tool(tool_id)
    if not tool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown provider tool: {tool_id}",
        )
    return {
        "tool_id": tool.tool_id,
        "canonical_name": tool.canonical_name,
        "input_schema": tool.input_schema_json or {},
        "output_schema": tool.output_schema_json or {},
        "examples": tool.examples_json or [],
    }


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
