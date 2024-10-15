"""
Endpoints related to dataset management and operations.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from starlette import status

from orchestra.db.dao.dataset_dao import DatasetDAO
from orchestra.db.dao.dataset_entry_dao import DatasetEntryDAO
from orchestra.web.api.datasetv2.schema import (
    DatasetInfo,
    DatasetNewName,
    EntriesConfig,
)
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


@router.get(
    "/datasetsv2/",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        {"name": "dataset1"},
                        {"name": "folder/dataset2"},
                    ],
                },
            },
        },
    },
)
def list_datasets(
    request: Request,
    dataset_dao: DatasetDAO = Depends(),
):
    """
    Retrieve a list of all datasets.
    """
    datasets = dataset_dao.list_datasets(user_id=request.state.user_id)
    return [{"name": d.name} for d in datasets]


@router.get(
    "/datasetv2/{name:path}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        {"id": "puzfJbFPmZ", "entry": "...", "created_at": "..."},
                        {"id": "eAdXb2X3f8", "entry": "...", "created_at": "..."},
                    ],
                },
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {
                "application/json": {"example": {"detail": "Dataset not found."}},
            },
        },
    },
)
def get_dataset_entries(
    request: Request,
    name: str = Path(
        ...,
        description="Dataset name (can include forward slashes)",
        example="my_dataset",
    ),
    limit: int = Query(10, ge=1, le=200),
    offset: int = Query(0, ge=0),
    dataset_dao: DatasetDAO = Depends(),
    dataset_entry_dao: DatasetEntryDAO = Depends(),
):
    """
    Retrieve entries from a specific dataset.
    """
    dataset_id = dataset_dao.get_id(
        user_id=request.state.user_id,
        name=name,
        include_public=True,
    )
    if dataset_id is None:
        raise not_found("Dataset")
    # Get entries of the dataset
    raw_entries = dataset_entry_dao.filter(
        dataset_id=dataset_id,
        limit=limit,
        offset=offset,
    )
    entries = [
        {
            "id": e.id,
            "entry": json.loads(e.entry),
            "created_at": e.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for e in raw_entries
    ]
    return entries


# @router.get("/datasetv2/{name}/url")
# def get_dataset_url(
#     request: Request,
#     name: str = Path(..., description="Dataset name (can include forward slashes)"),
#     dataset_dao: DatasetDAO = Depends(),
# ):
#     """
#     Retrieve a download URL for a specific dataset.
#     """
#     raise NotImplementedError


@router.get(
    "/datasetv2/{name:path}/entry/{id}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "id": "uoreHGVKfQ",
                        "entry": "...",
                        "created_at": "...",
                    },
                },
            },
        },
        404_1: {
            "description": "Dataset Not Found",
            "content": {"application/json": {"example": "Dataset not found."}},
        },
        404_2: {
            "description": "Dataset entry Not Found",
            "content": {
                "application/json": {"example": "Dataset entry <id> not found."},
            },
        },
    },
)
def get_dataset_entry(
    request: Request,
    name: str = Path(
        ...,
        description="Dataset name (can include forward slashes)",
        example="my_dataset",
    ),
    id: str = Path(..., description="Entry ID", example="123"),
    dataset_dao: DatasetDAO = Depends(),
    dataset_entry_dao: DatasetEntryDAO = Depends(),
):
    """
    Retrieve a specific entry from a dataset.
    """
    dataset_id = dataset_dao.get_id(
        user_id=request.state.user_id,
        name=name,
        include_public=True,
    )
    if dataset_id is None:
        raise not_found("Dataset")
    # Get entry
    raw_entry = dataset_entry_dao.filter(id=id, dataset_id=dataset_id)
    if not raw_entry:
        raise not_found(f"Dataset entry {id}")
    entry = raw_entry[0]
    return {
        "id": entry.id,
        "entry": json.loads(entry.entry),
        "created_at": entry.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.post(
    "/datasetv2",
    responses={
        201: {
            "description": "Dataset Created",
            "content": {
                "application/json": {
                    "example": {"info": "Dataset created successfully!"},
                },
            },
        },
        400: {
            "description": "Dataset already exists",
            "content": {"application/json": {"example": "Dataset already exists."}},
        },
    },
)
def create_dataset(
    request: Request,
    dataset: DatasetInfo,
    dataset_dao: DatasetDAO = Depends(),
):
    """
    Create a new dataset.
    """
    dataset_id = dataset_dao.get_id(
        user_id=request.state.user_id,
        name=dataset.name,
        include_public=True,
    )
    if dataset_id is not None:
        raise HTTPException(status_code=400, detail="Dataset already exists")
    dataset_dao.create(user_id=request.state.user_id, name=dataset.name)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"info": "Dataset created successfully!"},
    )


