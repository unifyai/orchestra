"""Dashboard token registration and resolution endpoints.

Provides endpoints for:
- Token registration (Unity registers tokens after inserting into Unify contexts)
- Token resolution (console resolves tokens to context paths + creator identity)
- Tile bridges (console proxies live-data requests on behalf of tile creators)
  - Filter bridge (row-level queries on a single context)
  - Reduce bridge (aggregation on a single context)
  - Join bridge (cross-context row-level join)
  - Join-reduce bridge (cross-context join + aggregation)
"""

import logging
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.dashboard_token_dao import DashboardTokenDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.dashboard.schema import (
    FilterBridgeRequest,
    FilterBridgeResponse,
    JoinBridgeRequest,
    JoinBridgeResponse,
    JoinReduceBridgeRequest,
    JoinReduceBridgeResponse,
    ReduceBridgeRequest,
    ReduceBridgeResponse,
    RegisterTokenRequest,
    RegisterTokenResponse,
    TokenResolutionResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()


def _resolve_tile_token(session: Session, token: str):
    """Resolve a token and verify it belongs to a tile.

    Returns the DashboardToken ORM object or raises HTTPException.
    """
    dao = DashboardTokenDAO(session)
    entry = dao.get_by_token(token)

    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found",
        )

    if entry.entity_type != "tile":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bridge endpoints are only available for tiles",
        )

    return entry


def _bridge_daos(session: Session):
    """Instantiate the common set of DAOs used by all bridge endpoints."""
    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.field_type_dao import FieldTypeDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.project_dao import ProjectDAO

    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    return project_dao, field_type_dao, context_dao


