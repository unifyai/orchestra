"""Table View API endpoints.

Provides endpoints for creating, listing, retrieving, updating, and deleting
shareable table view configurations. Access control is based on project permissions.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.table_view_dao import TableViewDAO, TokenGenerationError
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Project, TableView
from orchestra.settings import settings
from orchestra.web.api.table_view.schema import (
    AdminTableViewResponse,
    CreateTableViewRequest,
    DeleteTableViewsByProjectRequest,
    TableViewListItem,
    TableViewListResponse,
    TableViewMetadata,
    TableViewResponse,
    UpdateTableViewRequest,
    UserMetadata,
)

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()


# =============================================================================
# Helper Functions
# =============================================================================


def _get_project_by_name(
    project_name: str,
    user_id: str,
    organization_id: Optional[int],
    session: Session,
) -> Optional[Project]:
    """Get project by name with access validation."""
    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)

    # Use filter_by_user_access to respect API key context
    # Returns list of Row tuples, need to extract Project from first element
    rows = project_dao.filter_by_user_access(
        user_id=user_id,
        organization_id=organization_id,
        name=project_name,
    )

    return rows[0][0] if rows else None


def _check_project_permission(
    project: Project,
    user_id: str,
    organization_id: Optional[int],
    permission: str,
    session: Session,
) -> bool:
    """Check if user has the specified permission on the project."""
    # Personal projects: owner has all permissions
    if project.organization_id is None:
        return project.user_id == user_id

    # Org projects: check RBAC
    resource_access_dao = ResourceAccessDAO(session)
    return resource_access_dao.check_user_permission(
        user_id=user_id,
        resource_type="project",
        resource_id=project.id,
        permission_name=permission,
    )


def _build_table_view_url(token: str) -> str:
    """Build the shareable table view URL."""
    console_url = settings.console_url.rstrip("/")
    return f"{console_url}/table/view/{token}"


def _table_view_to_response(table_view: TableView) -> TableViewResponse:
    """Convert TableView model to TableViewResponse.

    Uses FK relationship to get current project name (never stale).
    """
    # Get project_name from FK relationship - always current
    project_name = table_view.project.name if table_view.project else "unknown"

    return TableViewResponse(
        url=_build_table_view_url(table_view.token),
        token=table_view.token,
        table_config=table_view.table_config,
        project_config=table_view.project_config,
        table_view_metadata=TableViewMetadata(
            token=table_view.token,
            title=table_view.title,
            project_name=project_name,
            created_at=table_view.created_at,
            updated_at=table_view.updated_at,
            created_by=table_view.user_id,
        ),
        user_metadata=UserMetadata(
            user_id=table_view.user_id,
            organization_id=table_view.organization_id,
        ),
    )


def _table_view_to_list_item(table_view: TableView) -> TableViewListItem:
    """Convert TableView model to TableViewListItem.

    Uses FK relationship to get current project name (never stale).
    """
    # Get project_name from FK relationship - always current
    project_name = table_view.project.name if table_view.project else "unknown"

    return TableViewListItem(
        token=table_view.token,
        title=table_view.title,
        project_name=project_name,
        created_at=table_view.created_at,
        updated_at=table_view.updated_at,
        created_by=table_view.user_id,
        url=_build_table_view_url(table_view.token),
    )


# =============================================================================
# User-Scoped Endpoints
# =============================================================================


@router.post(
    "/logs/table",
    response_model=TableViewResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Table view created successfully"},
        400: {"description": "Invalid request"},
        403: {"description": "Access denied to project"},
        404: {"description": "Project not found"},
    },
)
async def create_table_view(
    request_fastapi: Request,
    body: CreateTableViewRequest,
    session: Session = Depends(get_db_session),
) -> TableViewResponse:
    """
    Create a new shareable table view.

    Requires project:read permission on the target project.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id
    project_name = body.project_config.project_name

    # Get and validate project
    project = _get_project_by_name(project_name, user_id, organization_id, session)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_name}' not found",
        )

    # Check project:read permission
    if not _check_project_permission(
        project,
        user_id,
        organization_id,
        "project:read",
        session,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have read access to this project",
        )

    # Validate context exists if specified
    context_dao = ContextDAO(session)
    if body.project_config.context:
        contexts = context_dao.filter(
            project_id=project.id,
            name=body.project_config.context,
        )
        if not contexts:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Context '{body.project_config.context}' not found in project '{project_name}'",
            )

    # Build config dicts
    table_config_dict = (
        body.table_config.model_dump(exclude_none=True) if body.table_config else {}
    )
    # Exclude project_name from JSONB - use project_id FK as source of truth
    project_config_dict = body.project_config.model_dump(
        exclude_none=True,
        exclude={"project_name"},
    )

    # Create table view
    table_view_dao = TableViewDAO(session)
    try:
        table_view = table_view_dao.create(
            project_id=project.id,
            user_id=user_id,
            organization_id=organization_id,
            table_config=table_config_dict,
            project_config=project_config_dict,
            title=body.title,
        )
    except TokenGenerationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to generate unique token. Please try again.",
        )

    session.commit()

    # Ensure project relationship is loaded for response serialization
    session.refresh(table_view)
    return _table_view_to_response(table_view)


