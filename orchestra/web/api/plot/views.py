"""Plot API endpoints.

Provides endpoints for creating, listing, retrieving, updating, and deleting
shareable plot configurations. Access control is based on project permissions.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.plot_dao import PlotDAO, TokenGenerationError
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Plot, Project
from orchestra.settings import settings
from orchestra.web.api.plot.llm_inference import (
    PlotConfigInferenceError,
    PlotConfigValidationError,
    infer_plot_config,
)
from orchestra.web.api.plot.schema import (
    AdminPlotResponse,
    CreatePlotRequest,
    DeletePlotsByProjectRequest,
    InferredConfigResponse,
    PlotListItem,
    PlotListResponse,
    PlotMetadata,
    PlotResponse,
    UpdatePlotRequest,
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


def _build_plot_url(token: str) -> str:
    """Build the shareable plot URL."""
    console_url = settings.console_url.rstrip("/")
    return f"{console_url}/plot/view/{token}"


def _plot_to_response(plot: Plot) -> PlotResponse:
    """Convert Plot model to PlotResponse.

    Uses FK relationship to get current project name (never stale).
    """
    # Get project_name from FK relationship - always current
    project_name = plot.project.name if plot.project else "unknown"

    return PlotResponse(
        url=_build_plot_url(plot.token),
        token=plot.token,
        plot_config=plot.plot_config,
        project_config=plot.project_config,
        plot_metadata=PlotMetadata(
            token=plot.token,
            title=plot.title,
            project_name=project_name,
            created_at=plot.created_at,
            updated_at=plot.updated_at,
            created_by=plot.user_id,
        ),
        user_metadata=UserMetadata(
            user_id=plot.user_id,
            organization_id=plot.organization_id,
        ),
    )


def _plot_to_list_item(plot: Plot) -> PlotListItem:
    """Convert Plot model to PlotListItem.

    Uses FK relationship to get current project name (never stale).
    """
    # Get project_name from FK relationship - always current
    project_name = plot.project.name if plot.project else "unknown"

    return PlotListItem(
        token=plot.token,
        title=plot.title,
        project_name=project_name,
        created_at=plot.created_at,
        updated_at=plot.updated_at,
        created_by=plot.user_id,
        url=_build_plot_url(plot.token),
    )


# =============================================================================
# User-Scoped Endpoints
# =============================================================================


@router.post(
    "/logs/plot",
    response_model=PlotResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Plot created successfully"},
        400: {"description": "Invalid request"},
        403: {"description": "Access denied to project"},
        404: {"description": "Project not found"},
    },
)
async def create_plot(
    request_fastapi: Request,
    body: CreatePlotRequest,
    session: Session = Depends(get_db_session),
) -> PlotResponse:
    """
    Create a new shareable plot.

    Supports two modes:
    1. Direct config: Provide explicit plot_config
    2. Description-based: Provide natural language description for LLM inference

    Requires project:read permission on the target project.
    LLM inference (if used) is billed to the caller's account.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id
    project_name = body.project_config.project_name

    # Validate request
    if not body.plot_config and not body.description:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either plot_config or description is required",
        )

    if body.plot_config and not body.plot_config.x_axis:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="plot_config.x_axis is required",
        )

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
    context_id = None
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
        context_id = contexts[0][0].id

    # Fetch available fields for validation
    # If a context is specified, fetch fields for that context; otherwise fetch all project fields
    field_type_dao = FieldTypeDAO(session)
    field_types_dict = field_type_dao.get_field_types(
        project_id=project.id,
        context_id=context_id,
    )
    available_fields = list(field_types_dict.keys())

    # Determine plot config (direct or inferred)
    plot_config_dict = None
    inferred_config = None

    if body.description and not body.plot_config:
        # LLM inference mode
        if not available_fields:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields found in project for LLM inference",
            )

        try:
            # Get API key for LLM call
            from orchestra.db.dao.api_key_dao import ApiKeyDAO

            api_key_dao = ApiKeyDAO(session)
            if organization_id:
                keys = api_key_dao.get_organization_keys(user_id, organization_id)
            else:
                keys = api_key_dao.get_personal_keys(user_id)

            if not keys:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not retrieve API key for LLM inference",
                )

            api_key = keys[0][0].key

            # Infer config
            inferred = await infer_plot_config(
                description=body.description,
                available_fields=available_fields,
                field_types=field_types_dict,
                api_key=api_key,
                orchestra_url=f"http://localhost:{settings.port}",
            )

            plot_config_dict = {
                "type": inferred.get("type", "scatter"),
                "x_axis": inferred.get("x_axis"),
                "y_axis": inferred.get("y_axis"),
                "group_by": inferred.get("group_by"),
                "aggregate": inferred.get("aggregate"),
                "scale_x": inferred.get("scale_x", "linear"),
                "scale_y": inferred.get("scale_y", "linear"),
                "metric": inferred.get("metric", "mean"),
                "bin_count": inferred.get("bin_count", 10),
                "show_regression": inferred.get("show_regression", False),
            }

            inferred_config = InferredConfigResponse(
                type=inferred.get("type", "scatter"),
                x_axis=inferred.get("x_axis", ""),
                y_axis=inferred.get("y_axis"),
                group_by=inferred.get("group_by"),
                confidence=inferred.get("confidence", 0.5),
                reasoning=inferred.get("reasoning"),
            )

        except PlotConfigInferenceError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"LLM inference failed: {e}",
            )
        except PlotConfigValidationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid inferred config: {e}",
            )
    else:
        # Direct config mode
        # Validate that referenced fields exist in the project
        if available_fields:
            # Validate x_axis
            if body.plot_config.x_axis not in available_fields:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Field '{body.plot_config.x_axis}' not found in project. "
                    f"Available fields: {', '.join(available_fields[:10])}"
                    + ("..." if len(available_fields) > 10 else ""),
                )

            # Validate y_axis if provided
            if (
                body.plot_config.y_axis
                and body.plot_config.y_axis not in available_fields
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Field '{body.plot_config.y_axis}' not found in project. "
                    f"Available fields: {', '.join(available_fields[:10])}"
                    + ("..." if len(available_fields) > 10 else ""),
                )

            # Validate group_by if provided
            if (
                body.plot_config.group_by
                and body.plot_config.group_by not in available_fields
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Field '{body.plot_config.group_by}' not found in project. "
                    f"Available fields: {', '.join(available_fields[:10])}"
                    + ("..." if len(available_fields) > 10 else ""),
                )

        plot_config_dict = body.plot_config.model_dump(exclude_none=True)

    # Build project config dict - exclude project_name (use project_id FK as source of truth)
    project_config_dict = body.project_config.model_dump(
        exclude_none=True, exclude={"project_name"}
    )

    # Create plot
    plot_dao = PlotDAO(session)
    try:
        plot = plot_dao.create(
            project_id=project.id,
            user_id=user_id,
            organization_id=organization_id,
            plot_config=plot_config_dict,
            project_config=project_config_dict,
            title=body.title,
        )
    except TokenGenerationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to generate unique token. Please try again.",
        )

    session.commit()

    # Refresh to load project relationship for response
    session.refresh(plot)
    response = _plot_to_response(plot)
    if inferred_config:
        response.inferred_config = inferred_config

    return response


