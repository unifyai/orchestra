"""
Includes endpoints related to log projects.
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.log_event_dao import LogEventDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.web.api.project.schema import ProjectConfig
from orchestra.web.api.utils.http_responses import not_found

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
    name: str = Path(
        description="Name of the project to delete.",
        example="eval-project",
    ),
    project_dao: ProjectDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
    context_dao: ContextDAO = Depends(),
):
    """
    Deletes a project from your account.
    """
    try:
        # Get the project
        project = project_dao.filter(user_id=request_fastapi.state.user_id, name=name)[
            0
        ][0]

        # Get all contexts for this project and delete them
        # This will cascade delete log_event_context associations
        contexts = context_dao.filter(project_id=project.id)
        for context in contexts:
            context_dao.delete(context[0].id)

        # Now get and delete any remaining log events
        # This will cascade delete logs and derived logs
        log_events = log_event_dao.filter(project_id=project.id)
        for event in log_events:
            log_event_dao.delete(event[0].id)

        # Finally delete the project
        # This will cascade delete interfaces and temp_interfaces
        project_dao.delete(id=project.id)

    except (IndexError, ValueError):
        raise not_found(f"Project {name}")

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
                    "example": {
                        "detail": "Project <name> not found.",
                    },
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
        raise not_found(f"Project {name}")
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


@router.delete(
    "/project/{name}/logs",
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
    name: str,
    project_dao: ProjectDAO = Depends(),
    log_event_dao: LogEventDAO = Depends(),
):
    """
    Deletes all logs in a project.
    """
    # Verify project exists and user has access
    projects = project_dao.filter(
        user_id=request_fastapi.state.user_id,
        name=name,
    )
    if len(projects) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"Project {name} not found.",
        )

    project_id = projects[0][0].id

    # Get all log events for the project
    log_events = log_event_dao.filter(project_id=project_id)

    # Delete each log event (cascade delete will handle related logs)
    for event in log_events:
        log_event_dao.delete(event[0].id)

    return {"info": "All logs in project deleted successfully"}


@router.delete(
    "/project/{name}/contexts",
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
    name: str = Path(
        description="Name of the project to delete contexts from.",
        example="test-project",
    ),
    project_dao: ProjectDAO = Depends(),
    context_dao: ContextDAO = Depends(),
):
    """
    Deletes all contexts and their associated logs from a project.
    The project's interfaces remain untouched.
    """
    # Verify project exists and user has access
    projects = project_dao.filter(
        user_id=request_fastapi.state.user_id,
        name=name,
    )
    if len(projects) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"Project {name} not found.",
        )

    project_id = projects[0][0].id

    # Get all contexts for the project
    contexts = context_dao.filter(project_id=project_id)
    for context in contexts:
        context_dao.delete(context[0].id)

    return {"info": "Project contexts and logs deleted successfully!"}