@router.get(
    "/logs/tables",
    response_model=TableViewListResponse,
    responses={
        200: {"description": "List of table views"},
    },
)
def list_table_views(
    request_fastapi: Request,
    project_name: Optional[str] = Query(
        None,
        description="Filter by project name",
    ),
    context: Optional[str] = Query(
        None,
        description="Filter by context (stored in project_config)",
    ),
    limit: int = Query(
        50,
        ge=1,
        description="Maximum number of results to return (capped at 100)",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Number of results to skip for pagination",
    ),
    session: Session = Depends(get_db_session),
) -> TableViewListResponse:
    """
    List table views accessible to the user.

    For personal API keys: Returns table views for personal projects.
    For organization API keys: Returns table views for org projects with access.

    Supports pagination via limit and offset parameters.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    table_view_dao = TableViewDAO(session)

    # If project_name specified, get project first
    project_id = None
    if project_name:
        project = _get_project_by_name(project_name, user_id, organization_id, session)
        if project:
            project_id = project.id

    # Query with eager loading of project relationship (avoids N+1)
    table_views, total_count = table_view_dao.list_by_user_context(
        user_id=user_id,
        organization_id=organization_id,
        project_id=project_id,
        context=context,
        limit=limit,
        offset=offset,
    )

    # Build response items using eager-loaded project relationship
    items = []
    for table_view in table_views:
        # Project is already loaded via joinedload - no extra query
        if table_view.project:
            items.append(_table_view_to_list_item(table_view))

    return TableViewListResponse(table_views=items, count=total_count)


@router.get(
    "/logs/tables/{token}",
    response_model=TableViewResponse,
    responses={
        200: {"description": "Table view details"},
        403: {"description": "Access denied"},
        404: {"description": "Table view not found"},
    },
)
def get_table_view(
    request_fastapi: Request,
    token: str,
    session: Session = Depends(get_db_session),
) -> TableViewResponse:
    """
    Get a table view by token.

    Requires project:read permission on the table view's project.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    table_view_dao = TableViewDAO(session)
    table_view = table_view_dao.get_by_token(token)

    if not table_view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table view not found",
        )

    # Get project
    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)
    project = project_dao.get(table_view.project_id)

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table view's project not found",
        )

    # Check project:read permission
    if not _check_project_permission(
        project,
        user_id,
        organization_id,
        "project:read",
        session,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this table view's project",
        )

    return _table_view_to_response(table_view)


