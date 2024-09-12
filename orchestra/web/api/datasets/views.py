import json
from typing import Annotated, Dict, List

import tiktoken
from fastapi import (
    APIRouter,
    Body,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)

from orchestra.db.dao.dataset_dao import DatasetDAO
from orchestra.web.api.utils.http_responses import invalid_dataset_name

router = APIRouter()


def check_file_content(file_content: str):
    valid = True
    info = (
        "The uploaded dataset has the wrong format."
        " It must be a jsonl file where each line has a `prompt` key"
        " and optionally a `ref_answer` one."
    )
    try:
        dicts = file_content.decode().split("\n")
        dicts = [json.loads(d) for d in dicts if d != ""]
        if not isinstance(dicts, List):
            raise ValueError
        for i, dct in enumerate(dicts):
            prompt_present = False
            for kw in dct.keys():
                if kw == "prompt":
                    prompt_present = True
                if kw not in ["prompt", "ref_answer"]:
                    info += f" Unknown keyword `{kw}` in line {i+1}."
                    continue
            if not prompt_present:
                info += f" Key `prompt` not found in line {i+1}."
                raise ValueError
    except:
        valid = False
    if not valid:
        raise HTTPException(status_code=400, detail=info)


def get_tokens_in_dataset(dataset_content: List[Dict[str, str]]):
    encoding = tiktoken.get_encoding("cl100k_base")
    num_tokens = 0
    dicts = dataset_content.decode().split("\n")
    dicts = [json.loads(d) for d in dicts if d != ""]
    for line in dicts:
        prompt = line["prompt"]
        num_tokens += len(encoding.encode(prompt))
    return num_tokens


# endpoints


@router.post(
    "/dataset",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Dataset uploaded sucessfully!"},
                },
            },
        },
        400: {
            "description": "Invalid dataset name",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid name for a dataset."
                        "Please, choose a different one.",
                    },
                },
            },
        },
        400: {
            "description": "Dataset already exists",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "A dataset with this name already exists."
                        "Please, choose a different one.",
                    },
                },
            },
        },
    },
)
def upload_dataset(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    file: Annotated[
        UploadFile,
        Form(
            description="The contents of the `.jsonl` file being uploaded.",
            json_schema_extra={"example": "dataset.jsonl"},
        ),
    ],
    name: Annotated[
        str,
        Form(
            description="The name to give to this dataset.",
            json_schema_extra={"example": "dataset1"},
        ),
    ],
    dataset_dao: DatasetDAO = Depends(),
) -> Dict[str, str]:
    """
    Uploads a custom dataset to your account.

    The uploaded file must be a JSONL file with **at least** a `prompt` key for
    each prompt each:

    ```
    {"prompt": "This is the first prompt"}
    {"prompt": "This is the second prompt"}
    {"prompt": "This is the third prompt"}
    ```

    Additionally, you can include any extra keys as desired, depending on the use case
    for the dataset, and how it will be used by the evaluators and/or router training.
    For example, you could include a reference answer to each prompt as follows:

    ```
    {"prompt": "This is the first prompt", "ref_answer": "First reference answer"}
    {"prompt": "This is the second prompt", "ref_answer": "Second reference answer"}
    {"prompt": "This is the third prompt", "ref_answer": "Third reference answer"}
    ```
    """
    if "../" in name or name[0] == "/":
        raise invalid_dataset_name
    file_content = file.file.read()
    check_file_content(file_content)

    user_datasets = dataset_dao.filter(user_id=request_fastapi.state.user_id, name=name)
    if user_datasets:
        raise HTTPException(400, detail=f"Dataset {name} already exists.")

    dataset_dao.create(user_id=request_fastapi.state.user_id, name=name)

    try:
        for entry in file_content.decode().split("\n"):
            if not entry.strip():
                continue
            prompt_data = json.loads(entry.strip())
            dataset_dao.add_prompt_to_dataset(
                user_id=request_fastapi.state.user_id,
                dataset_name=name,
                prompt_data=prompt_data,
            )
    except Exception as e:
        print(e)
        raise HTTPException(400, detail=f"Incorrect data format")

    return {"info": "Dataset uploaded successfully!"}