@router.post(
    "/dashboards/tokens",
    response_model=RegisterTokenResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Token registered successfully"},
        409: {"description": "Token already exists"},
    },
)
def register_token(
    request_fastapi: Request,
    body: RegisterTokenRequest,
    session: Session = Depends(get_db_session),
) -> RegisterTokenResponse:
    """Register a token-to-context mapping for a dashboard tile or layout.

    Called by Unity after inserting content into a Unify context.
    The token is generated client-side (Unity) using secrets.token_urlsafe.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    dao = DashboardTokenDAO(session)

    if dao.get_by_token(body.token):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Token '{body.token}' already exists",
        )

    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.project_dao import ProjectDAO

    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)

    rows = project_dao.filter_by_user_access(
        user_id=user_id,
        organization_id=organization_id,
        name=body.project_name,
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{body.project_name}' not found or not accessible",
        )
    project = rows[0][0]

    entry = dao.register(
        token=body.token,
        entity_type=body.entity_type,
        context_name=body.context_name,
        project_id=project.id,
        user_id=user_id,
        organization_id=organization_id,
    )
    session.commit()

    return RegisterTokenResponse(
        token=entry.token,
        entity_type=entry.entity_type,
        context_name=entry.context_name,
    )


@router.delete(
    "/dashboards/tokens/{token}",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Token deleted"},
        404: {"description": "Token not found"},
    },
)
def delete_token(
    request_fastapi: Request,
    token: str,
    session: Session = Depends(get_db_session),
) -> dict:
    """Remove a token mapping. Only the creator can delete their tokens."""
    user_id = request_fastapi.state.user_id
    dao = DashboardTokenDAO(session)

    entry = dao.get_by_token(token)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found",
        )

    if entry.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own tokens",
        )

    dao.delete_by_token(token)
    session.commit()
    return {"deleted": True, "token": token}


@admin_router.get(
    "/dashboards/tokens/{token}",
    response_model=TokenResolutionResponse,
    responses={
        200: {"description": "Token resolved"},
        404: {"description": "Token not found"},
    },
)
def admin_resolve_token(
    token: str,
    session: Session = Depends(get_db_session),
) -> TokenResolutionResponse:
    """Resolve a token to its context path and creator identity.

    Used by the console to fetch tile/dashboard content from Unify contexts
    using the creator's API key.
    """
    dao = DashboardTokenDAO(session)
    entry = dao.get_by_token(token)

    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found",
        )

    return TokenResolutionResponse(
        entity_type=entry.entity_type,
        context_name=entry.context_name,
        user_id=entry.user_id,
        organization_id=entry.organization_id,
        project_id=entry.project_id,
    )


@admin_router.post(
    "/dashboards/tiles/{token}/filter",
    response_model=FilterBridgeResponse,
    responses={
        200: {"description": "Data fetched successfully"},
        400: {"description": "Bridge endpoints are only available for tiles"},
        404: {"description": "Token not found"},
    },
)
def admin_filter_bridge(
    token: str,
    body: FilterBridgeRequest,
    session: Session = Depends(get_db_session),
) -> FilterBridgeResponse:
    """Fetch filtered log data for a tile token.

    Resolves the token to the tile creator's identity, queries logs directly
    via the same internal query path used by GET /v0/logs, and returns
    flattened row entries for ergonomic JS consumption.
    """
    from orchestra.web.api.log.utils.logging_utils import _format_logs, _get_logs_query

    entry = _resolve_tile_token(session, token)
    project_dao, field_type_dao, context_dao = _bridge_daos(session)

    fake_request = SimpleNamespace(
        state=SimpleNamespace(
            user_id=entry.user_id,
            organization_id=entry.organization_id,
        ),
    )

    project_name = entry.context_name.split("/")[0]

    rows, total_count = _get_logs_query(
        request_fastapi=fake_request,
        project_name=project_name,
        context=body.context,
        filter_expr=body.filter_expr,
        sorting=body.sorting,
        from_ids=None,
        exclude_ids=None,
        from_fields=body.from_fields,
        exclude_fields=body.exclude_fields,
        limit=body.limit,
        offset=body.offset or 0,
        project_dao=project_dao,
        field_type_dao=field_type_dao,
        context_dao=context_dao,
        session=session,
        randomize=body.randomize or False,
    )

    project_id = project_dao.get_by_user_and_name(
        name=project_name,
        user_id=entry.user_id,
        organization_id=entry.organization_id,
    ).id

    context_id = None
    if body.context:
        context_obj = context_dao.filter(name=body.context, project_id=project_id)
        if context_obj:
            context_id = context_obj[0][0].id
    else:
        context_obj = context_dao.filter(name="", project_id=project_id)
        if context_obj:
            context_id = context_obj[0][0].id

    field_types = field_type_dao.get_field_types(
        project_id,
        context_id=context_id,
        return_mutable=True,
    )
    field_order_map = field_type_dao.get_ordered_field_names(
        project_id,
        context_id=context_id,
    )

    logs_out, _ = _format_logs(
        rows=rows,
        field_types=field_types,
        value_limit=None,
        column_context=body.column_context,
        field_order_map=field_order_map,
        from_fields=body.from_fields,
        exclude_fields=body.exclude_fields,
    )

    flat_rows = [
        {**log.get("entries", {}), **log.get("derived_entries", {})} for log in logs_out
    ]

    return FilterBridgeResponse(rows=flat_rows, total_count=total_count)


@admin_router.post(
    "/dashboards/tiles/{token}/reduce",
    response_model=ReduceBridgeResponse,
    responses={
        200: {"description": "Metric computed successfully"},
        400: {
            "description": "Bridge endpoints are only available for tiles / invalid params",
        },
        404: {"description": "Token not found"},
    },
)
def admin_reduce_bridge(
    token: str,
    body: ReduceBridgeRequest,
    session: Session = Depends(get_db_session),
) -> ReduceBridgeResponse:
    """Compute an aggregation metric for a tile token.

    Resolves the token to the tile creator's identity and delegates to
    ``compute_metric_for_key`` (ungrouped) or
    ``_compute_metric_for_key_grouped`` (grouped).
    """
    from orchestra.web.api.log.utils.metric_utils import (
        _compute_metric_for_key_grouped,
        compute_metric_for_key,
    )

    entry = _resolve_tile_token(session, token)
    project_dao, field_type_dao, context_dao = _bridge_daos(session)

    project_name = entry.context_name.split("/")[0]
    project_obj = project_dao.get_by_user_and_name(
        name=project_name,
        user_id=entry.user_id,
        organization_id=entry.organization_id,
    )

    context_name = body.context or ""
    context_obj = context_dao.filter(name=context_name, project_id=project_obj.id)
    context_id = context_obj[0][0].id if context_obj else None
    field_types = field_type_dao.get_field_types(project_obj.id, context_id=context_id)

    all_keys = [body.columns] if isinstance(body.columns, str) else list(body.columns)
    single_key = isinstance(body.columns, str)

    if body.group_by:
        results = {}
        for k in all_keys:
            grouped_value = _compute_metric_for_key_grouped(
                key=k,
                metric=body.metric,
                project_obj=project_obj,
                context_id=context_id,
                field_types=field_types,
                group_by=body.group_by,
                key_filter_expr=body.filter_expr,
                key_from_ids=None,
                key_exclude_ids=None,
                session=session,
            )
            results[k] = grouped_value
        result = results[all_keys[0]] if single_key else results
    else:
        results = {}
        for k in all_keys:
            value = compute_metric_for_key(
                key=k,
                metric=body.metric,
                project_obj=project_obj,
                context_id=context_id,
                field_types=field_types,
                key_filter_expr=body.filter_expr,
                key_from_ids=None,
                key_exclude_ids=None,
                session=session,
            )
            results[k] = value
        result = results[all_keys[0]] if single_key else results

    return ReduceBridgeResponse(result=result)


@admin_router.post(
    "/dashboards/tiles/{token}/join",
    response_model=JoinBridgeResponse,
    responses={
        200: {"description": "Join query executed successfully"},
        400: {
            "description": "Bridge endpoints are only available for tiles / invalid params",
        },
        404: {"description": "Token not found"},
    },
)
def admin_join_bridge(
    token: str,
    body: JoinBridgeRequest,
    session: Session = Depends(get_db_session),
) -> JoinBridgeResponse:
    """Execute a cross-context join for a tile token (row mode).

    Resolves the token to the tile creator's identity and delegates to
    ``_join_query_internal`` with ``metric=None``.
    """
    from orchestra.web.api.log.utils.logging_utils import _join_query_internal

    entry = _resolve_tile_token(session, token)
    project_dao, field_type_dao, context_dao = _bridge_daos(session)

    fake_request = SimpleNamespace(
        state=SimpleNamespace(
            user_id=entry.user_id,
            organization_id=entry.organization_id,
        ),
    )

    project_name = entry.context_name.split("/")[0]
    project_obj = project_dao.get_by_user_and_name(
        name=project_name,
        user_id=entry.user_id,
        organization_id=entry.organization_id,
    )

    pair_of_args = [
        {"context": body.tables[0], "filter_expr": body.left_where},
        {"context": body.tables[1], "filter_expr": body.right_where},
    ]

    try:
        result = _join_query_internal(
            project_id=project_obj.id,
            project_name=project_name,
            pair_of_args=pair_of_args,
            join_expr=body.join_expr,
            mode=body.mode,
            columns=body.select,
            filter_expr=body.result_where,
            sorting=None,
            limit=body.result_limit,
            offset=body.result_offset,
            group_by=None,
            metric=None,
            key=None,
            request_fastapi=fake_request,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception("Join bridge error for token %s", token)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error performing join query: {e}",
        )

    logs = result.get("logs", [])
    return JoinBridgeResponse(rows=logs, total_count=result.get("count", 0))


@admin_router.post(
    "/dashboards/tiles/{token}/join-reduce",
    response_model=JoinReduceBridgeResponse,
    responses={
        200: {"description": "Join + reduce executed successfully"},
        400: {
            "description": "Bridge endpoints are only available for tiles / invalid params",
        },
        404: {"description": "Token not found"},
    },
)
def admin_join_reduce_bridge(
    token: str,
    body: JoinReduceBridgeRequest,
    session: Session = Depends(get_db_session),
):
    """Execute a cross-context join + aggregation for a tile token.

    Resolves the token to the tile creator's identity and delegates to
    ``_join_query_internal`` with ``metric`` + ``key``.
    """
    from orchestra.web.api.log.utils.logging_utils import _join_query_internal

    entry = _resolve_tile_token(session, token)
    project_dao, field_type_dao, context_dao = _bridge_daos(session)

    fake_request = SimpleNamespace(
        state=SimpleNamespace(
            user_id=entry.user_id,
            organization_id=entry.organization_id,
        ),
    )

    project_name = entry.context_name.split("/")[0]
    project_obj = project_dao.get_by_user_and_name(
        name=project_name,
        user_id=entry.user_id,
        organization_id=entry.organization_id,
    )

    pair_of_args = [
        {"context": body.tables[0], "filter_expr": body.left_where},
        {"context": body.tables[1], "filter_expr": body.right_where},
    ]

    try:
        result = _join_query_internal(
            project_id=project_obj.id,
            project_name=project_name,
            pair_of_args=pair_of_args,
            join_expr=body.join_expr,
            mode=body.mode,
            columns=body.select,
            filter_expr=body.result_where,
            sorting=None,
            limit=None,
            offset=0,
            group_by=body.group_by,
            metric=body.metric,
            key=body.columns,
            request_fastapi=fake_request,
            project_dao=project_dao,
            field_type_dao=field_type_dao,
            context_dao=context_dao,
            session=session,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception("Join-reduce bridge error for token %s", token)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error performing join-reduce query: {e}",
        )

    return JoinReduceBridgeResponse(result=result)