@router.patch(
    "/logs/tables/{token}",
    response_model=TableViewResponse,
    responses={
        200: {"description": "Table view updated"},
        403: {"description": "Access denied"},
        404: {"description": "Table view not found"},
    },
)
def update_table_view(
    request_fastapi: Request,
    token: str,
    body: UpdateTableViewRequest,
    session: Session = Depends(get_db_session),
) -> TableViewResponse:
    """
    Update a table view.

    Requires project:write permission on the table view's project.
    If updating project_config, the new project_name and context are validated.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    table_view_dao = TableViewDAO(session)
    table_view = table_view_dao.get_by_token(token)

    if not table_view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table view not found",
        )

    # Get current project
    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)
    project = project_dao.get(table_view.project_id)

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table view's project not found",
        )

    # Check project:write permission on current project
    if not _check_project_permission(
        project,
        user_id,
        organization_id,
        "project:write",
        session,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have write access to this table view's project",
        )

    # If updating project_config, validate the new project_name and context
    target_project = project  # Default to current project
    project_changed = False

    if body.project_config and body.project_config.project_name:
        new_project_name = body.project_config.project_name

        # If project_name is changing, validate access to new project
        if new_project_name != project.name:
            new_project = _get_project_by_name(
                new_project_name,
                user_id,
                organization_id,
                session,
            )
            if not new_project:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Project '{new_project_name}' not found",
                )

            # Check write permission on new project too
            if not _check_project_permission(
                new_project,
                user_id,
                organization_id,
                "project:write",
                session,
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"You do not have write access to project '{new_project_name}'",
                )

            target_project = new_project
            project_changed = True

        # Validate context exists if specified
        if body.project_config.context:
            contexts = context_dao.filter(
                project_id=target_project.id,
                name=body.project_config.context,
            )
            if not contexts:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Context '{body.project_config.context}' not found in project '{target_project.name}'",
                )

    # Update table view - include project_id and organization_id if project changed
    # Exclude project_name from JSONB - use project_id FK as source of truth
    updated_table_view = table_view_dao.update(
        table_view_id=table_view.id,
        title=body.title,
        table_config=(
            body.table_config.model_dump(exclude_none=True)
            if body.table_config
            else None
        ),
        project_config=(
            body.project_config.model_dump(
                exclude_none=True,
                exclude={"project_name"},
            )
            if body.project_config
            else None
        ),
        project_id=target_project.id if project_changed else None,
        organization_id=target_project.organization_id if project_changed else ...,
    )

    session.commit()

    # Refresh to load updated project relationship
    session.refresh(updated_table_view)
    return _table_view_to_response(updated_table_view)


@router.delete(
    "/logs/tables/{token}",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Table view deleted"},
        403: {"description": "Access denied"},
        404: {"description": "Table view not found"},
    },
)
def delete_table_view(
    request_fastapi: Request,
    token: str,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Delete a table view.

    Requires project:write permission on the table view's project.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    table_view_dao = TableViewDAO(session)
    table_view = table_view_dao.get_by_token(token)

    if not table_view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table view not found",
        )

    # Get project
    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)
    project = project_dao.get(table_view.project_id)

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table view's project not found",
        )

    # Check project:write permission
    if not _check_project_permission(
        project,
        user_id,
        organization_id,
        "project:write",
        session,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have write access to this table view's project",
        )

    table_view_dao.delete(table_view.id)
    session.commit()

    return {"deleted": True, "token": token}


@router.delete(
    "/logs/tables",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Table views deleted"},
        403: {"description": "Access denied to project"},
        404: {"description": "Project not found"},
    },
)
def delete_table_views_by_project(
    request_fastapi: Request,
    body: DeleteTableViewsByProjectRequest,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Delete all table views for a project, optionally filtered by context.

    Requires project:write permission on the target project.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    # Get and validate project
    project = _get_project_by_name(
        body.project_name,
        user_id,
        organization_id,
        session,
    )
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{body.project_name}' not found",
        )

    # Check project:write permission
    if not _check_project_permission(
        project,
        user_id,
        organization_id,
        "project:write",
        session,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have write access to this project",
        )

    # Delete table views
    table_view_dao = TableViewDAO(session)
    deleted_count = table_view_dao.delete_by_project(
        project_id=project.id,
        context=body.context,
    )

    session.commit()

    return {
        "deleted_count": deleted_count,
        "project_name": body.project_name,
        "context": body.context,
    }


# =============================================================================
# Admin Endpoints
# =============================================================================


@admin_router.get(
    "/logs/table",
    response_model=AdminTableViewResponse,
    responses={
        200: {"description": "Table view details for admin"},
        404: {"description": "Table view not found"},
    },
)
def admin_get_table_view(
    token: str = Query(..., description="Table view token"),
    session: Session = Depends(get_db_session),
) -> AdminTableViewResponse:
    """
    Admin endpoint to get table view by token.

    Returns user_metadata for API key lookup during table viewing.
    """
    table_view_dao = TableViewDAO(session)
    table_view = table_view_dao.get_by_token(token)

    if not table_view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table view not found",
        )

    # Use FK relationship (eager-loaded via joinedload in get_by_token)
    project_name = table_view.project.name if table_view.project else "Unknown"

    return AdminTableViewResponse(
        user_id=table_view.user_id,
        organization_id=table_view.organization_id,
        config=table_view.table_config,
        project_config=table_view.project_config,
        metadata=TableViewMetadata(
            token=table_view.token,
            title=table_view.title,
            project_name=project_name,
            created_at=table_view.created_at,
            updated_at=table_view.updated_at,
            created_by=table_view.user_id,
        ),
    )