# download dataset
# TODO: This probably should get a URL that the user can cURL instead
@router.get(
    "/dataset",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        {"prompt": "This is the first prompt"},
                        {"prompt": "This is the second prompt"},
                        "...",
                    ],
                },
            },
        },
        400: {
            "description": "Invalid dataset name",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid name for a dataset. Please, choose a different one.",
                    },
                },
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "This dataset does not exist."},
                },
            },
        },
    },
)
def download_dataset(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    name: str = Query(description="Name of the dataset.", example="dataset1"),
    dataset_dao: DatasetDAO = Depends(),
):
    """
    Downloads a specific dataset from your account.
    """
    if "../" in name or name[0] == "/":
        raise invalid_dataset_name
    return dataset_dao.fetch_dataset(user_id=request_fastapi.state.user_id, name=name)


# delete dataset
@router.delete(
    "/dataset",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Dataset deleted successfully!"},
                },
            },
        },
        400: {
            "description": "Invalid dataset name",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid name for a dataset."
                        "Please, choose a different one.",
                    },
                },
            },
        },
    },
)
def delete_dataset(
    request_fastapi: Request,
    name: str = Query(description="Name of the dataset.", example="dataset1"),
    dataset_dao: DatasetDAO = Depends(),
) -> Dict[str, str]:
    """
    Deletes a previously updated dataset and any relevant artifacts from your account.
    """
    return dataset_dao.delete_dataset(user_id=request_fastapi.state.user_id, name=name)


@router.post(
    "/dataset/rename",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Dataset name updated sucessfully!"},
                },
            },
        },
        400: {
            "description": "Invalid dataset name",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid name for a dataset."
                        "Please, choose a different one.",
                    },
                },
            },
        },
        400: {
            "description": "Dataset already exists",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "A dataset with this name already exists."
                        "Please, choose a different one.",
                    },
                },
            },
        },
    },
)
def rename_dataset(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    name: str = Query(
        description="Name of the dataset to be updated.",
        example="dataset1",
    ),
    new_name: str = Query(
        description="New name for the dataset.",
        example="dataset2",
    ),
    dataset_dao: DatasetDAO = Depends(),
) -> Dict[str, str]:
    """
    Renames a previously uploaded dataset.

    """
    dataset_dao.rename(
        user_id=request_fastapi.state.user_id,
        name=name,
        new_name=new_name,
    )
    return {"info": "Dataset name updated successfully!"}


# list datasets
@router.get(
    "/dataset/list",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {"example": ["dataset_1", "dataset_2", "..."]},
            },
        },
    },
)
def list_datasets(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    dataset_dao: DatasetDAO = Depends(),
) -> List[str]:
    """
    Lists all the datasets stored in the user account by name.
    """
    dataset_info = dataset_dao.filter(user_id=request_fastapi.state.user_id)
    return [d.name for d in dataset_info]


@router.delete(
    "/dataset/delete_prompt",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Dataset prompt deleted successfully"},
                },
            },
        },
    },
)
def delete_prompt(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the dataset for prompt to be deleted from.",
        example="dataset1",
    ),
    prompt_id: str = Query(
        description="ID of the prompt to be removed.",
        example="123",
    ),
    dataset_dao: DatasetDAO = Depends(),
):
    return dataset_dao.remove_prompt_from_dataset(
        user_id=request_fastapi.state.user_id,
        dataset_name=name,
        prompt_id=prompt_id,
    )


@router.post(
    "/dataset/add_prompt",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Prompt added sucessfully!"},
                },
            },
        },
    },
)
def add_prompt(
    request_fastapi: Request,
    name: str = Body(
        description="Name of the dataset to add to",
        json_schema_extra={"example": "dataset_1"},
    ),
    prompt_data: dict = Body(
        description="JSON object containing the prompt data to upload.",
        json_schema_extra={
            "example": {
                "prompt": {
                    "messages": [
                        {"role": "user", "content": "What is the capital of Spain?"},
                    ],
                },
                "ref_answer": "Madrid",
            },
        },
    ),
    dataset_dao: DatasetDAO = Depends(),
):
    ret = dataset_dao.add_prompt_to_dataset(
        user_id=request_fastapi.state.user_id,
        dataset_name=name,
        prompt_data=prompt_data,
    )
    if isinstance(ret, dict):
        return ret
    return {"info": "Prompt added successfully"}
