"""
Includes endpoints related to log artifacts.
"""

import json

from fastapi import APIRouter, Depends, Path, Request

from orchestra.db.dao.artifact_dao import ArtifactDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.web.api.artifact.schema import ArtifactConfig
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


###########################
# endpoints
###########################


@router.post(
    "/project/{project}/artifacts",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Artifact(s) created successfully!"},
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def create_artifacts(
    request_fastapi: Request,
    request: ArtifactConfig,
    project: str = Path(
        description="Name of the project the artifacts belong to.",
        example="eval-project",
    ),
    project_dao: ProjectDAO = Depends(),
    artifact_dao: ArtifactDAO = Depends(),
):
    """
    Creates one or more artifacts associated to a project. Artifacts are
    project-level metadata that don't depend on other variables.
    """

    try:
        # TODO: Add organization id
        user_id = request_fastapi.state.user_id
        _project_id = project_dao.filter(user_id=user_id, name=project)[0][0].id
        for k, v in request.artifacts.items():
            v_str = json.dumps(v)
            artifact_dao.create(project_id=_project_id, key=k, value=v_str)
        return {"info": "Artifact(s) created successfully!"}
    except:
        raise not_found(f"Project {project}")


@router.delete(
    "/project/{project}/artifacts/{key}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Artifact deleted successfully!"},
                },
            },
        },
        404_1: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
        404_2: {
            "description": "Artifact Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Artifact <key> not found.",
                    },
                },
            },
        },
    },
)
def delete_artifact(
    request_fastapi: Request,
    project: str = Path(
        description="Name of the project to delete an artifact from.",
        example="eval-project",
    ),
    key: str = Path(
        description="Key of the artifact to delete.",
        example="project-description",
    ),
    project_dao: ProjectDAO = Depends(),
    artifact_dao: ArtifactDAO = Depends(),
):
    """
    Deletes an artifact from a project.
    """
    try:
        # TODO: Deal with org when appropriate
        user_id = request_fastapi.state.user_id
        project_id = project_dao.filter(user_id=user_id, name=project)[0][0].id
    except IndexError:
        raise not_found(f"Project {project}")
    try:
        artifact_id = artifact_dao.filter(project_id=project_id, key=key)[0][0].id
    except IndexError:
        raise not_found(f"Artifact {key}")
    artifact_dao.delete(id=artifact_id)
    return {"info": "Artifact deleted successfully!"}


@router.get(
    "/project/{project}/artifacts",
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
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def list_artifacts(
    request_fastapi: Request,
    project: str = Path(
        description="Name of the project to delete an artifact from.",
        example="eval-project",
    ),
    project_dao: ProjectDAO = Depends(),
    artifact_dao: ArtifactDAO = Depends(),
):
    """
    Returns the key-value pairs of all artifacts in a project.
    """
    try:
        # TODO: Deal with org when appropriate
        user_id = request_fastapi.state.user_id
        project_id = project_dao.filter(user_id=user_id, name=project)[0][0].id
    except IndexError:
        raise not_found(f"Project {project}")
    raw_artifacts = artifact_dao.filter(project_id=project_id)
    return {ra[0].key: json.loads(ra[0].value) for ra in raw_artifacts}
