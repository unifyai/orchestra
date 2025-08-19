"""
Includes endpoints related to context management within projects.
"""

import re
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy.exc import IntegrityError

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.field_type_dao import FieldTypeDAO
from orchestra.db.dao.log_dao import LogDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.context.schema import (
    AddLogsToContextRequest,
    ContextCommit,
    ContextCommitHistory,
    ContextCreateRequest,
    ContextRollback,
    RenameContextRequest,
)
from orchestra.web.api.log.views import _get_logs_query
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


@router.post(
    "/project/{project_name:path}/contexts",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "name": "experiment1/trial1",
                        "description": "Context for experiment 1 trial 1",
                        "is_versioned": True,
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
    session=Depends(get_db_session),
):
    """
    Creates a new context within a project. Contexts can be used to organize logs
    and artifacts within a project.

    If is_versioned=True, all logs in this context will be versioned and mutable.
    The context version will increment automatically when logs are added, updated, or removed.

    The context can be provided as a string (which will be used as the name with no description)
    or as an object with name and description fields.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    try:
        project = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=project_name,
        )
        if not project:
            raise IndexError
        project_id = project.id

        if isinstance(request, str):
            context_name = request
            context_description = None
            context_is_versioned = False
            context_allow_duplicates = True
            unique_keys = None
        else:
            context_name = request.name
            context_description = request.description
            context_is_versioned = request.is_versioned
            context_allow_duplicates = request.allow_duplicates
            unique_keys = request.unique_keys

        # Normalize context name: remove leading slash to treat '/exp1/name1' the same as 'exp1/name1'
        context_name = context_name.lstrip("/")

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
            allow_duplicates=context_allow_duplicates,
            unique_keys=unique_keys,
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
    "/project/{project_name:path}/contexts",
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
                            "allow_duplicates": True,
                            "unique_keys": ["row_id"],
                        },
                        {
                            "name": "context2",
                            "description": "description2",
                            "is_versioned": False,
                            "allow_duplicates": True,
                            "unique_keys": ["user_id", "session_id"],
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
    prefix: Optional[str] = Query(
        None,
        description="Optional prefix to filter contexts by name",
        example="experiment1/",
    ),
    session=Depends(get_db_session),
):
    """
    Get a list of contexts within a project.
    Returns information about each context including its versioning status and current version.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)

    try:
        project = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=project_name,
        )
        if not project:
            raise IndexError
        project_id = project.id
        existing_contexts = context_dao.filter(project_id=project_id)
        # filter out default context
        if not existing_contexts:
            return []

        contexts = [
            {
                "name": context[0].name,
                "description": context[0].description,
                "is_versioned": context[0].is_versioned,
                "allow_duplicates": context[0].allow_duplicates,
                "unique_keys": (
                    context[0].unique_keys_order
                    if hasattr(context[0], "unique_keys_order")
                    and context[0].unique_keys_order
                    else (
                        list(context[0].unique_keys.keys())
                        if isinstance(context[0].unique_keys, dict)
                        and context[0].unique_keys
                        else []
                    )
                ),
            }
            for context in existing_contexts
            if context[0].name != ""
        ]

        # Filter by prefix if provided
        if prefix:
            contexts = [
                context for context in contexts if context["name"].startswith(prefix)
            ]

        return contexts
    except IndexError:
        raise not_found("Project")


@router.get(
    "/project/{project_name:path}/contexts/{context_name:path}/commits",
    response_model=List[ContextCommitHistory],
    summary="Get context commit history",
)
def get_context_commits(
    request_fastapi: Request,
    project_name: str,
    context_name: str,
    session=Depends(get_db_session),
):
    """
    Retrieves the commit history for a versioned context.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    user_id = request_fastapi.state.user_id

    project = project_dao.get_by_user_and_name(user_id=user_id, name=project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    context_obj = context_dao.filter(project_id=project.id, name=context_name)
    if not context_obj:
        raise HTTPException(status_code=404, detail="Context not found")
    context_id = context_obj[0][0].id

    try:
        history = context_dao.get_commit_history(context_id)
        return history
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/project/{project_name:path}/contexts/{context_name:path}",
    responses={
        200: {
            "description": "Context retrieved.",
            "content": {
                "application/json": {
                    "example": {
                        "name": "context1",
                        "description": "description1",
                        "is_versioned": True,
                        "allow_duplicates": True,
                        "unique_keys": ["row_id"],
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
    session=Depends(get_db_session),
):
    """
    Get information about a specific context including its versioning status and current version.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    try:
        project = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=project_name,
        )
        if not project:
            raise IndexError("Project not found")
        project_id = project.id

        # Normalize context name: remove leading slash to treat '/exp1/name1' the same as 'exp1/name1'
        context_name = context_name.lstrip("/")

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
            "allow_duplicates": context[0][0].allow_duplicates,
            "unique_keys": (
                context[0][0].unique_keys_order
                if hasattr(context[0][0], "unique_keys_order")
                and context[0][0].unique_keys_order
                else (
                    list(context[0][0].unique_keys.keys())
                    if isinstance(context[0][0].unique_keys, dict)
                    and context[0][0].unique_keys
                    else []
                )
            ),
        }
    except IndexError as e:
        raise not_found(str(e))