@router.get(
    "/logs/plots",
    response_model=PlotListResponse,
    responses={
        200: {"description": "List of plots"},
    },
)
def list_plots(
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
) -> PlotListResponse:
    """
    List plots accessible to the user.

    For personal API keys: Returns plots for personal projects.
    For organization API keys: Returns plots for org projects with access.

    Supports pagination via limit and offset parameters.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    plot_dao = PlotDAO(session)

    # If project_name specified, get project first
    project_id = None
    if project_name:
        project = _get_project_by_name(project_name, user_id, organization_id, session)
        if project:
            project_id = project.id

    # Query with eager loading of project relationship (avoids N+1)
    plots, total_count = plot_dao.list_by_user_context(
        user_id=user_id,
        organization_id=organization_id,
        project_id=project_id,
        context=context,
        limit=limit,
        offset=offset,
    )

    # Build response items using eager-loaded project relationship
    items = []
    for plot in plots:
        # Project is already loaded via joinedload - no extra query
        if plot.project:
            items.append(_plot_to_list_item(plot))

    return PlotListResponse(plots=items, count=total_count)


@router.get(
    "/logs/plots/{token}",
    response_model=PlotResponse,
    responses={
        200: {"description": "Plot details"},
        403: {"description": "Access denied"},
        404: {"description": "Plot not found"},
    },
)
def get_plot(
    request_fastapi: Request,
    token: str,
    session: Session = Depends(get_db_session),
) -> PlotResponse:
    """
    Get a plot by token.

    Requires project:read permission on the plot's project.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    plot_dao = PlotDAO(session)
    plot = plot_dao.get_by_token(token)

    if not plot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plot not found",
        )

    # Get project
    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)
    project = project_dao.get(plot.project_id)

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plot's project not found",
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
            detail="You do not have access to this plot's project",
        )

    return _plot_to_response(plot)


