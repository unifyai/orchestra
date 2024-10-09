"""
Includes endpoints related to log artifacts.
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from orchestra.db.dao.artifact_dao import ArtifactDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.web.api.artifact.schema import ArtifactConfig

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
                        "detail": "A project with this name doesn't exists.",
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
        json_schema_extra={"example": "eval-project"},
    ),
    project_dao: ProjectDAO = Depends(),
    artifact_dao: ArtifactDAO = Depends(),
):
    """
    Creates one or more artifacts associated to a project. Artifacts are
    project-level metadata that don't depend on other variables.
    """

    try:
        existing_project = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            # TODO: Add organization id
            name=project,
        )
        if not existing_project:
            raise ValueError
        for k, v in request.artifacts.items():
            artifact_dao.create(
                project_id=existing_project[0][0].id,
                key=k,
                value=v,
            )

        return {"info": "Artifact(s) created successfully!"}
    except:
        raise HTTPException(
            status_code=404,
            detail="A project with this name doesn't exists.",
        )


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
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "detail": "Project <name> not found in your account.",
                },
            },
        },
        404: {
            "description": "Artifact Not Found",
            "content": {
                "application/json": {
                    "detail": "Artifact <key> not found in this project.",
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
        project_id = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            # TODO: Deal with org when appropriate
            name=project,
        )[0][0].id
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found in your account.",
        )
    try:
        artifact_id = artifact_dao.filter(project_id=project_id, key=key)[0][0].id
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Artifact {key} not found in this project.",
        )
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
                    "detail": "Project <project> not found in your account.",
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
        project_id = project_dao.filter(
            user_id=request_fastapi.state.user_id,
            # TODO: Deal with org when appropriate
            name=project,
        )[0][0].id
    except IndexError:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found in your account.",
        )
    raw_artifacts = artifact_dao.filter(project_id=project_id)
    return {ra[0].key: ra[0].value for ra in raw_artifacts}