@router.delete(
    "/project/{project_name:path}/contexts/{context_name:path}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Context deleted successfully!"},
                },
            },
        },
        403: {
            "description": "Forbidden Operation",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Cannot delete built-in Tasks context.",
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
    session=Depends(get_db_session),
):
    """
    Deletes a context from a project. This will not delete the logs or artifacts
    within the context, but will remove their association with this context.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    # Normalize context name: remove leading slash to treat '/exp1/name1' the same as 'exp1/name1'
    context_name = context_name.lstrip("/")

    # Protect the built-in Tasks context in Unity project
    if project_name == "Unity" and context_name == "Tasks":
        raise HTTPException(
            status_code=403,
            detail="Cannot delete built-in Tasks context.",
        )
    try:
        project = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=project_name,
        )
        if not project:
            raise IndexError("Project not found")
        project_id = project.id

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
    "/project/{project_name:path}/contexts/add_logs",
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
    session=Depends(get_db_session),
):
    """
    Adds existing logs to a context within a project. The logs must already exist
    in the project and can be specified by their IDs or by log_args criteria.
    The same logs can be associated with multiple contexts.

    The context_name can be provided as a string or as an object with a name field.
    If the context doesn't exist, it will be created automatically.

    If copy=True, new copies of the logs will be created and added to the context.
    If copy=False (default), the existing logs will be associated with the context.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    field_type_dao = FieldTypeDAO(session)
    log_dao = LogDAO(session, context_dao)
    try:
        project = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=project_name,
        )
        if not project:
            raise IndexError("Project not found")
        project_id = project.id

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
                allow_duplicates=True,  # Default to allowing duplicates
            )
        else:
            context_id = context[0][0].id

        # Check if either log_ids or log_args is provided
        log_ids = []
        if hasattr(request, "log_ids") and request.log_ids:
            log_ids = request.log_ids
        elif hasattr(request, "log_args") and request.log_args:
            # Use log_args to query for matching logs
            log_args = request.log_args
            raw_rows, _, _ = _get_logs_query(
                request_fastapi,
                project_name,
                column_context=log_args.get("column_context"),
                context=log_args.get("context"),
                filter_expr=log_args.get("filter_expr"),
                sorting=log_args.get("sorting"),
                from_ids=log_args.get("from_ids"),
                exclude_ids=log_args.get("exclude_ids"),
                from_fields=log_args.get("from_fields"),
                exclude_fields=log_args.get("exclude_fields"),
                limit=log_args.get("limit"),
                offset=log_args.get("offset", 0),
                project_dao=project_dao,
                field_type_dao=field_type_dao,
                context_dao=context_dao,
                session=session,
            )

            log_ids = list({row[7] for row in raw_rows})

            if not log_ids:
                raise HTTPException(
                    status_code=400,
                    detail="No logs found matching the provided log_args criteria.",
                )
        else:
            # Neither log_ids nor log_args provided
            raise HTTPException(
                status_code=400,
                detail="Either log_ids or log_args must be provided.",
            )

        # Add logs to context based on copy flag
        try:
            if hasattr(request, "copy") and request.copy:
                # Create copies of logs and add them to the context
                context_dao.add_logs_copy(
                    context_id=context_id,
                    log_ids=log_ids,
                )
            else:
                # Associate existing logs with the context
                context_dao.add_logs(
                    context_id=context_id,
                    log_ids=log_ids,
                )
        except ValueError as e:
            if "duplicate" in str(e).lower():
                raise HTTPException(
                    status_code=400,
                    detail=str(e),
                )
            raise

        # Implicitly create field types for any fields in the logs
        # First, get existing field types for this context to avoid redundant creation
        existing_fields = field_type_dao.get_field_types(
            project_id=project_id,
            context_id=context_id,
        )
        existing_field_names = set(existing_fields)
        logs = log_dao.filter(
            project_id=project_id,
            log_event_id=log_ids,
        )

        # Create field types for each field found, but only if not already existing
        for row in logs:
            field_name = row[0].key
            value = row[0].value
            param_version = row[0].param_version

            # Skip if field already exists in this context
            if field_name not in existing_field_names:
                field_type_dao.create_field_type_if_absent(
                    project_id=project_id,
                    field_name=field_name,
                    value=value,
                    context_id=context_id,
                    mutable=False,
                    field_category="param" if param_version is not None else "entry",
                )
                # Add to set to prevent duplicate creation in this batch
                existing_field_names.add(field_name)

        return {"info": "Logs added to context successfully!"}
    except IndexError as e:
        raise not_found(str(e))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="One or more specified logs do not exist in the project.",
        )


