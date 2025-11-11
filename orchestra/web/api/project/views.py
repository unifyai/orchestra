"""
Includes endpoints related to log projects.
"""

from datetime import datetime, timezone
from typing import List

import sqlalchemy
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.auth_user_dao import AuthUserDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.derived_log_dao import DerivedLogDAO
from orchestra.db.dao.favorite_project_dao import FavoriteProjectDAO
from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.db.dao.tile_dao import TileDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import (
    Context,
    DerivedLog,
    FavoriteProject,
    FieldType,
    JSONLog,
    Log,
    LogEvent,
    LogEventContext,
    LogEventDerivedLog,
    LogEventJSONLog,
    LogEventLog,
    Organization,
    Project,
    ResourceAccess,
)
from orchestra.settings import settings
from orchestra.web.api.interface.schema import (
    ProjectTemplateSchema,
    TemplateExportResponse,
    TemplateImportResponse,
)
from orchestra.web.api.interface.template_utils import (
    TemplateConverter,
    TemplateValidator,
)
from orchestra.web.api.project.schema import (
    DuplicateProjectRequest,
    ExportProjectTemplateRequest,
    FavoriteProjectIn,
    FavoriteProjectOut,
    FavoriteProjectUpdate,
    ImportProjectTemplateRequest,
    InterfaceInfo,
    ProjectCommitHistory,
    ProjectCommitRequest,
    ProjectConfig,
    ProjectOut,
    ProjectRollbackRequest,
    ProjectTreeItem,
    ProjectUpdate,
    ShareProjectRequest,
    TabInfo,
    TransferResponse,
    TransferToOrganizationRequest,
)
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


def get_project_or_404(
    request_fastapi: Request,
    project_name: str = Path(
        ...,
        description="Project name, may contain slashes",
        example="proj/a",
    ),
    session: Session = Depends(get_db_session),
) -> Project:

    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project_name,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_name} not found.",
        )
    return project


###########################
# endpoints
###########################
@router.post(
    "/project/{project_name:path}/commit",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Project committed successfully!",
                        "commit_hash": "...",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <name> not found.",
                    },
                },
            },
        },
        400: {
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project is not versioned.",
                    },
                },
            },
        },
    },
)
def commit_project(
    request_fastapi: Request,
    request: ProjectCommitRequest,
    project: Project = Depends(get_project_or_404),
    session: Session = Depends(get_db_session),
):
    """
    Creates a new version of a project.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    try:
        commit_hash = project_dao.commit(
            project_id=project.id,
            commit_message=request.commit_message,
        )
        return {
            "info": "Project committed successfully!",
            "commit_hash": commit_hash,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# New endpoint for rolling back a project
@router.post(
    "/project/{project_name:path}/rollback",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Project rolled back successfully!"},
                },
            },
        },
        404: {
            "description": "Project or Commit Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project or commit not found.",
                    },
                },
            },
        },
        400: {
            "description": "Bad Request",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project is not versioned.",
                    },
                },
            },
        },
    },
)
def rollback_project(
    request_fastapi: Request,
    request: ProjectRollbackRequest,
    project: Project = Depends(get_project_or_404),
    session: Session = Depends(get_db_session),
):
    """
    Rolls back a project to a specific version.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    try:
        project_dao.rollback(project_id=project.id, commit_hash=request.commit_hash)
        return {"info": "Project rolled back successfully!"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/project/{project_name:path}/commits",
    response_model=List[ProjectCommitHistory],
    summary="Get project commit history",
)
def get_project_commits(
    request_fastapi: Request,
    project_name: str,
    project: Project = Depends(get_project_or_404),
    session: Session = Depends(get_db_session),
):
    """
    Retrieves the commit history for a versioned project.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)

    try:
        history = project_dao.get_commit_history(project.id)
        return history
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/project/favorites",
    response_model=List[FavoriteProjectOut],
    responses={
        200: {
            "description": "List of favorite projects",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": 1,
                            "project": "my-project",
                            "icon": "star",
                            "position": 0,
                        },
                        {
                            "id": 2,
                            "project": "another-project",
                            "icon": "folder",
                            "position": 1,
                        },
                    ],
                },
            },
        },
    },
)
def get_favorites(
    request_fastapi: Request,
    session=Depends(get_db_session),
):
    """
    Returns a list of the user's favorite projects, sorted by position.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    favorite_project_dao = FavoriteProjectDAO(session)

    favorites = favorite_project_dao.filter_by_user(request_fastapi.state.user_id)

    # Sort by position
    favorites.sort(key=lambda x: x.position)

    # Convert to response model
    result = []
    for fav in favorites:
        project_name = str(fav.project_id)
        try:
            project = project_dao.filter(id=fav.project_id)[0][0]
            if project:
                project_name = project.name
        except Exception:
            pass

        result.append(
            FavoriteProjectOut(
                id=fav.id,
                project=project_name,
                position=fav.position,
            ),
        )

    return result


@router.post(
    "/project/favorites",
    response_model=FavoriteProjectOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {
            "description": "Favorite created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "project": "my-project",
                        "icon": "star",
                        "position": 0,
                    },
                },
            },
        },
        400: {
            "description": "Invalid request or duplicate favorite",
            "content": {
                "application/json": {
                    "example": {"detail": "Project is already in favorites"},
                },
            },
        },
        404: {
            "description": "Project not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Project 'unknown-project' not found"},
                },
            },
        },
    },
)
def create_favorite(
    request_fastapi: Request,
    favorite: FavoriteProjectIn,
    session=Depends(get_db_session),
):
    """
    Creates a new favorite project for the user.

    Each favorite must include a project name, icon, and position.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    favorite_project_dao = FavoriteProjectDAO(session)

    user_id = request_fastapi.state.user_id

    # Verify project exists and user has access
    project = project_dao.get_by_user_and_name(user_id=user_id, name=favorite.project)

    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{favorite.project}' not found",
        )

    try:
        # Create new favorite
        favorite_project_dao.create(
            user_id=user_id,
            project_id=project.id,
            position=favorite.position,
        )

        favorite_project_dao.session.commit()

        new_id = (
            favorite_project_dao.session.query(FavoriteProject)
            .filter_by(user_id=user_id, project_id=project.id)
            .first()
            .id
        )

        return FavoriteProjectOut(
            id=new_id,
            project=favorite.project,
            position=favorite.position,
        )
    except ValueError:
        favorite_project_dao.session.rollback()
        raise HTTPException(status_code=400, detail="Project is already in favorites")
    except Exception as e:
        favorite_project_dao.session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create favorite: {str(e)}",
        )


@router.get(
    "/project/favorites/{id}",
    response_model=FavoriteProjectOut,
    responses={
        200: {
            "description": "Favorite project details",
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "project": "my-project",
                        "icon": "star",
                        "position": 0,
                    },
                },
            },
        },
        404: {
            "description": "Favorite not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Favorite with ID 123 not found"},
                },
            },
        },
    },
)
def get_favorite(
    request_fastapi: Request,
    id: int = Path(..., description="The ID of the favorite to retrieve"),
    session=Depends(get_db_session),
):
    """
    Returns details of a specific favorite project.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    favorite_project_dao = FavoriteProjectDAO(session)

    user_id = request_fastapi.state.user_id
    # Get the favorite
    try:
        favorite = favorite_project_dao.get_by_id(user_id, id)
    except:
        raise HTTPException(status_code=404, detail=f"Favorite with ID {id} not found")

    # Get project name from project_id
    try:
        project = project_dao.filter(id=favorite.project_id)[0][0]
        project_name = project.name if project else str(favorite.project_id)
    except:
        raise HTTPException(
            status_code=404,
            detail=f"Project with ID {favorite.project_id} not found",
        )

    # Return as response model
    return FavoriteProjectOut(
        id=favorite.id,
        project=project_name,
        position=favorite.position,
    )


