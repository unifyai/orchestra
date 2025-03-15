"""
Includes endpoints related to context management within projects.
"""

from typing import Union

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.web.api.context.schema import (
    AddLogsToContextRequest,
    ContextCreateRequest,
)
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


@router.post(
    "/project/{project_name}/contexts",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "name": "experiment1/trial1",
                        "description": "Context for experiment 1 trial 1",
                        "is_versioned": True,
                        "version": 1,
                    },
                },
            },
        },
        400: {
            "description": "Already Existing Context",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "A context with this name already exists in the project.",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project not found.",
                    },
                },
            },
        },
    },
)
def create_context(
    request_fastapi: Request,
    request: Union[ContextCreateRequest, str],
    project_name: str = Path(
        description="Name of the project to create context in.",
        example="my_project",
    ),
    project_dao: ProjectDAO = Depends(),
    context_dao: ContextDAO = Depends(),
):
    """
    Creates a new context within a project. Contexts can be used to organize logs
    and artifacts within a project.

    If is_versioned=True, all logs in this context will be versioned and mutable.
    The context version will increment automatically when logs are added, updated, or removed.

    The context can be provided as a string (which will be used as the name with no description)
    or as an object with name and description fields.
    """
    try:
        project = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            name=project_name,
        )
        if not project:
            raise IndexError
        project_id = project[0][0].id

        # Handle string input for context name
        context_name = request.name
        context_description = request.description
        context_is_versioned = getattr(request, "is_versioned", False)

        # If request is a string, use it as the name with no description
        if isinstance(request, str):
            context_name = request
            context_description = None
            context_is_versioned = False

        # Validate context name
        if not re.match(r"^[a-zA-Z0-9\_\-/]+$", context_name) or "//" in context_name:
            raise HTTPException(
                status_code=400,
                detail="Invalid context name. Names can only contain alphanumeric characters, underscores, dashes, and forward slashes. Consecutive slashes are not allowed.",
            )

        existing_context = context_dao.filter(
            project_id=project_id,
            name=context_name,
        )
        if existing_context:
            raise ValueError("Context already exists")

        context_dao.create(
            project_id=project_id,
            name=context_name,
            description=context_description,
            is_versioned=context_is_versioned,
        )

        return {"info": "Context created successfully."}
    except IndexError:
        raise not_found("Project")
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail="A context with this name already exists in the project.",
        )


@router.get(
    "/project/{project_name}/contexts",
    responses={
        200: {
            "description": "Contexts retrieved.",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "name": "context1",
                            "description": "description1",
                            "is_versioned": True,
                            "version": 1,
                        },
                        {
                            "name": "context2",
                            "description": "description2",
                            "is_versioned": False,
                            "version": 1,
                        },
                    ],
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project not found.",
                    },
                },
            },
        },
    },
)
def get_contexts(
    request_fastapi: Request,
    project_name: str = Path(
        description="Name of the project to create context in.",
        example="my_project",
    ),
    project_dao: ProjectDAO = Depends(),
    context_dao: ContextDAO = Depends(),
):
    """
    Get a list of contexts within a project.
    Returns information about each context including its versioning status and current version.
    """
    try:
        project = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            name=project_name,
        )
        if not project:
            raise IndexError
        project_id = project[0][0].id
        existing_contexts = context_dao.filter(project_id=project_id)
        # filter out default context
        if not existing_contexts:
            return []
        return [
            {
                "name": context[0].name,
                "description": context[0].description,
            }
            for context in existing_contexts
            if context[0].name != ""
        ]
    except IndexError:
        raise not_found("Project")


