"""
Includes endpoints related to log projects.
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.web.api.project.schema import ProjectConfig

router = APIRouter()


###########################
# endpoints
###########################


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
    },
)
def create_project(
    request_fastapi: Request,
    request: ProjectConfig,
    project_dao: ProjectDAO = Depends(),
):
    """
    Creates a logging project and adds this to your account. This project will
    have a set of logs associated with it.
    """

    try:
        existing_project = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            # TODO: Add organization id
            name=request.name,
        )
        if existing_project:
            raise ValueError
        project_dao.create(
            user_id=request_fastapi.state.user_id,
            # TODO: Add organization id when appropriate
            name=request.name,
        )

        return {"info": "Project created successfully!"}
    except:
        raise HTTPException(
            status_code=400,
            detail="A logging project with this name already exists.",
        )


@router.delete(
    "/project/{name}",
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
                    "detail": "Project <name> not found in your account.",
                },
            },
        },
    },
)
def delete_project(
    request_fastapi: Request,
    name: str = Path(
        description="Name of the project to delete.",
        example="eval-project",
    ),
    project_dao: ProjectDAO = Depends(),
):
    """
    Deletes a project from your account.
    """
    try:
        project_id = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            # TODO: Deal with org when appropriate
            name=name,
        )[0][0].id
        project_dao.delete(id=project_id)
    except (IndexError, ValueError):
        raise HTTPException(
            status_code=404,
            detail=f"Project {name} not found in your account.",
        )
    return {"info": "Project deleted successfully"}


@router.patch(
    "/project/{name}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Project renamed successfully!"},
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "detail": "Project <name> not found in your account.",
                },
            },
        },
    },
)
def rename_project(
    request_fastapi: Request,
    request: ProjectConfig,
    name: str = Path(
        description="Name of the project to rename.",
        example="old-project-name",
    ),
    project_dao: ProjectDAO = Depends(),
):
    """
    Renames a project from `name` to `new_name` in your account.
    """
    try:
        project_dao.rename(
            user_id=request_fastapi.state.user_id,
            # TODO: Deal with organization id properly
            name=name,
            new_name=request.name,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=f"Project {name} not found in your account.",
        )
    return {"info": "Project renamed successfully!"}


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
    project_dao: ProjectDAO = Depends(),
):
    """
    Returns the names of all projects stored in your account.
    """
    raw_projects = project_dao.filter(
        user_id=request_fastapi.state.user_id,
        # TODO: Deal with organization id properly
    )
    return [p[0].name for p in raw_projects]