@router.patch(
    "/project/favorites/{id}",
    response_model=FavoriteProjectOut,
    responses={
        200: {
            "description": "Favorite updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "project": "my-project",
                        "icon": "updated-icon",
                        "position": 2,
                    },
                },
            },
        },
        404: {
            "description": "Favorite not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Favorite with ID 123 not found"},
                },
            },
        },
    },
)
def update_favorite(
    request_fastapi: Request,
    update: FavoriteProjectUpdate,
    id: int = Path(..., description="The ID of the favorite to update"),
    session=Depends(get_db_session),
):
    """
    Updates a specific favorite project.

    Only the provided fields will be updated.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    favorite_project_dao = FavoriteProjectDAO(session)

    # Get the favorite
    user_id = request_fastapi.state.user_id
    try:
        favorite = favorite_project_dao.get_by_id(user_id, id)
    except:
        raise HTTPException(status_code=404, detail=f"Favorite with ID {id} not found")

    try:
        # Update fields if provided
        update_data = {}
        if update.position is not None:
            update_data["position"] = update.position

        # Apply updates
        if update_data:
            favorite_project_dao.update(user_id, id, **update_data)
            favorite_project_dao.session.commit()

        # Get updated favorite
        updated_favorite = favorite_project_dao.get_by_id(user_id, id)

        # Get project name from project_id
        try:
            project = project_dao.filter(id=updated_favorite.project_id)[0][0]
            project_name = project.name if project else str(updated_favorite.project_id)
        except:
            raise HTTPException(
                status_code=404,
                detail=f"Project with ID {updated_favorite.project_id} not found",
            )

        # Return updated favorite
        return FavoriteProjectOut(
            id=updated_favorite.id,
            project=project_name,
            position=updated_favorite.position,
        )
    except Exception as e:
        favorite_project_dao.session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update favorite: {str(e)}",
        )


@router.delete(
    "/project/favorites/{id}",
    responses={
        200: {
            "description": "Favorite deleted successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Favorite deleted successfully!"},
                },
            },
        },
        404: {
            "description": "Favorite not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Favorite with ID 123 not found"},
                },
            },
        },
    },
)
def delete_favorite(
    request_fastapi: Request,
    id: int = Path(..., description="The ID of the favorite to delete"),
    session=Depends(get_db_session),
):
    """
    Deletes a specific favorite project.
    """
    favorite_project_dao = FavoriteProjectDAO(session)
    # Get the favorite
    user_id = request_fastapi.state.user_id
    try:
        favorite = favorite_project_dao.get_by_id(user_id, id)
    except:
        raise HTTPException(status_code=404, detail=f"Favorite with ID {id} not found")

    try:
        # Delete the favorite
        favorite_project_dao.delete(user_id, id)
        favorite_project_dao.session.commit()

        # Return no content
        return {"info": "Favorite deleted successfully!"}
    except Exception as e:
        favorite_project_dao.session.rollback()
        raise HTTPException(
            status_code=404,
            detail=f"Failed to delete favorite: {str(e)}",
        )


@router.post(
    "/project",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Project created successfully!"},
                },
            },
        },
        400: {
            "description": "Already Existing Project",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "A logging project with this name already exists.",
                    },
                },
            },
        },
        422: {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Description must be 256 characters or less.",
                    },
                },
            },
        },
    },
)
def create_project(
    request_fastapi: Request,
    request: ProjectConfig,
    session=Depends(get_db_session),
):
    """
    Creates a logging project and adds this to your account. This project will
    have a set of logs associated with it.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)

    try:
        existing_project = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=request.name,
        )
        if existing_project:
            raise ValueError("Project already exists")
        project_dao.create(
            user_id=request_fastapi.state.user_id,
            # TODO: Add organization id when appropriate
            name=request.name,
            icon=request.icon or "folder",
            is_versioned=request.is_versioned,
            description=request.description,
            order=request.order,
        )

        return {"info": "Project created successfully!"}
    except ValueError as e:
        if "Project already exists" in str(e):
            raise HTTPException(
                status_code=400,
                detail="A logging project with this name already exists.",
            )
        else:
            raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="A logging project with this name already exists.",
        )


@router.delete(
    "/project/{project_name:path}/logs",
    responses={
        200: {
            "description": "Project logs deleted.",
            "content": {
                "application/json": {
                    "example": {"info": "All logs in project deleted successfully"},
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <name> not found.",
                    },
                },
            },
        },
    },
)
def delete_project_logs(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
    project: Project = Depends(get_project_or_404),
):
    """
    Deletes all logs in a project.
    """
    # Check if trying to delete from protected projects (Unity, AssistantJobs)
    if project.name in ["Unity", "AssistantJobs"]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"The '{project.name}' project is protected "
                "and cannot have its logs deleted.",
            ),
        )

    log_event_dao = LogEventDAO(session)
    # Get all log events for the project
    log_events = log_event_dao.filter(project_id=project.id)

    # Delete each log event (cascade delete will handle related logs)
    for event in log_events:
        log_event_dao.delete(event[0].id)

    return {"info": "All logs in project deleted successfully"}


