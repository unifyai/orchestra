"""Provider-backed integration APIs.

Console uses these endpoints for gallery, connect, reconnect, disconnect, and
health states. Unity runtime uses the same contract for effective connection
discovery, provider tool search, schema lookup, and governed invocation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from orchestra.db.dao.integration_provider_dao import IntegrationProviderDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.integrations.operations import (
    OwnerContext,
    cancel_connection,
    complete_connection,
    complete_connection_by_provider_connection_id,
    disconnect_connection,
    get_app_detail,
    get_apps,
    get_connection_tool_policy,
    get_tool_schema,
    get_tools,
    list_apps,
    list_connections,
    patch_connection_tool_policy,
    reconnect_connection,
    run_tool,
    search_apps,
    search_tools,
    seed_default_provider_catalog,
    start_connection,
    sync_composio_catalog,
    sync_pipedream_catalog,
    sync_provider_catalog,
    test_connection,
    update_connection,
)
from orchestra.web.api.integrations.schema import (
    ComposioCatalogSyncRequest,
    ComposioCatalogSyncResponse,
    DynamicIntegrationAppResponse,
    IntegrationAppDetailResponse,
    IntegrationBackendCreate,
    IntegrationBackendPatchRequest,
    IntegrationBackendResponse,
    IntegrationConnectCompleteByProviderRequest,
    IntegrationConnectCompleteRequest,
    IntegrationConnectionPatchRequest,
    IntegrationConnectionResponse,
    IntegrationConnectStartRequest,
    IntegrationConnectStartResponse,
    IntegrationHealthResponse,
    IntegrationToolPolicyPatchRequest,
    IntegrationToolPolicyResponse,
    PipedreamCatalogSyncRequest,
    PipedreamCatalogSyncResponse,
    ProviderAppGetRequest,
    ProviderAppGetResponse,
    ProviderAppSearchRequest,
    ProviderAppSearchResult,
    ProviderCatalogSyncRequest,
    ProviderCatalogSyncResponse,
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


@admin_router.post("/sync")
def sync_integration_catalog(
    body: ProviderCatalogSyncRequest,
    session: Session = Depends(get_db_session),
) -> ProviderCatalogSyncResponse:
    return ProviderCatalogSyncResponse(**sync_provider_catalog(session, body))


@admin_router.post("/sync/composio")
def sync_composio_integration_catalog(
    body: ComposioCatalogSyncRequest,
    session: Session = Depends(get_db_session),
) -> ComposioCatalogSyncResponse:
    return sync_composio_catalog(session, body)


@admin_router.post("/sync/pipedream")
def sync_pipedream_integration_catalog(
    body: PipedreamCatalogSyncRequest,
    session: Session = Depends(get_db_session),
) -> PipedreamCatalogSyncResponse:
    return sync_pipedream_catalog(session, body)


@router.get("/apps")
def get_integration_apps(
    query: str = "",
    owner_scope: str = Query("assistant"),
    org_id: int | None = None,
    team_id: int | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
    session: Session = Depends(get_db_session),
) -> list[DynamicIntegrationAppResponse]:
    return list_apps(
        session,
        query_text=query,
        owner=_owner_from_query(owner_scope, org_id, team_id, user_id, assistant_id),
    )


@router.post("/apps/get")
def get_integration_apps_page(
    body: ProviderAppGetRequest,
    session: Session = Depends(get_db_session),
) -> ProviderAppGetResponse:
    return get_apps(session, body)


@router.post("/apps/search")
def search_integration_apps(
    body: ProviderAppSearchRequest,
    session: Session = Depends(get_db_session),
) -> list[ProviderAppSearchResult]:
    return search_apps(session, body)


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
        connection, connect_url, auth_mode, requires_browser_redirect = (
            start_connection(
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
        requested_scopes=body.requested_scopes,
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
    session: Session = Depends(get_db_session),
) -> IntegrationToolPolicyResponse:
    try:
        return get_connection_tool_policy(session, connection_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.patch("/connections/{connection_id}/tool-policy")
def patch_integration_tool_policy(
    connection_id: str,
    body: IntegrationToolPolicyPatchRequest,
    session: Session = Depends(get_db_session),
) -> IntegrationToolPolicyResponse:
    try:
        return patch_connection_tool_policy(session, connection_id, body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post("/tools/search")
def search_provider_tools(
    body: ProviderToolSearchRequest,
    session: Session = Depends(get_db_session),
) -> list[ProviderToolSearchResult]:
    return search_tools(session, body)


@router.post("/tools/get")
def get_provider_tools(
    body: ProviderToolGetRequest,
    session: Session = Depends(get_db_session),
) -> ProviderToolGetResponse:
    return get_tools(session, body)


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
