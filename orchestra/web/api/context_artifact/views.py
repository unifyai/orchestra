"""
Includes endpoints related to context artifacts.
"""

import json

from fastapi import APIRouter, Depends, Path, Request

from orchestra.db.dao.artifact_dao import ArtifactDAO
from orchestra.db.dao.context_artifact_dao import ContextArtifactDAO
from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.context_artifact.schema import ContextArtifactCreateRequest
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


###########################
# endpoints
###########################


@router.post(
    "/project/{project}/contexts/{context_name:path}/artifacts",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Context artifact(s) created successfully!"},
                },
            },
        },
        404: {
            "description": "Project or Context Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> or context <context> not found.",
                    },
                },
            },
        },
    },
)
def create_context_artifacts(
    request_fastapi: Request,
    request: ContextArtifactCreateRequest,
    project: str = Path(
        description="Name of the project the context belongs to.",
        example="eval-project",
    ),
    context_name: str = Path(
        description="Name of the context to create artifacts in.",
        example="experiment1/trial1",
    ),
    session=Depends(get_db_session),
):
    """
    Creates one or more artifacts associated to a context within a project.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    context_artifact_dao = ContextArtifactDAO(session)

    try:
        user_id = request_fastapi.state.user_id
        project_id = project_dao.filter(user_id=user_id, name=project)[0][0].id
        context_id = context_dao.filter(project_id=project_id, name=context_name)[0][
            0
        ].id

        for k, v in request.artifacts.items():
            v_str = json.dumps(v)
            context_artifact_dao.create(context_id=context_id, key=k, value=v_str)
        return {"info": "Context artifact(s) created successfully!"}
    except IndexError:
        raise not_found(f"Project {project} or context {context_name}")


@router.delete(
    "/project/{project}/contexts/{context:path}/artifacts/{key}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Context artifact deleted successfully!"},
                },
            },
        },
        404: {
            "description": "Project, Context, or Artifact Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project>, context <context>, or artifact <key> not found.",
                    },
                },
            },
        },
    },
)
def delete_context_artifact(
    request_fastapi: Request,
    project: str = Path(
        description="Name of the project containing the context.",
        example="eval-project",
    ),
    context: str = Path(
        description="Name of the context to delete an artifact from.",
        example="training",
    ),
    key: str = Path(
        description="Key of the artifact to delete.",
        example="context-description",
    ),
    session=Depends(get_db_session),
):
    """
    Deletes an artifact from a context within a project.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    artifact_dao = ArtifactDAO(session)

    try:
        user_id = request_fastapi.state.user_id
        project_id = project_dao.filter(user_id=user_id, name=project)[0][0].id
        context_id = context_dao.filter(project_id=project_id, name=context)[0][0].id
    except IndexError:
        raise not_found(f"Project {project} or context {context}")

    try:
        artifact_id = artifact_dao.filter(context_id=context_id, key=key)[0][0].id
    except IndexError:
        raise not_found(f"Artifact {key}")

    artifact_dao.delete(id=artifact_id)
    return {"info": "Context artifact deleted successfully!"}


@router.get(
    "/project/{project}/contexts/{context_name:path}/artifacts",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "artifact_1": "value_1",
                        "artifact_2": "value_2",
                    },
                },
            },
        },
        404: {
            "description": "Project or Context Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> or context <context> not found.",
                    },
                },
            },
        },
    },
)
def list_context_artifacts(
    request_fastapi: Request,
    project: str = Path(
        description="Name of the project containing the context.",
        example="eval-project",
    ),
    context_name: str = Path(
        description="Name of the context to list artifacts from.",
        example="training",
    ),
    session=Depends(get_db_session),
):
    """
    Returns the key-value pairs of all artifacts in a context.
    """
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    context_artifact_dao = ContextArtifactDAO(session)

    try:
        user_id = request_fastapi.state.user_id
        project_id = project_dao.filter(user_id=user_id, name=project)[0][0].id
        context_id = context_dao.filter(project_id=project_id, name=context_name)[0][
            0
        ].id
    except IndexError:
        raise not_found(f"Project {project} or context {context_name}")

    raw_artifacts = context_artifact_dao.filter(context_id=context_id)
    return {ra[0].key: json.loads(ra[0].value) for ra in raw_artifacts}