@router.delete(
    "/project/{project_name:path}/contexts",
    responses={
        200: {
            "description": "Project contexts and logs deleted.",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Project contexts and logs deleted successfully!",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <name> not found.",
                    },
                },
            },
        },
    },
)
def delete_project_contexts(
    request_fastapi: Request,
    project: Project = Depends(get_project_or_404),
    session=Depends(get_db_session),
):
    """
    Deletes all contexts and their associated logs from a project.
    The project's interfaces remain untouched.
    """
    # Check if trying to delete from protected projects (Unity, AssistantJobs)
    if project.name in ["Unity", "AssistantJobs"]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"The '{project.name}' project is protected "
                "and cannot have its contexts deleted.",
            ),
        )

    context_dao = ContextDAO(session)
    # Get all contexts for the project
    contexts = context_dao.filter(project_id=project.id)
    for context in contexts:
        context_dao.delete(context[0].id)

    return {"info": "Project contexts and logs deleted successfully!"}


@router.delete(
    "/project/{project_name:path}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Project deleted successfully!"},
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <name> not found.",
                    },
                },
            },
        },
    },
)
def delete_project(
    request_fastapi: Request,
    project: Project = Depends(get_project_or_404),
    session=Depends(get_db_session),
):
    """
    Deletes a project from your account.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)

    # Check if trying to delete the protected projects (Unity, AssistantJobs)
    if project.name in ["Unity", "AssistantJobs"]:
        raise HTTPException(
            status_code=403,
            detail=f"The '{project.name}' project is protected and cannot be deleted.",
        )

    # Check if trying to delete the protected project (Production Traffic)
    ORGANIZATION_NAME = settings.orchestra_organization_name
    OWNER_ID = settings.orchestra_owner_id
    PROD_TRAFFIC_PROJECT_NAME = settings.orchestra_prod_traffic_name
    CHAT_COMPLETIONS_PROJECT_NAME = settings.chat_completions_project_name
    orchestra_org = (
        session.query(Organization)
        .filter(
            Organization.name == ORGANIZATION_NAME,
            Organization.owner_id == OWNER_ID,
        )
        .first()
    )
    try:
        if project.name == CHAT_COMPLETIONS_PROJECT_NAME:
            raise HTTPException(
                status_code=403,
                detail=f"The '{CHAT_COMPLETIONS_PROJECT_NAME}' project cannot be deleted.",
            )
        if (
            project.name == PROD_TRAFFIC_PROJECT_NAME
            and project.organization_id == orchestra_org.id
        ):
            raise HTTPException(
                status_code=403,
                detail=f"The '{PROD_TRAFFIC_PROJECT_NAME}' project cannot be deleted.",
            )
        project_dao.delete(id=project.id)

    except:
        raise not_found(f"Project {project.name}")

    return {"info": "Project deleted successfully"}


@router.patch(
    "/project/{project_name:path}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Project updated successfully!"},
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <name> not found.",
                    },
                },
            },
        },
        422: {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Description must be 256 characters or less.",
                    },
                },
            },
        },
    },
)
def update_project(
    request_fastapi: Request,
    request: ProjectUpdate,
    project: Project = Depends(get_project_or_404),
    session=Depends(get_db_session),
):
    """
    Updates a project's name and/or description in your account.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)

    # Check if trying to rename the protected Unity project
    if project.name == "Unity" and request.name is not None:
        raise HTTPException(
            status_code=403,
            detail="The 'Unity' project cannot be renamed.",
        )

    try:
        # Update the project with provided fields
        if request.name is not None:
            # Rename functionality
            project_dao.rename(
                user_id=request_fastapi.state.user_id,
                name=project.name,
                new_name=request.name,
            )

        update_kwargs = {}
        if request.description is not None:
            update_kwargs["description"] = request.description
        if request.icon is not None:
            update_kwargs["icon"] = request.icon
        if request.order is not None:
            update_kwargs["order"] = request.order

        if update_kwargs:
            project_dao.update(id=project.id, **update_kwargs)

        return {"info": "Project updated successfully!"}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        raise not_found(f"Project {project.name}")


@router.post(
    "/project/{project_id}/transfer-to-organization",
    response_model=TransferResponse,
    responses={
        200: {
            "description": "Project transferred successfully",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "project_id": 123,
                        "project_name": "my-project",
                        "from_type": "personal",
                        "to_type": "organization",
                        "message": "Project successfully transferred to organization 'My Org'",
                    },
                },
            },
        },
        400: {
            "description": "Bad Request - Project already in organization or invalid state",
        },
        403: {
            "description": "Forbidden - User doesn't have required permissions",
        },
        404: {
            "description": "Project or Organization Not Found",
        },
    },
)
def transfer_project_to_organization(
    request_fastapi: Request,
    project_id: int,
    transfer_request: TransferToOrganizationRequest,
    session: Session = Depends(get_db_session),
) -> TransferResponse:
    """
    Transfer a personal project to an organization.

    Requirements:
    - User must own the personal project (project.user_id == user_id)
    - User must have org:write permission on target organization
    - Project must be personal (organization_id = NULL)

    Process:
    - Sets project.organization_id = target org
    - Sets project.user_id = NULL (org-owned)
    - No ResourceAccess entries created (implicit membership handles access)
    """
    user_id = request_fastapi.state.user_id
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get project
    project = session.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project with id {project_id} not found",
        )

    # Verify project is personal
    if project.organization_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is already associated with an organization. "
            "Use transfer-to-personal first if you want to move it to a different organization.",
        )

    # Verify user owns the project
    if project.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not own this project",
        )

    # Get target organization
    org = org_dao.get(transfer_request.organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with id {transfer_request.organization_id} not found",
        )

    # Verify user has org:write permission on target organization
    has_permission = resource_access_dao.check_user_permission(
        user_id,
        "org",
        transfer_request.organization_id,
        "org:write",
    )
    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not have permission to add projects to organization '{org.name}'",
        )

    try:
        # Transfer the project (direct SQLAlchemy update to ensure None is set)
        project.organization_id = transfer_request.organization_id
        project.user_id = None  # Org-owned projects don't have user_id
        session.commit()

        return TransferResponse(
            success=True,
            project_id=project_id,
            project_name=project.name,
            from_type="personal",
            to_type="organization",
            message=f"Project '{project.name}' successfully transferred to organization '{org.name}'",
        )
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to transfer project: {str(e)}",
        )


