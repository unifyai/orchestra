import json
from typing import Annotated, Dict, List

import tiktoken
from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile

from orchestra.web.api.utils.gcp import (
    blob_exists,
    delete_dir,
    dir_exists,
    list_dir,
    read_json_from_bucket,
    upload_json_to_bucket,
)
from orchestra.web.api.utils.http_responses import (
    dataset_already_exists,
    dataset_does_not_exist,
    invalid_dataset_name,
)

router = APIRouter()

bucket_name = "uploaded_datasets"


# utils
# TODO: Remove duplication in batch_eval endpoints


def _upload_dataset(user_id: str, name: str, file_content: bytes):
    # TODO: 0 will need to be accounted when introducing dynamic datasets
    blob_name = f"{user_id}/{name}/0/dataset.jsonl"
    check_file_content(file_content)
    if blob_exists(bucket_name, blob_name):
        raise dataset_already_exists
    else:
        upload_json_to_bucket(file_content, bucket_name, blob_name)


def _delete_dataset(user_id: str, name: str):
    # TODO: This needs to ensure that no evaluations exist before
    # deleting the whole directory
    # TODO: 0 will need to be accounted when introducing dynamic datasets
    if name == "":
        raise dataset_does_not_exist
    dir_name = f"{user_id}/{name}/"
    if not dir_exists(bucket_name, dir_name):
        raise dataset_does_not_exist
    else:
        delete_dir(bucket_name, dir_name)


def _list_datasets(user_id: str):
    blobs = list_dir(bucket_name, user_id)
    dirs = set([b.id.split("/")[2] for b in blobs])
    # Clean legacy datasets
    dirs = {d for d in dirs if not d.endswith(".jsonl")}
    return list(dirs)


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
        for i, dict in enumerate(dicts):
            prompt_present = False
            for kw in dict.keys():
                if kw == "prompt":
                    prompt_present = True
                if kw not in ["prompt", "ref_answer"]:
                    info += f" Unknown keyword `{kw}` in line {i+1}."
                    raise ValueError
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


def _store_num_tokens(user_id: str, name: str, num_tokens: int):
    blob_name = f"{user_id}/{name}/0/num_tokens.json"
    if blob_exists(bucket_name, blob_name):
        raise dataset_already_exists
    else:
        string = json.dumps({"num_tokens": num_tokens})
        upload_json_to_bucket(string, bucket_name, blob_name)


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
                        "detail": "Invalid name for a dataset. Please, choose a different one.",
                    },
                },
            },
        },
        400: {
            "description": "Dataset already exists",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "A dataset with this name already exists. Please, choose a different one.",
                    },
                },
            },
        },
    },
)
def upload_dataset(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    file: Annotated[UploadFile, Form()],
    name: Annotated[str, Form()],
) -> Dict[str, str]:
    """
    Uploads a custom dataset to the platform.

    The uploaded file must be a JSONL file with **at least** a `prompt` key:

    ```
    {"prompt": "This is the first prompt"}
    {"prompt": "This is the second prompt"}
    {"prompt": "This is the third prompt"}
    ```

    Additionally, you can include a `ref_answer` key, which will be accounted
    during the evaluations.

    ```
    {"prompt": "This is the first prompt", "ref_answer": "First reference answer"}
    {"prompt": "This is the second prompt", "ref_answer": "Second reference answer"}
    {"prompt": "This is the third prompt", "ref_answer": "Third reference answer"}
    ```

    """
    if "/" in name:
        raise invalid_dataset_name
    file_content = file.file.read()
    _upload_dataset(request_fastapi.state.user_id, name, file_content)
    num_tokens = get_tokens_in_dataset(file_content)
    _store_num_tokens(request_fastapi.state.user_id, name, num_tokens)
    return {"info": "Dataset uploaded succesfully!"}


# delete dataset
@router.delete(
    "/dataset",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Dataset deleted succesfully!"},
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
    },
)
def delete_dataset(
    request_fastapi: Request,
    name: str = Query(..., description="Name of the dataset."),
) -> Dict[str, str]:
    """
    Deletes a previously updated dataset and any relevant artifacts from the platform.
    """
    if "/" in name:
        raise invalid_dataset_name
    _delete_dataset(request_fastapi.state.user_id, name)
    return {"info": "Dataset deleted succesfully!"}


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
) -> List[str]:
    """
    Lists all the custom datasets uploaded by the user to the platform.
    """
    datasets = _list_datasets(request_fastapi.state.user_id)
    return datasets


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
    name: str = Query(..., description="Name of the dataset."),
) -> List[Dict[str, str]]:
    """
    Downloads a specific dataset from the platform.
    """
    if "/" in name:
        raise invalid_dataset_name
    blob_name = f"{request_fastapi.state.user_id}/{name}/0/dataset.jsonl"
    if not blob_exists(bucket_name, blob_name):
        raise dataset_does_not_exist
    else:
        string = read_json_from_bucket(bucket_name, blob_name, raw=True)
        string = "[".encode() + string + "]".encode()
        string = string.replace("}\n{".encode(), "},{".encode())
        return json.loads(string)
