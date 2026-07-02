"""Provider-backed integration APIs.

Console uses these endpoints for gallery, connect, reconnect, disconnect, and
health states. Unity runtime uses the same contract for effective connection
discovery, provider tool search, schema lookup, and governed invocation.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from sqlalchemy.orm import Session

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.integrations.operations import (
    OwnerContext,
    ProviderConnectError,
    approve_tool_execution,
    cancel_connection,
    complete_connection,
    complete_connection_by_provider_connection_id,
    delete_custom_auth_config,
    deny_tool_execution,
    disconnect_connection,
    get_connection_tool_policy,
    list_connections,
    list_custom_auth_configs,
    patch_connection_tool_policy,
    reconnect_connection,
    run_tool,
    seed_default_provider_catalog,
    stage_composio_file,
    set_custom_auth_config,
    start_connection,
    test_connection,
    update_connection,
)
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
    IntegrationCustomAuthConfigListResponse,
    IntegrationCustomAuthConfigRequest,
    IntegrationCustomAuthConfigResponse,
    IntegrationConnectCompleteByProviderRequest,
    IntegrationConnectCompleteRequest,
    IntegrationConnectionPatchRequest,
    IntegrationConnectionResponse,
    IntegrationConnectStartRequest,
    IntegrationConnectStartResponse,
    IntegrationComposioStageFileResponse,
    IntegrationHealthResponse,
    IntegrationToolExecutionApprovalRequest,
    IntegrationToolExecutionApprovalResponse,
    IntegrationToolPolicyPatchRequest,
    IntegrationToolPolicyResponse,
    ProviderToolRunRequest,
    ProviderToolRunResponse,
)

router = APIRouter(prefix="/integrations", tags=["Integrations"])
admin_router = APIRouter(prefix="/integrations", tags=["Integration Admin"])


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


def _catalog_route_removed() -> None:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Integration app and tool catalog reads use Builtins logs, not "
            "Orchestra integration catalog routes."
        ),
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
        run_id=diagnostics.get("run_id"),
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
) -> IntegrationBackendStatusResponse:
    bootstrap_state = _bootstrap_state_response(state) if state else None
    return IntegrationBackendStatusResponse(
        backend=IntegrationBackendResponse.model_validate(backend),
        bootstrap_state=bootstrap_state,
        catalog_app_count=bootstrap_state.apps_upserted if bootstrap_state else 0,
        catalog_tool_count=bootstrap_state.tools_upserted if bootstrap_state else 0,
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


@admin_router.get("/backends/{backend_id}/custom-auth")
def list_integration_custom_auth_configs(
    backend_id: str,
    session: Session = Depends(get_db_session),
) -> IntegrationCustomAuthConfigListResponse:
    """List operator-configured bring-your-own OAuth apps for a backend."""

    try:
        configs = list_custom_auth_configs(session, backend_id=backend_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return IntegrationCustomAuthConfigListResponse(
        backend_id=backend_id,
        configs=[IntegrationCustomAuthConfigResponse(**config) for config in configs],
    )


@admin_router.put("/backends/{backend_id}/custom-auth")
def put_integration_custom_auth_config(
    backend_id: str,
    body: IntegrationCustomAuthConfigRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationCustomAuthConfigResponse:
    """Register/replace a bring-your-own OAuth app for a toolkit.

    Client credentials are forwarded to the provider vault and never persisted
    by Orchestra.
    """

    try:
        entry = set_custom_auth_config(
            session,
            backend_id=backend_id,
            toolkit_slug=body.toolkit_slug,
            client_id=body.client_id,
            client_secret=body.client_secret,
            auth_scheme=body.auth_scheme,
            scopes=body.scopes,
            display_name=body.display_name,
            oauth_redirect_uri=body.oauth_redirect_uri,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001 — surface provider errors to admin
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Provider rejected the custom OAuth config: {exc}",
        ) from exc
    return IntegrationCustomAuthConfigResponse(**entry)


@admin_router.delete("/backends/{backend_id}/custom-auth/{toolkit_slug}")
def delete_integration_custom_auth_config(
    backend_id: str,
    toolkit_slug: str,
    session: Session = Depends(get_db_session),
) -> Response:
    """Remove a bring-your-own OAuth app for a toolkit."""

    try:
        delete_custom_auth_config(
            session,
            backend_id=backend_id,
            toolkit_slug=toolkit_slug,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    body: IntegrationCatalogSyncRequest,  # noqa: ARG001
) -> IntegrationCatalogSyncResponse:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Legacy integration catalog projection sync is removed. Seed "
            "Builtins Integrations/Apps and Integrations/Tools through the "
            "Builtins artifacts/logging path."
        ),
    )


@admin_router.post("/builtins-sync/start")
def start_builtins_integration_sync(
    request: Request,
    response: Response,
    body: BuiltinsIntegrationSyncRequest,
) -> BuiltinsIntegrationSyncResponse:
    """Start the Builtins-context catalog materializer.

    Hosted deployments launch the standalone worker in a dedicated Cloud Run
    Job (configured via ``ORCHESTRA_BUILTINS_SEED_JOB_NAME``): the request
    payload is persisted to GCS, ``bootstrap_state`` is seeded to ``running``
    with the new ``run_id``, the job is triggered, and ``202`` is returned so
    the caller polls ``bootstrap-state`` rather than holding a long connection.

    When no job is configured (self-host/local), the sync runs inline in this
    process and returns its terminal result. Set
    ``ORCHESTRA_BUILTINS_SYNC_INLINE_ENABLED=false`` to fail the inline path
    loudly instead of routing heavy sync work into the API process.
    """

    try:
        from orchestra.services.builtins_integration_sync import (
            BuiltinsSyncRequest,
            run_builtins_sync,
            write_bootstrap_state,
        )

        payload = body.model_dump()
        payload["run_id"] = str(payload.get("run_id") or uuid.uuid4())

        from orchestra.services.builtins_seed_launcher import (
            builtins_seed_job_configured,
            execute_seed_job,
            upload_seed_request,
        )

        if builtins_seed_job_configured():
            request_uri = upload_seed_request(payload)
            payload["request_uri"] = request_uri
            sync_request = BuiltinsSyncRequest.from_payload(payload)
            factory = request.app.state.db_session_factory
            with factory() as session:
                write_bootstrap_state(
                    session,
                    request=sync_request,
                    status="running",
                )
                session.commit()
            execute_seed_job(request_uri)
            response.status_code = status.HTTP_202_ACCEPTED
            return BuiltinsIntegrationSyncResponse(
                status="running",
                run_id=sync_request.run_id,
                desired_hash=sync_request.desired_hash,
                request_uri=request_uri,
            )

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

        sync_request = BuiltinsSyncRequest.from_payload(payload)
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
    owner_scope: str = Query("assistant"),  # noqa: ARG001
    org_id: int | None = None,  # noqa: ARG001
    team_id: int | None = None,  # noqa: ARG001
    user_id: str | None = None,  # noqa: ARG001
    assistant_id: int | None = None,  # noqa: ARG001
    query: str = "",  # noqa: ARG001
    source_type: str | None = None,  # noqa: ARG001
    status_filter: list[str] | None = Query(None, alias="status"),  # noqa: ARG001
    status_group: str | None = None,  # noqa: ARG001
    detail_level: str = "full",  # noqa: ARG001
    limit: int = Query(50, ge=1, le=200),  # noqa: ARG001
    offset: int = Query(0, ge=0),  # noqa: ARG001
) -> dict:
    _catalog_route_removed()


@router.get("/apps/search")
def search_integration_apps(
    query: str = Query(""),  # noqa: ARG001
    source_type: str | None = None,  # noqa: ARG001
    limit: int = Query(20, ge=1, le=100),  # noqa: ARG001
) -> list[dict]:
    _catalog_route_removed()


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
            provider_app_id=body.provider_app_id,
            requested_scopes=body.requested_scopes,
            auth_mode=body.auth_mode,
            api_key_fields=body.api_key_fields,
            created_by=body.created_by,
            redirect_url=body.redirect_url,
            account_label=body.account_label,
        )
    except ProviderConnectError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
        ) from exc
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
    owner_scope: str = Query("assistant"),  # noqa: ARG001
    org_id: int | None = None,  # noqa: ARG001
    team_id: int | None = None,  # noqa: ARG001
    user_id: str | None = None,  # noqa: ARG001
    assistant_id: int | None = None,  # noqa: ARG001
    canonical_app_slug: str | None = None,  # noqa: ARG001
    activation_state: str | None = None,  # noqa: ARG001
    include_schema: bool = False,  # noqa: ARG001
    limit: int = Query(50, ge=1, le=200),  # noqa: ARG001
    offset: int = Query(0, ge=0),  # noqa: ARG001
) -> dict:
    _catalog_route_removed()


@router.get("/tools/search")
def search_provider_tools(
    query: str = Query(""),  # noqa: ARG001
    owner_scope: str = Query("assistant"),  # noqa: ARG001
    org_id: int | None = None,  # noqa: ARG001
    team_id: int | None = None,  # noqa: ARG001
    user_id: str | None = None,  # noqa: ARG001
    assistant_id: int | None = None,  # noqa: ARG001
    include_unconnected: bool = False,  # noqa: ARG001
    include_schema: bool = False,  # noqa: ARG001
    limit: int = Query(20, ge=1, le=100),  # noqa: ARG001
) -> list[dict]:
    _catalog_route_removed()


@router.get("/tools/{tool_id}/schema")
def get_provider_tool_schema(
    tool_id: str,  # noqa: ARG001
    owner_scope: str = Query("assistant"),  # noqa: ARG001
    org_id: int | None = None,  # noqa: ARG001
    team_id: int | None = None,  # noqa: ARG001
    user_id: str | None = None,  # noqa: ARG001
    assistant_id: int | None = None,  # noqa: ARG001
) -> dict:
    _catalog_route_removed()


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


@router.post(
    "/composio/stage-file",
    response_model=IntegrationComposioStageFileResponse,
)
async def stage_provider_file(
    file: UploadFile = File(...),
    toolkit_slug: str = Form(...),
    tool_slug: str = Form(...),
) -> IntegrationComposioStageFileResponse:
    """Stage a local file in Composio storage for FileUploadable tool args."""
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )
    filename = file.filename or "upload.bin"
    mimetype = file.content_type or "application/octet-stream"
    result = stage_composio_file(
        content=content,
        filename=filename,
        mimetype=mimetype,
        toolkit_slug=toolkit_slug,
        tool_slug=tool_slug,
    )
    if result.get("status") != "ok":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(result.get("error") or {}).get("message") or "file staging failed",
        )
    return IntegrationComposioStageFileResponse(status="ok", file=result.get("file"))


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