@router.post(
    "/project/{project_id}/transfer-to-personal",
    response_model=TransferResponse,
    responses={
        200: {
            "description": "Project transferred successfully",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "project_id": 123,
                        "project_name": "my-project",
                        "from_type": "organization",
                        "to_type": "personal",
                        "message": "Project successfully transferred to personal ownership",
                    },
                },
            },
        },
        400: {
            "description": "Bad Request - Project is already personal",
        },
        403: {
            "description": "Forbidden - User doesn't have required permissions",
        },
        404: {
            "description": "Project Not Found",
        },
    },
)
def transfer_project_to_personal(
    request_fastapi: Request,
    project_id: int,
    session: Session = Depends(get_db_session),
) -> TransferResponse:
    """
    Transfer an organizational project to personal ownership.

    Requirements:
    - Project must be organizational (organization_id IS NOT NULL)
    - User must have project:delete permission OR be org owner

    Process:
    - Sets project.user_id = requesting user
    - Sets project.organization_id = NULL
    - Deletes all ResourceAccess entries for this project
    - Deletes all team shares for this project

    Warning: This is a destructive operation that removes team sharing.
    """
    user_id = request_fastapi.state.user_id
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    org_dao = OrganizationDAO(session)
    resource_access_dao = ResourceAccessDAO(session)

    # Get project
    project = session.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project with id {project_id} not found",
        )

    # Verify project is organizational
    if project.organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is already personal",
        )

    # Get organization
    org = org_dao.get(project.organization_id)

    # Verify user has project:delete permission OR is org owner
    has_delete_permission = resource_access_dao.check_user_permission(
        user_id,
        "project",
        project_id,
        "project:delete",
    )

    is_org_owner = org and org.owner_id == user_id

    if not (has_delete_permission or is_org_owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to transfer this project to personal ownership. "
            "Only users with project:delete permission or organization owners can do this.",
        )

    try:
        # Delete all ResourceAccess entries for this project
        session.query(ResourceAccess).filter(
            ResourceAccess.resource_type == "project",
            ResourceAccess.resource_id == project_id,
        ).delete()

        # Transfer the project to personal ownership (direct SQLAlchemy update to ensure None is set)
        project.user_id = user_id
        project.organization_id = None
        session.commit()

        return TransferResponse(
            success=True,
            project_id=project_id,
            project_name=project.name,
            from_type="organization",
            to_type="personal",
            message=f"Project '{project.name}' successfully transferred to personal ownership. "
            "All team shares have been removed.",
        )
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to transfer project: {str(e)}",
        )


@router.get(
    "/project/{project_name:path}",
    response_model=ProjectOut,
    responses={
        200: {
            "description": "Project details",
            "content": {
                "application/json": {
                    "example": {
                        "name": "my-project",
                        "description": "A sample project for evaluation",
                        "is_versioned": True,
                        "created_at": "2023-01-01T00:00:00Z",
                        "updated_at": "2023-01-02T00:00:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <name> not found.",
                    },
                },
            },
        },
    },
)
def get_project(
    request_fastapi: Request,
    project: Project = Depends(get_project_or_404),
    session=Depends(get_db_session),
):
    """
    Returns detailed information about a specific project.
    """
    return ProjectOut(
        name=project.name,
        description=project.description,
        icon=project.icon,
        is_versioned=project.is_versioned,
        created_at=project.created_at.isoformat() if project.created_at else None,
        updated_at=project.updated_at.isoformat() if project.updated_at else None,
    )


@router.get(
    "/projects",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        "project_a",
                        "project_b",
                        "project_c",
                    ],
                },
            },
        },
    },
)
def list_projects(
    request_fastapi: Request,
    session=Depends(get_db_session),
):
    """
    Returns the names of all projects stored in your account.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)

    raw_projects = project_dao.filter_by_user_access(
        user_id=request_fastapi.state.user_id,
    )
    return [p[0].name for p in raw_projects]


@router.get("/projects/tree", response_model=List[ProjectTreeItem])
async def list_projects_tree(
    request_fastapi: Request,
    session: Session = Depends(get_db_session),
):
    """Return all projects the user can access with their icons and interface names."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    favorite_project_dao = FavoriteProjectDAO(session)

    projects = project_dao.filter_by_user_access(user_id=request_fastapi.state.user_id)
    favorites = favorite_project_dao.filter_by_user(request_fastapi.state.user_id)
    fav_map = {f.project_id: f for f in favorites}

    # Extract project objects and IDs for bulk queries
    project_list = [proj_row[0] for proj_row in projects]
    project_ids = [proj.id for proj in project_list]

    # Bulk query: Get all interfaces for all projects in one query
    all_interfaces = interface_dao.get_interfaces_bulk(
        project_ids=project_ids,
        is_checkpoint=False,
    )

    # Group interfaces by project_id for efficient lookup
    interfaces_by_project = {}
    for interface in all_interfaces:
        if interface.project_id not in interfaces_by_project:
            interfaces_by_project[interface.project_id] = []
        interfaces_by_project[interface.project_id].append(interface)

    # Bulk query: Get all tabs for all interfaces in one query
    interface_ids = [str(interface.id) for interface in all_interfaces]
    all_tabs = tab_dao.list_tabs_bulk(
        interface_ids=interface_ids,
        is_checkpoint=False,
    )

    # Group tabs by interface_id for efficient lookup
    tabs_by_interface = {}
    for tab in all_tabs:
        if tab.interface_id not in tabs_by_interface:
            tabs_by_interface[tab.interface_id] = []
        tabs_by_interface[tab.interface_id].append(tab)

    # Build the response structure efficiently
    items: List[ProjectTreeItem] = []
    for proj in project_list:
        interfaces = interfaces_by_project.get(proj.id, [])

        interface_items: List[InterfaceInfo] = []
        for interface in interfaces:
            tabs = tabs_by_interface.get(str(interface.id), [])
            tab_items = [TabInfo(name=t.name, icon=t.icon, order=t.order) for t in tabs]
            # tabs are already sorted by order from the bulk query
            interface_items.append(
                InterfaceInfo(
                    name=interface.name,
                    icon=interface.icon,
                    order=interface.order,
                    tabs=tab_items,
                ),
            )
        # interfaces are already sorted by order from the bulk query

        fav_entry = fav_map.get(proj.id)
        items.append(
            ProjectTreeItem(
                project=proj.name,
                icon=proj.icon,
                order=proj.order,
                interfaces=interface_items,
                favorite=fav_entry is not None,
                position=fav_entry.position if fav_entry else None,
            ),
        )

    # Sort: favorites first by position, then non-favorites by order
    items.sort(
        key=lambda x: (
            not x.favorite,
            x.position if x.position is not None else 1e9,
            x.order,
        ),
    )
    return items