@router.patch(
    "/logs/plots/{token}",
    response_model=PlotResponse,
    responses={
        200: {"description": "Plot updated"},
        403: {"description": "Access denied"},
        404: {"description": "Plot not found"},
    },
)
def update_plot(
    request_fastapi: Request,
    token: str,
    body: UpdatePlotRequest,
    session: Session = Depends(get_db_session),
) -> PlotResponse:
    """
    Update a plot.

    Requires project:write permission on the plot's project.
    If updating project_config, the new project_name and context are validated.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    plot_dao = PlotDAO(session)
    plot = plot_dao.get_by_token(token)

    if not plot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plot not found",
        )

    # Get current project
    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)
    project = project_dao.get(plot.project_id)

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plot's project not found",
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
            detail="You do not have write access to this plot's project",
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

    # Update plot - include project_id and organization_id if project changed
    # Exclude project_name from JSONB - use project_id FK as source of truth
    updated_plot = plot_dao.update(
        plot_id=plot.id,
        title=body.title,
        plot_config=body.plot_config.model_dump(exclude_none=True)
        if body.plot_config
        else None,
        project_config=body.project_config.model_dump(exclude_none=True, exclude={"project_name"})
        if body.project_config
        else None,
        project_id=target_project.id if project_changed else None,
        organization_id=target_project.organization_id if project_changed else ...,
    )

    session.commit()

    # Refresh to load updated project relationship
    session.refresh(updated_plot)
    return _plot_to_response(updated_plot)


@router.delete(
    "/logs/plots/{token}",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Plot deleted"},
        403: {"description": "Access denied"},
        404: {"description": "Plot not found"},
    },
)
def delete_plot(
    request_fastapi: Request,
    token: str,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Delete a plot.

    Requires project:write permission on the plot's project.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    plot_dao = PlotDAO(session)
    plot = plot_dao.get_by_token(token)

    if not plot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plot not found",
        )

    # Get project
    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)
    project = project_dao.get(plot.project_id)

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plot's project not found",
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
            detail="You do not have write access to this plot's project",
        )

    plot_dao.delete(plot.id)
    session.commit()

    return {"deleted": True, "token": token}


@router.delete(
    "/logs/plots",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Plots deleted"},
        403: {"description": "Access denied to project"},
        404: {"description": "Project not found"},
    },
)
def delete_plots_by_project(
    request_fastapi: Request,
    body: DeletePlotsByProjectRequest,
    session: Session = Depends(get_db_session),
) -> dict:
    """
    Delete all plots for a project, optionally filtered by context.

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

    # Delete plots
    plot_dao = PlotDAO(session)
    deleted_count = plot_dao.delete_by_project(
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
    "/logs/plot",
    response_model=AdminPlotResponse,
    responses={
        200: {"description": "Plot details for admin"},
        404: {"description": "Plot not found"},
    },
)
def admin_get_plot(
    token: str = Query(..., description="Plot token"),
    session: Session = Depends(get_db_session),
) -> AdminPlotResponse:
    """
    Admin endpoint to get plot by token.

    Returns user_metadata for API key lookup during plot viewing.
    """
    plot_dao = PlotDAO(session)
    plot = plot_dao.get_by_token(token)

    if not plot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plot not found",
        )

    # Use FK relationship (eager-loaded via joinedload in get_by_token)
    project_name = plot.project.name if plot.project else "Unknown"

    return AdminPlotResponse(
        user_id=plot.user_id,
        organization_id=plot.organization_id,
        config=plot.plot_config,
        project_config=plot.project_config,
        metadata=PlotMetadata(
            token=plot.token,
            title=plot.title,
            project_name=project_name,
            created_at=plot.created_at,
            updated_at=plot.updated_at,
            created_by=plot.user_id,
        ),
    )