@router.post(
    "/datasetv2/{name:path}/entries",
    status_code=201,
    responses={
        201: {
            "description": "Entries Added",
            "content": {
                "application/json": {
                    "example": {
                        "added": ["id_1", "id_2", "id_3"],
                        "already_present": ["id_4", "id_5"],
                    },
                },
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {"application/json": {"example": "Dataset not found."}},
        },
    },
)
def add_dataset_entries(
    request_fastapi: Request,
    request: EntriesConfig,
    name: str = Path(
        ...,
        description="Dataset name (can include forward slashes)",
        example="my_dataset",
    ),
    dataset_dao: DatasetDAO = Depends(),
    dataset_entry_dao: DatasetEntryDAO = Depends(),
):
    """
    Add multiple entries to a dataset.
    """
    dataset_id = dataset_dao.get_id(
        user_id=request_fastapi.state.user_id,
        name=name,
        include_public=True,
    )
    if dataset_id is None:
        raise not_found("Dataset")

    existing_ids = []
    new_ids = []

    for entry in request.entries:
        # check if the entry already exists
        existing_id = dataset_entry_dao.filter(dataset_id=dataset_id, entry=entry)
        if existing_id:
            existing_ids.append(existing_id[0][0].id)
            continue
        # if not, add it to the dataset
        _id = dataset_entry_dao.create(dataset_id=dataset_id, entry=json.dumps(entry))
        new_ids.append(_id)
    return {
        "already_present": existing_ids,
        "added": new_ids,
    }


@router.patch(
    "/datasetv2/{name:path}",
    responses={
        200: {
            "description": "Dataset Renamed",
            "content": {"application/json": {"info": "Dataset renamed successfully!"}},
        },
        404: {
            "description": "Dataset Not Found",
            "content": {"application/json": {"example": "Dataset not found."}},
        },
    },
)
def rename_dataset(
    request: Request,
    new_name: DatasetNewName,
    name: str = Path(
        ...,
        description="Current dataset name (can include forward slashes)",
        example="my_dataset",
    ),
    dataset_dao: DatasetDAO = Depends(),
):
    """
    Rename an existing dataset.
    """
    dataset_id = dataset_dao.get_id(
        user_id=request.state.user_id,
        name=name,
        include_public=False,
    )
    if dataset_id is None:
        raise not_found("Dataset")
    dataset_dao.update(id=dataset_id, name=new_name.name)
    return "Dataset renamed successfully!"


@router.delete(
    "/datasetv2/{name:path}/entry/{id}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {"info": "Dataset entry deleted successfully!"},
            },
        },
        404_1: {
            "description": "Dataset Not Found",
            "content": {"application/json": {"example": "Dataset not found."}},
        },
        404_2: {
            "description": "Dataset Entry Not Found",
            "content": {
                "application/json": {"example": "Dataset entry <id> not found."},
            },
        },
    },
)
def delete_dataset_entry(
    request: Request,
    name: str = Path(
        ...,
        description="Dataset name (can include forward slashes)",
        example="my_dataset",
    ),
    id: str = Path(..., description="Entry ID", example="123"),
    dataset_dao: DatasetDAO = Depends(),
    dataset_entry_dao: DatasetEntryDAO = Depends(),
):
    """
    Delete a specific entry from a dataset.
    """
    dataset_id = dataset_dao.get_id(
        user_id=request.state.user_id,
        name=name,
        include_public=False,
    )
    if dataset_id is None:
        raise not_found("Dataset")
    dataset_entry = dataset_entry_dao.filter(id=id, dataset_id=dataset_id)
    if not dataset_entry:
        raise not_found(f"Dataset entry {id}")
    dataset_entry_dao.delete(id=id)
    return "Dataset Entry deleted successfully!"


@router.delete(
    "/datasetv2/{name:path}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {"info": "Dataset deleted successfully!"},
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {"application/json": {"example": "Dataset not found"}},
        },
    },
)
def delete_dataset(
    request: Request,
    name: str = Path(
        ...,
        description="Dataset name (can include forward slashes)",
        example="my_dataset",
    ),
    dataset_dao: DatasetDAO = Depends(),
):
    """
    Delete a dataset and all its corresponding entries.
    """
    dataset_id = dataset_dao.get_id(
        user_id=request.state.user_id,
        name=name,
        include_public=False,
    )
    if dataset_id is None:
        raise not_found("Dataset")
    dataset_dao.delete(id=dataset_id)
    return "Dataset deleted successfully!"