# Template Endpoints for Projects
@router.post(
    "/project/export_template",
    response_model=TemplateExportResponse,
    responses={
        200: {
            "description": "Project template exported successfully",
            "content": {
                "application/json": {
                    "example": {
                        "template": {
                            "interfaces": [
                                {
                                    "name": "Analytics Dashboard",
                                    "tabs": [
                                        {
                                            "name": "Overview",
                                            "tiles": [
                                                {
                                                    "name": "Data Table",
                                                    "type": "Table",
                                                    "position": {
                                                        "x": 0,
                                                        "y": 0,
                                                        "width": 6,
                                                        "height": 4,
                                                    },
                                                },
                                            ],
                                        },
                                    ],
                                },
                            ],
                        },
                        "metadata": {"exported_at": "2024-01-01T12:00:00Z"},
                        "export_stats": {"interfaces": 1, "tabs": 1, "tiles": 1},
                    },
                },
            },
        },
    },
)
def export_project_template(
    request_fastapi: Request,
    request: ExportProjectTemplateRequest,
    session: Session = Depends(get_db_session),
):
    """Export project interfaces as a reusable template."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)

    # Get the project
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=request.project,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project} not found or you don't have access.",
        )

    # Get interfaces to export
    interfaces = interface_dao.get_interfaces(
        project_id=project.id,
        is_checkpoint=request.checkpoint,
    )

    # Filter interfaces if specific names are provided
    if request.interface_names:
        interfaces = [
            interface
            for interface in interfaces
            if interface.name in request.interface_names
        ]

    # Convert interfaces to templates
    interface_templates = []
    total_tabs = 0
    total_tiles = 0

    for interface in interfaces:
        # Get tabs with tiles for this interface
        tabs = tab_dao.list_tabs(
            interface_id=interface.id,
            is_checkpoint=interface.is_checkpoint,
        )

        # Ensure tabs have their tiles loaded
        interface.tabs = tabs

        # Convert to template
        interface_template = TemplateConverter.interface_to_template(
            interface=interface,
            description=request.description,
            created_by=request_fastapi.state.user_id,
            tags=request.tags,
        )

        interface_templates.append(interface_template)
        total_tabs += len(interface_template.tabs)
        total_tiles += sum(len(tab.tiles) for tab in interface_template.tabs)

    # Create project template
    project_template = ProjectTemplateSchema(
        interfaces=interface_templates,
        description=request.description,
        created_by=request_fastapi.state.user_id,
        tags=request.tags,
    )

    # Create metadata
    from datetime import datetime, timezone

    metadata = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by": request_fastapi.state.user_id,
        "source_project": request.project,
        "template_name": request.template_name or f"{request.project}_template",
    }

    # Calculate export stats
    export_stats = {
        "interfaces": len(interface_templates),
        "tabs": total_tabs,
        "tiles": total_tiles,
    }

    return TemplateExportResponse(
        template=project_template.model_dump(),
        metadata=metadata,
        export_stats=export_stats,
    )


@router.post(
    "/project/import_template",
    response_model=TemplateImportResponse,
    responses={
        200: {
            "description": "Project template imported successfully",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "import_stats": {"interfaces": 2, "tabs": 4, "tiles": 10},
                        "created_ids": {"interface_ids": ["abc123", "def456"]},
                        "warnings": [],
                    },
                },
            },
        },
    },
)
def import_project_template(
    request_fastapi: Request,
    request: ImportProjectTemplateRequest,
    session: Session = Depends(get_db_session),
):
    """Import a project template into a project."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # Get target project
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=request.project,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project} not found or you don't have access.",
        )

    validation_result = None
    warnings = []
    created_interface_ids = []
    import_stats = {"interfaces": 0, "tabs": 0, "tiles": 0}

    # Validate template if requested
    if request.validate_first:
        validator = TemplateValidator(session)
        validation_schema = validator.get_project_validation_schema(
            user_id=request_fastapi.state.user_id,
            project_name=request.project,
        )

    # Import each interface from the project template
    for interface_data in request.template.interfaces:
        # Determine interface name with optional prefix
        interface_name = interface_data.name or "Imported Interface"
        if request.interface_name_prefix:
            interface_name = f"{request.interface_name_prefix}{interface_name}"

        # Check for name conflicts
        existing_interface = interface_dao.get_by_project_and_name(
            project_id=project.id,
            name=interface_name,
            is_checkpoint=False,
        )

        if existing_interface and not request.overwrite_existing:
            warnings.append(f"Skipped interface '{interface_name}' - already exists")
            continue

        # Create the interface
        interface = interface_dao.create_interface(
            name=interface_name,
            project_id=project.id,
            color=interface_data.color,
            is_checkpoint=False,
        )

        created_interface_ids.append(str(interface.id))
        import_stats["interfaces"] += 1

        # Create tabs and tiles for this interface
        for tab_data in interface_data.tabs:
            tab = tab_dao.create_tab(
                interface_id=str(interface.id),
                name=tab_data.name or "Imported Tab",
                visible=tab_data.visible if tab_data.visible is not None else True,
                active=tab_data.active if tab_data.active is not None else False,
                order=tab_data.order if tab_data.order is not None else 0,
                context=tab_data.context,
                color=tab_data.color,
                is_checkpoint=False,
            )
            import_stats["tabs"] += 1

            # Create tiles for this tab
            for tile_data in tab_data.tiles:
                position = tile_data.position or {
                    "x": 0,
                    "y": 0,
                    "width": 4,
                    "height": 4,
                }

                tile = tile_dao.create_tile(
                    tab_id=str(tab.id),
                    name=tile_data.name or "Imported Tile",
                    type=tile_data.type,
                    x_position=(
                        position.get("x", 0)
                        if isinstance(position, dict)
                        else getattr(position, "x", 0)
                    ),
                    y_position=(
                        position.get("y", 0)
                        if isinstance(position, dict)
                        else getattr(position, "y", 0)
                    ),
                    width=(
                        position.get("width", 4)
                        if isinstance(position, dict)
                        else getattr(position, "width", 4)
                    ),
                    height=(
                        position.get("height", 4)
                        if isinstance(position, dict)
                        else getattr(position, "height", 4)
                    ),
                    minW=tile_data.minW,
                    minH=tile_data.minH,
                    visible=(
                        tile_data.visible if tile_data.visible is not None else True
                    ),
                    locked=tile_data.locked if tile_data.locked is not None else False,
                    moved=tile_data.moved if tile_data.moved is not None else False,
                    static=tile_data.static if tile_data.static is not None else False,
                    color=tile_data.color,
                    context=tile_data.context,
                    table=tile_data.table,
                    auto_update=tile_data.auto_update,
                    freeze=tile_data.freeze,
                    filters=tile_data.filters,
                    common_filter=tile_data.common_filter,
                    metric=tile_data.metric,
                    column_context=tile_data.column_context,
                    grouping=tile_data.grouping,
                    is_checkpoint=False,
                    # Pass specialized tile data as dictionaries
                    table_tile=(
                        tile_data.table_tile.model_dump()
                        if tile_data.table_tile
                        else None
                    ),
                    plot_tile=(
                        tile_data.plot_tile.model_dump()
                        if tile_data.plot_tile
                        else None
                    ),
                    view_tile=(
                        tile_data.view_tile.model_dump()
                        if tile_data.view_tile
                        else None
                    ),
                    editor_tile=(
                        tile_data.editor_tile.model_dump()
                        if tile_data.editor_tile
                        else None
                    ),
                    terminal_tile=(
                        tile_data.terminal_tile.model_dump()
                        if tile_data.terminal_tile
                        else None
                    ),
                )
                import_stats["tiles"] += 1

        # Set active tab if specified
        active_tab_name = interface_data.active_tab_name
        if active_tab_name:
            tabs = tab_dao.list_tabs(
                interface_id=str(interface.id),
                is_checkpoint=False,
            )
            for tab in tabs:
                if tab.name == active_tab_name:
                    interface_dao.update_interface(
                        id=str(interface.id),
                        active_tab_id=str(tab.id),
                    )
                    break

    created_ids = {"interface_ids": created_interface_ids}

    return TemplateImportResponse(
        success=True,
        validation_result=validation_result,
        import_stats=import_stats,
        created_ids=created_ids,
        warnings=warnings,
    )


