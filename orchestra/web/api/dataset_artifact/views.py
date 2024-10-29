"""
Includes endpoints related to dataset artifacts.
"""

import json

from fastapi import APIRouter, Depends, Path, Request

from orchestra.db.dao.dataset_artifact_dao import DatasetArtifactDAO
from orchestra.db.dao.dataset_dao import DatasetDAO
from orchestra.web.api.dataset_artifact.schema import DatasetArtifactConfig
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


###########################
# endpoints
###########################


@router.post(
    "/dataset/{dataset:path}/artifacts",
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
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Dataset <dataset> not found.",
                    },
                },
            },
        },
    },
)
def create_artifacts(
    request_fastapi: Request,
    request: DatasetArtifactConfig,
    dataset: str = Path(
        description="Name of the dataset the artifacts belong to.",
        example="eval-dataset",
    ),
    dataset_dao: DatasetDAO = Depends(),
    artifact_dao: DatasetArtifactDAO = Depends(),
):
    """
    Creates one or more artifacts associated to a dataset. Artifacts are
    dataset-level metadata that don't depend on other variables.
    """

    try:
        # TODO: Add organization id
        user_id = request_fastapi.state.user_id
        _dataset_id = dataset_dao.get_id(
            user_id=user_id,
            name=dataset,
            include_public=False,
        )
        if _dataset_id is None:
            raise IndexError
        for k, v in request.artifacts.items():
            v_str = json.dumps(v)
            artifact_dao.create(dataset_id=_dataset_id, key=k, value=v_str)
        return {"info": "Artifact(s) created successfully!"}
    except:
        raise not_found(f"Dataset {dataset}")


@router.delete(
    "/dataset/{dataset:path}/artifacts/{key}",
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
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Dataset <dataset> not found.",
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
    dataset: str = Path(
        description="Name of the dataset to delete an artifact from.",
        example="eval-dataset",
    ),
    key: str = Path(
        description="Key of the artifact to delete.",
        example="dataset-description",
    ),
    dataset_dao: DatasetDAO = Depends(),
    dataset_artifact_dao: DatasetArtifactDAO = Depends(),
):
    """
    Deletes an artifact from a dataset.
    """
    try:
        # TODO: Deal with org when appropriate
        user_id = request_fastapi.state.user_id
        dataset_id = dataset_dao.get_id(
            user_id=user_id,
            name=dataset,
            include_public=False,
        )
        if dataset_id is None:
            raise IndexError
    except IndexError:
        raise not_found(f"Dataset {dataset}")
    try:
        artifact_id = dataset_artifact_dao.filter(dataset_id=dataset_id, key=key)[0][
            0
        ].id
    except IndexError:
        raise not_found(f"Artifact {key}")
    dataset_artifact_dao.delete(id=artifact_id)
    return {"info": "Artifact deleted successfully!"}


@router.get(
    "/dataset/{dataset:path}/artifacts",
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
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Dataset <dataset> not found.",
                    },
                },
            },
        },
    },
)
def list_artifacts(
    request_fastapi: Request,
    dataset: str = Path(
        description="Name of the dataset to delete an artifact from.",
        example="eval-dataset",
    ),
    dataset_dao: DatasetDAO = Depends(),
    dataset_artifact_dao: DatasetArtifactDAO = Depends(),
):
    """
    Returns the key-value pairs of all artifacts in a dataset.
    """
    try:
        # TODO: Deal with org when appropriate
        user_id = request_fastapi.state.user_id
        dataset_id = dataset_dao.get_id(
            user_id=user_id,
            name=dataset,
            include_public=False,
        )
        if dataset_id is None:
            raise IndexError
    except IndexError:
        raise not_found(f"Dataset {dataset}")
    raw_artifacts = dataset_artifact_dao.filter(dataset_id=dataset_id)
    return {ra[0].key: json.loads(ra[0].value) for ra in raw_artifacts}