@router.get(
    "/project/{project_name}/contexts/{context_name}",
    responses={
        200: {
            "description": "Context retrieved.",
            "content": {
                "application/json": {
                    "example": {
                        "name": "context1",
                        "description": "description1",
                        "is_versioned": True,
                        "version": 1,
                    },
                },
            },
        },
        404: {
            "description": "Project or Context Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project or context not found.",
                    },
                },
            },
        },
    },
)
def get_context(
    request_fastapi: Request,
    project_name: str = Path(
        description="Name of the project containing the context.",
        example="my_project",
    ),
    context_name: str = Path(
        description="Name of the context to retrieve.",
        example="my_context",
    ),
    project_dao: ProjectDAO = Depends(),
    context_dao: ContextDAO = Depends(),
):
    """
    Get information about a specific context including its versioning status and current version.
    """
    try:
        project = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            name=project_name,
        )
        if not project:
            raise IndexError("Project not found")
        project_id = project[0][0].id

        context = context_dao.filter(
            project_id=project_id,
            name=context_name,
        )
        if not context:
            raise IndexError("Context not found")

        return {
            "name": context[0][0].name,
            "description": context[0][0].description,
            "is_versioned": context[0][0].is_versioned,
            "version": context[0][0].version,
        }
    except IndexError as e:
        raise not_found(str(e))


@router.delete(
    "/project/{project_name}/contexts/{context_name:path}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Context deleted successfully!"},
                },
            },
        },
        404: {
            "description": "Project or Context Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project or context not found.",
                    },
                },
            },
        },
    },
)
def delete_context(
    request_fastapi: Request,
    project_name: str = Path(
        description="Name of the project to create context in.",
        example="my_project",
    ),
    context_name: str = Path(
        description="Name of the context to delete.",
        example="my_context",
    ),
    project_dao: ProjectDAO = Depends(),
    context_dao: ContextDAO = Depends(),
):
    """
    Deletes a context from a project. This will not delete the logs or artifacts
    within the context, but will remove their association with this context.
    """
    try:
        project = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            name=project_name,
        )
        if not project:
            raise IndexError("Project not found")
        project_id = project[0][0].id

        context = context_dao.filter(
            project_id=project_id,
            name=context_name,
        )
        if not context:
            raise IndexError("Context not found")

        context_dao.delete(id=context[0][0].id)
        return {"info": "Context deleted successfully!"}
    except IndexError as e:
        raise not_found(str(e))


@router.post(
    "/project/{project_name}/contexts/add_logs",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Logs added to context successfully!"},
                },
            },
        },
        404: {
            "description": "Project, Context or Logs Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project, context or specified logs not found.",
                    },
                },
            },
        },
    },
)
def add_logs_to_context(
    request_fastapi: Request,
    request: AddLogsToContextRequest,
    project_name: str = Path(
        description="Name of the project to create context in.",
        example="my_project",
    ),
    project_dao: ProjectDAO = Depends(),
    context_dao: ContextDAO = Depends(),
):
    """
    Adds existing logs to a context within a project. The logs must already exist
    in the project and can be specified by their IDs.
    The same logs can be associated with multiple contexts.

    The context_name can be provided as a string or as an object with a name field.
    If the context doesn't exist, it will be created automatically.
    """
    try:
        project = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            name=project_name,
        )
        if not project:
            raise IndexError("Project not found")
        project_id = project[0][0].id

        # Try to get the context, or create it if it doesn't exist
        context_name = request.context_name
        # Handle string or object input for context
        if isinstance(context_name, str):
            context_name_value = context_name
        else:
            context_name_value = context_name.get("name")

        context = context_dao.filter(
            project_id=project_id,
            name=context_name_value,
        )

        # Implicitly create the context if it doesn't exist
        if not context:
            context_id = context_dao.create(
                project_id=project_id,
                name=context_name_value,
                description=None,  # Default description to None for implicitly created contexts
                is_versioned=False,  # Default to non-versioned
            )
        else:
            context_id = context[0][0].id

        context_dao.add_logs(
            context_id=context_id,
            log_ids=request.log_ids,
        )
        return {"info": "Logs added to context successfully!"}
    except IndexError as e:
        raise not_found(str(e))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="One or more specified logs do not exist in the project.",
        )