###########################
# Admin endpoints
###########################
# Admin router for protected endpoints
admin_router = APIRouter()


@admin_router.post(
    "/share-project",
    responses={
        200: {
            "description": "Project shared successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Project shared successfully!"},
                },
            },
        },
        404: {
            "description": "User or Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "User or Project not found.",
                    },
                },
            },
        },
    },
)
def admin_share_project(
    request: ShareProjectRequest,
    session=Depends(get_db_session),
):
    """
    Admin endpoint to share a project between users.
    This enables real-time collaboration between users.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    auth_user_dao = AuthUserDAO(session)
    organization_dao = OrganizationDAO(session)

    # Lookup the from_user and to_user
    from_user = auth_user_dao.get_by_id(request.from_user_id)
    to_user = auth_user_dao.get_by_id(request.to_user_id)

    if not from_user or not to_user:
        raise not_found("User")

    # Retrieve the project
    try:
        project = project_dao.get_by_user_and_name(
            user_id=request.from_user_id,
            name=request.project_name,
        )
    except HTTPException:
        raise not_found(f"Project {request.project_name}")

    # Handle organization assignment
    if project.organization_id is None:
        # Project is not associated with an organization yet
        # Try to find an existing organization for from_user
        orgs = organization_dao.filter(owner_id=request.from_user_id)

        if orgs:
            # Use existing organization
            organization = orgs[0][0]
        else:
            # Create a new organization
            org_name = f"{from_user[0].email.split('@')[0]}'s Organization"
            organization_dao.create(name=org_name, owner_id=request.from_user_id)
            organization_dao.session.commit()

            # Re-fetch the newly created organization
            orgs = organization_dao.filter(owner_id=request.from_user_id)
            organization = orgs[0][0]

        # Update the project to be associated with the organization
        project_dao.update(
            id=project.id,
            organization_id=organization.id,
            user_id=None,  # Remove user_id as it's now org-owned
        )
    else:
        # Project already belongs to an organization
        orgs = organization_dao.filter(id=project.organization_id)
        organization = orgs[0][0]

    # Add the to_user to the organization
    organization_member_dao.create(
        organization_id=organization.id,
        user_id=request.to_user_id,
        level="admin",  # Give admin access to the shared user
    )

    # Commit all changes
    organization_member_dao.session.commit()
    project_dao.session.commit()

    return {"info": "Project shared successfully!"}


@admin_router.post(
    "/duplicate-project",
    responses={
        200: {
            "description": "Project duplicated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Project 'source-project' duplicated successfully to 'new-project'!",
                        "details": {
                            "project_id": 123,
                            "contexts_copied": 5,
                            "field_types_copied": 10,
                            "log_events_copied": 20,
                            "logs_copied": 100,
                            "json_logs_copied": 50,
                            "derived_logs_copied": 15,
                            "interfaces_copied": 3,
                            "tabs_copied": 2,
                            "tiles_copied": 10,
                            "table_tiles_copied": 5,
                            "plot_tiles_copied": 3,
                            "editor_tiles_copied": 2,
                            "view_tiles_copied": 1,
                            "terminal_tiles_copied": 1,
                        },
                    },
                },
            },
        },
        404: {
            "description": "User or Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "User or Project not found.",
                    },
                },
            },
        },
        400: {
            "description": "Project Already Exists",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "A project with this name already exists for the target user.",
                    },
                },
            },
        },
    },
)
def admin_duplicate_project(
    request: DuplicateProjectRequest,
    session=Depends(get_db_session),
):
    """
    Admin endpoint to deep-copy (duplicate) a project from one user to another.

    This creates a complete clone of a project, copying all sub-resources:
    - Contexts
    - Field Types
    - Log Events
    - Logs
    - JSON Logs
    - Derived Logs
    - Interfaces
    - Tabs
    - Tiles
    - Table Tiles
    - Plot Tiles
    - Editor Tiles
    - View Tiles
    - Terminal Tiles

    The duplicate is a separate project where changes in one do not affect the other.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    auth_user_dao = AuthUserDAO(session)
    log_event_dao = LogEventDAO(session)
    derived_log_dao = DerivedLogDAO(session)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # 1. Validate users exist
    from_user = auth_user_dao.get_by_id(request.from_user_id)
    to_user = auth_user_dao.get_by_id(request.to_user_id)

    if not from_user or not to_user:
        raise not_found("User")

    # 2. Retrieve the source project
    try:
        source_project = project_dao.get_by_user_and_name(
            user_id=request.from_user_id,
            name=request.from_project_name,
        )
    except HTTPException:
        raise not_found(f"Project {request.from_project_name}")

    # 3. Check if a project with the new name already exists for the target user
    existing_project = project_dao.get_by_user_and_name(
        user_id=request.to_user_id,
        name=request.new_project_name,
    )

    if existing_project:
        raise HTTPException(
            status_code=400,
            detail="A project with this name already exists for the target user.",
        )

    # 4. Create a new project for the target user
    project_dao.create(
        user_id=request.to_user_id,
        name=request.new_project_name,
        description=source_project.description,
    )
    session.flush()  # Flush to get the new project ID

    # Get the new project to ensure we have all fields
    new_project = project_dao.get_by_user_and_name(
        user_id=request.to_user_id,
        name=request.new_project_name,
    )

    # Initialize counters for the response
    stats = {
        "project_id": new_project.id,
        "contexts_copied": 0,
        "field_types_copied": 0,
        "log_events_copied": 0,
        "logs_copied": 0,
        "json_logs_copied": 0,
        "derived_logs_copied": 0,
        "interfaces_copied": 0,
        "tabs_copied": 0,
        "tiles_copied": 0,
        "table_tiles_copied": 0,
        "plot_tiles_copied": 0,
        "editor_tiles_copied": 0,
        "view_tiles_copied": 0,
        "terminal_tiles_copied": 0,
    }

    # Create mappings to track old IDs to new IDs
    context_id_map = {}
    log_event_id_map = {}

    # 5. Duplicate Contexts using bulk insert with RETURNING
    contexts = context_dao.filter(project_id=source_project.id)
    context_values = []
    old_context_ids = []

    for ctx_tuple in contexts:
        ctx = ctx_tuple[0]
        old_context_ids.append(ctx.id)
        context_values.append(
            {
                "project_id": new_project.id,
                "name": ctx.name,
                "description": ctx.description,
                "is_versioned": ctx.is_versioned,
                "allow_duplicates": ctx.allow_duplicates,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "old_id": ctx.id,  # Temporary field to track old ID
            },
        )

    if context_values:
        # Bulk insert contexts and get back the new IDs
        stmt = (
            sqlalchemy.insert(Context)
            .values(
                [
                    {k: v for k, v in ctx.items() if k != "old_id"}
                    for ctx in context_values
                ],
            )
            .returning(Context.id)
        )

        result = session.execute(stmt)
        new_context_ids = [row[0] for row in result]

        # Build the context ID mapping
        for i, old_id in enumerate(old_context_ids):
            context_id_map[old_id] = new_context_ids[i]

        stats["contexts_copied"] = len(context_values)

    # 6. Duplicate Field Types using bulk insert
    if context_id_map:
        field_types = (
            session.query(FieldType)
            .filter(
                FieldType.context_id.in_(list(context_id_map.keys())),
            )
            .all()
        )

        field_type_values = []
        for ft in field_types:
            field_type_values.append(
                {
                    "project_id": new_project.id,
                    "context_id": context_id_map[ft.context_id],
                    "field_name": ft.field_name,
                    "field_type": ft.field_type,
                    "mutable": ft.mutable,
                    "field_category": ft.field_category,
                },
            )

        if field_type_values:
            stmt = sqlalchemy.insert(FieldType).values(field_type_values)
            session.execute(stmt)
            stats["field_types_copied"] = len(field_type_values)

    # 7. Duplicate Log Events using bulk insert with RETURNING
    log_events = log_event_dao.filter(project_id=source_project.id)
    log_event_values = []
    old_log_event_ids = []

    for le_tuple in log_events:
        le = le_tuple[0]
        old_log_event_ids.append(le.id)
        log_event_values.append(
            {
                "project_id": new_project.id,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
        )

    if log_event_values:
        # Bulk insert log events and get back the new IDs
        stmt = (
            sqlalchemy.insert(LogEvent).values(log_event_values).returning(LogEvent.id)
        )
        result = session.execute(stmt)
        new_log_event_ids = [row[0] for row in result]

        # Build the log event ID mapping
        for i, old_id in enumerate(old_log_event_ids):
            log_event_id_map[old_id] = new_log_event_ids[i]

        stats["log_events_copied"] = len(log_event_values)

    # 8. Duplicate Log Event Context relationships using bulk insert
    if log_event_id_map and context_id_map:
        log_event_contexts = (
            session.query(LogEventContext)
            .filter(
                LogEventContext.log_event_id.in_(list(log_event_id_map.keys())),
            )
            .all()
        )

        lec_values = []
        for lec in log_event_contexts:
            # Only create if both mappings exist
            if (
                lec.log_event_id in log_event_id_map
                and lec.context_id in context_id_map
            ):
                lec_values.append(
                    {
                        "log_event_id": log_event_id_map[lec.log_event_id],
                        "context_id": context_id_map[lec.context_id],
                    },
                )

        if lec_values:
            stmt = sqlalchemy.insert(LogEventContext).values(lec_values)
            session.execute(stmt)

    # 9. Duplicate Logs using batched bulk insert
    if log_event_id_map:
        # Query for Log objects directly
        logs = (
            session.query(Log, LogEventLog.log_event_id)
            .join(LogEventLog, LogEventLog.log_id == Log.id)
            .filter(
                LogEventLog.log_event_id.in_(list(log_event_id_map.keys())),
            )
            .all()
        )

        log_values = []
        log_id_map = {}  # Map old log id to new log id for LogEventLog associations
        for log, log_event_id in logs:
            log_values.append(
                {
                    "key": log.key,
                    "value": log.value,
                    "param_version": log.param_version,
                    "inferred_type": log.inferred_type,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            log_id_map[log.id] = log_event_id_map[
                log_event_id
            ]  # Store mapping for later use

        # Process logs in batches to avoid memory issues
        batch_size = 5000
        new_log_ids = []
        for i in range(0, len(log_values), batch_size):
            batch = log_values[i : i + batch_size]
            if batch:
                stmt = sqlalchemy.insert(Log).values(batch).returning(Log.id)
                result = session.execute(stmt)
                new_log_ids.extend([row[0] for row in result])

        # Create LogEventLog associations
        log_event_log_values = []
        old_log_ids = list(log_id_map.keys())
        for i, new_log_id in enumerate(new_log_ids):
            if i < len(old_log_ids):
                old_log_id = old_log_ids[i]
                new_log_event_id = log_id_map[old_log_id]
                log_event_log_values.append(
                    {
                        "log_event_id": new_log_event_id,
                        "log_id": new_log_id,
                    },
                )

        # Bulk insert LogEventLog associations
        for i in range(0, len(log_event_log_values), batch_size):
            batch = log_event_log_values[i : i + batch_size]
            if batch:
                stmt = sqlalchemy.insert(LogEventLog).values(batch)
                session.execute(stmt)

        stats["logs_copied"] = len(log_values)

    # 10. Duplicate JSON Logs using batched bulk insert
    if log_event_id_map:
        # Query for JSONLog objects via LogEventJSONLog association
        json_logs_with_event_ids = (
            session.query(JSONLog, LogEventJSONLog.log_event_id)
            .join(LogEventJSONLog, LogEventJSONLog.json_log_id == JSONLog.id)
            .filter(
                LogEventJSONLog.log_event_id.in_(list(log_event_id_map.keys())),
            )
            .all()
        )

        # Prepare JSONLog values and associations
        json_log_values = []
        json_log_associations = []  # Track (old_log_event_id, key) -> new_log_event_id

        for jl, old_log_event_id in json_logs_with_event_ids:
            new_log_event_id = log_event_id_map[old_log_event_id]
            json_log_values.append(
                {
                    "key": jl.key,
                    "value": jl.value,
                },
            )
            json_log_associations.append((old_log_event_id, jl.key, new_log_event_id))

        # Process JSON logs in batches to avoid memory issues
        batch_size = 5000
        all_new_json_log_ids = []

        for i in range(0, len(json_log_values), batch_size):
            batch = json_log_values[i : i + batch_size]
            if batch:
                # Insert JSONLogs and get their IDs
                stmt = sqlalchemy.insert(JSONLog).values(batch).returning(JSONLog.id)
                result = session.execute(stmt)
                batch_ids = [row[0] for row in result]
                all_new_json_log_ids.extend(batch_ids)

        # Create LogEventJSONLog associations
        log_event_json_log_values = []
        for i, (old_log_event_id, key, new_log_event_id) in enumerate(
            json_log_associations,
        ):
            if i < len(all_new_json_log_ids):
                log_event_json_log_values.append(
                    {
                        "log_event_id": new_log_event_id,
                        "json_log_id": all_new_json_log_ids[i],
                    },
                )

        # Insert associations in batches
        for i in range(0, len(log_event_json_log_values), batch_size):
            batch = log_event_json_log_values[i : i + batch_size]
            if batch:
                stmt = sqlalchemy.insert(LogEventJSONLog).values(batch)
                session.execute(stmt)

        stats["json_logs_copied"] = len(json_log_values)

    # 11. Duplicate Derived Logs using bulk insert
    if log_event_id_map:
        derived_log_values = []
        derived_log_associations = (
            []
        )  # Track (old_log_event_id, new_log_event_id) for associations

        for old_log_event_id, new_log_event_id in log_event_id_map.items():
            derived_logs = derived_log_dao.filter(log_event_id=old_log_event_id)

            for dl_tuple in derived_logs:
                dl = dl_tuple[0]

                # Update referenced_logs to use new log_event IDs
                referenced_logs = dl.referenced_logs
                new_referenced_logs = {}

                for ref_key, ref_log in referenced_logs.items():
                    if isinstance(ref_log, list):
                        new_referenced_logs[ref_key] = [
                            log_event_id_map.get(lg_id, lg_id) for lg_id in ref_log
                        ]
                    else:
                        new_referenced_logs[ref_key] = ref_log

                derived_log_values.append(
                    {
                        "key": dl.key,
                        "equation": dl.equation,
                        "referenced_logs": new_referenced_logs,
                        "value": dl.value,
                        "inferred_type": dl.inferred_type,
                        "created_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
                derived_log_associations.append((old_log_event_id, new_log_event_id))

        # Process derived logs in batches and collect their IDs
        batch_size = 1000
        all_new_derived_log_ids = []

        for i in range(0, len(derived_log_values), batch_size):
            batch = derived_log_values[i : i + batch_size]
            if batch:
                stmt = (
                    sqlalchemy.insert(DerivedLog).values(batch).returning(DerivedLog.id)
                )
                result = session.execute(stmt)
                batch_ids = [row[0] for row in result]
                all_new_derived_log_ids.extend(batch_ids)

        # Create LogEventDerivedLog associations
        log_event_derived_log_values = []
        for i, (old_log_event_id, new_log_event_id) in enumerate(
            derived_log_associations,
        ):
            if i < len(all_new_derived_log_ids):
                log_event_derived_log_values.append(
                    {
                        "log_event_id": new_log_event_id,
                        "derived_log_id": all_new_derived_log_ids[i],
                    },
                )

        # Bulk insert LogEventDerivedLog associations
        for i in range(0, len(log_event_derived_log_values), batch_size):
            batch = log_event_derived_log_values[i : i + batch_size]
            if batch:
                stmt = sqlalchemy.insert(LogEventDerivedLog).values(batch)
                session.execute(stmt)

        stats["derived_logs_copied"] = len(derived_log_values)

    # 12. Duplicate Interfaces, Tabs, Tiles and specialized tile types
    # Use the DAO methods to duplicate the hierarchical data

    # Duplicate interfaces
    interface_result = interface_dao.duplicate_interfaces(
        source_project_id=source_project.id,
        target_project_id=new_project.id,
    )
    interface_id_map = interface_result["id_map"]
    stats["interfaces_copied"] = interface_result["count"]

    # Duplicate tabs for the interfaces
    if interface_id_map:
        tab_result = tab_dao.duplicate_tabs(interface_id_map)
        tab_id_map = tab_result["id_map"]
        stats["tabs_copied"] = tab_result["count"]

        # Duplicate tiles for the tabs
        if tab_id_map:
            tile_result = tile_dao.duplicate_tiles(tab_id_map)
            stats["tiles_copied"] = tile_result["tile_count"]
            stats["table_tiles_copied"] = tile_result["table_tile_count"]
            stats["plot_tiles_copied"] = tile_result["plot_tile_count"]
            stats["view_tiles_copied"] = tile_result["view_tile_count"]
            stats["editor_tiles_copied"] = tile_result["editor_tile_count"]
            stats["terminal_tiles_copied"] = tile_result["terminal_tile_count"]

    # 13. Commit all changes
    session.commit()

    return {
        "info": f"Project '{request.from_project_name}' duplicated successfully to '{request.new_project_name}'!",
        "details": stats,
    }
