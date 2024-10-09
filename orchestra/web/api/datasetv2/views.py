"""
Endpoints related to dataset management and operations.
"""

from typing import Any, List

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from starlette import status

from orchestra.db.dao.dataset_dao import DatasetDAO
from orchestra.db.dao.dataset_entry_dao import DatasetEntryDAO
from orchestra.web.api.datasetv2.schema import DatasetInfo, DatasetNewName

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
    return dataset_dao.list_datasets(user_id=request.state.user_id)


@router.get(
    "/datasetv2/{name}",
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
        404: {"description": "Dataset Not Found"},
    },
)
def get_dataset_entries(
    request: Request,
    name: str = Path(..., description="Dataset name (can include forward slashes)"),
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
        inclde_public=True,
    )
    if dataset_id is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    # Get entries of the dataset
    entries = dataset_entry_dao.filter(
        dataset_id=dataset_id,
        limit=limit,
        offset=offset,
    )
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
    "/datasetv2/{name}/entry/{id}",
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
        404: {"description": "Dataset Not Found"},
        404: {"description": "Dataset entry Not Found"},
    },
)
def get_dataset_entry(
    request: Request,
    name: str = Path(..., description="Dataset name (can include forward slashes)"),
    id: str = Path(..., description="Entry ID"),
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
        raise HTTPException(status_code=404, detail="Dataset not found")
    # Get entry
    entry = dataset_entry_dao.filter(id=id, dataset_id=dataset_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Dataset entry not found")
    return entry[0]


@router.post(
    "/datasetv2",
    responses={
        201: {
            "description": "Dataset Created",
            "content": {
                "application/json": {
                    "example": {"info": "Dataset created sccessfully!"},
                },
            },
        },
        400: {"description": "Dataset Already Exists"},
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
    try:
        dataset_dao.create(user_id=request.state.user_id, dataset_name=dataset.name)
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"info": "Dataset created successfully!"},
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Dataset already exists")


# TODO
@router.post(
    "/datasetv2/{name}/entries",
    response_model=List[str],
    status_code=201,
    responses={
        201: {
            "description": "Entries Added",
            "content": {"application/json": {"example": ["1", "2", "3"]}},
        },
        404: {"description": "Dataset Not Found"},
    },
)
def add_dataset_entries(
    request: Request,
    name: str = Path(..., description="Dataset name (can include forward slashes)"),
    entries: List[Any] = Body(..., description="List of entries to add"),
    dataset_dao: DatasetDAO = Depends(),
):
    """
    Add multiple entries to a dataset.
    """
    try:
        return dataset_dao.add_entries(
            user_id=request.state.user_id,
            dataset_name=name,
            entries=entries,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Dataset not found")


@router.patch(
    "/datasetv2/{name}",
    responses={
        200: {
            "description": "Dataset Renamed",
            "content": {"application/json": {"info": "Dataset renamed successfully!"}},
        },
        404: {"description": "Dataset Not Found"},
    },
)
def rename_dataset(
    request: Request,
    new_name: DatasetNewName,
    name: str = Path(
        ...,
        description="Current dataset name (can include forward slashes)",
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
        raise HTTPException(status_code=404, detail="Dataset not found")
    dataset_dao.update(id=dataset_id, name=new_name.name)
    return "Dataset renamed successfully!"


@router.delete(
    "/datasetv2/{name}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {"info": "Dataset deleted successfully!"},
            },
        },
        404: {"description": "Dataset Not Found"},
    },
)
def delete_dataset(
    request: Request,
    name: str = Path(..., description="Dataset name (can include forward slashes)"),
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
        raise HTTPException(status_code=404, detail="Dataset not found")
    dataset_dao.delete(id=dataset_id)
    return "Dataset deleted successfully!"


@router.delete(
    "/datasetv2/{name}/entry/{id}",
    status_code=204,
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {"info": "Dataset entry deleted successfully!"},
            },
        },
        404: {"description": "Dataset or Entry Not Found"},
    },
)
def delete_dataset_entry(
    request: Request,
    name: str = Path(..., description="Dataset name (can include forward slashes)"),
    id: str = Path(..., description="Entry ID"),
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
        raise HTTPException(status_code=404, detail="Dataset not found")
    dataset_entry = dataset_entry_dao.filter(id=id, dataset_id=dataset_id)
    if not dataset_entry:
        raise HTTPException(status_code=404, detail="Dataset entry not found")
    dataset_entry_dao.delete(id=id)
    return "Dataset Entry deleted successfully!"