@router.patch(
    "/project/{project_name:path}/contexts/{context_name:path}/rename",
    responses={
        200: {
            "description": "Context renamed successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Context renamed successfully!"},
                },
            },
        },
        400: {
            "description": "Context with new name already exists",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "A context with this name already exists in the project.",
                    },
                },
            },
        },
        403: {
            "description": "Forbidden Operation",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Cannot modify built-in Tasks context.",
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
def rename_context(
    request_fastapi: Request,
    body: RenameContextRequest,
    project_name: str = Path(...),
    context_name: str = Path(...),
    session=Depends(get_db_session),
):
    """Rename an existing context within a project."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)

    # Normalize context name: remove leading slash to treat '/exp1/name1' the same as 'exp1/name1'
    context_name = context_name.lstrip("/")

    # Protect the built-in Tasks context in Unity project
    if project_name == "Unity" and context_name == "Tasks":
        raise HTTPException(
            status_code=403,
            detail="Cannot modify built-in Tasks context.",
        )
    # 1) Verify project
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project_name,
    )
    if not project:
        raise not_found("Project")
    # 2) Load context
    ctx_list = context_dao.filter(
        project_id=project.id,
        name=context_name,
    )
    if not ctx_list:
        raise not_found("Context")
    ctx_id = ctx_list[0][0].id
    # 3) Attempt rename
    try:
        # Normalize new context name: remove any leading slash from provided name
        new_name = body.name.lstrip("/")
        context_dao.update(id=ctx_id, name=new_name)
    except IntegrityError:
        raise HTTPException(
            status_code=400,
            detail="A context with this name already exists in the project.",
        )
    return {"info": "Context renamed successfully!"}


@router.post(
    "/project/{project_name:path}/contexts/{context_name:path}/commit",
    summary="Commit a context version",
)
def commit_context_version(
    request_fastapi: Request,
    project_name: str,
    context_name: str,
    commit_data: ContextCommit,
    session=Depends(get_db_session),
):
    """
    Creates a new version snapshot for a specific context.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    user_id = request_fastapi.state.user_id

    project = project_dao.get_by_user_and_name(user_id=user_id, name=project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    context_obj = context_dao.filter(project_id=project.id, name=context_name)
    if not context_obj:
        raise HTTPException(status_code=404, detail="Context not found")
    context_id = context_obj[0][0].id

    try:
        commit_hash = context_dao.commit(context_id, commit_data.commit_message)
        return {"commit_hash": commit_hash}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/project/{project_name:path}/contexts/{context_name:path}/rollback",
    summary="Rollback a context to a version",
)
def rollback_context_version(
    request_fastapi: Request,
    project_name: str,
    context_name: str,
    rollback_data: ContextRollback,
    session=Depends(get_db_session),
):
    """
    Rolls back a context to a specific version by commit hash.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    user_id = request_fastapi.state.user_id

    project = project_dao.get_by_user_and_name(user_id=user_id, name=project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    context_obj = context_dao.filter(project_id=project.id, name=context_name)
    if not context_obj:
        raise HTTPException(status_code=404, detail="Context not found")
    context_id = context_obj[0][0].id

    try:
        context_dao.rollback(context_id, rollback_data.commit_hash)
        return {
            "status": "success",
            "message": f"Context {context_name} rolled back to {rollback_data.commit_hash}",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
