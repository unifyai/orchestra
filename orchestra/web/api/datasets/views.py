import hashlib
import json
import os
import time
from typing import Annotated, Dict, List

import tiktoken
from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile

from orchestra.web.api.utils import gcp, on_prem
from orchestra.web.api.utils.http_responses import (
    dataset_already_exists,
    dataset_does_not_exist,
    invalid_dataset_name,
)

router = APIRouter()

bucket_name = "uploaded_datasets"


# utils
# TODO: Remove duplication in batch_eval endpoints


def _upload_dataset(user_id: str, internal_id: str, file_content: bytes):
    # TODO: 0 will need to be accounted when introducing dynamic datasets
    blob_name = f"{user_id}/{internal_id}/0/dataset.jsonl"
    check_file_content(file_content)
    exists = (
        on_prem.file_exists(bucket_name, blob_name)
        if os.environ.get("ON_PREM")
        else gcp.blob_exists(bucket_name, blob_name)
    )
    if exists:
        raise dataset_already_exists
    elif os.environ.get("ON_PREM"):
        on_prem.write_json_to_folder(file_content, bucket_name, blob_name)
    else:
        gcp.upload_json_to_bucket(file_content, bucket_name, blob_name)


def _delete_dataset(user_id: str, internal_id: str):
    # TODO: This needs to ensure that no evaluations exist before
    # deleting the whole directory
    # TODO: 0 will need to be accounted when introducing dynamic datasets
    if internal_id == "":
        raise dataset_does_not_exist(internal_id)
    dir_name = f"{user_id}/{internal_id}/"
    exists = (
        on_prem.dir_exists(bucket_name, dir_name)
        if os.environ.get("ON_PREM")
        else gcp.dir_exists(bucket_name, dir_name)
    )
    if not exists:
        raise dataset_does_not_exist(internal_id)
    elif os.environ.get("ON_PREM"):
        on_prem.delete_dir(bucket_name, dir_name)
    else:
        gcp.delete_dir(bucket_name, dir_name)


def _list_datasets(user_id: str):
    blobs = (
        on_prem.list_dir(bucket_name, user_id)
        if os.environ.get("ON_PREM")
        else gcp.list_dir(bucket_name, user_id)
    )
    dirs = set(
        [(b if os.environ.get("ON_PREM") else b.id).split("/")[2] for b in blobs],
    )
    # Clean legacy datasets
    dirs = {d for d in dirs if not d.endswith(".jsonl")}
    dirs.discard("evaluation_configs")
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


def _store_num_tokens(user_id: str, internal_id: str, num_tokens: int):
    blob_name = f"{user_id}/{internal_id}/0/num_tokens.json"
    exists = (
        on_prem.file_exists(bucket_name, blob_name)
        if os.environ.get("ON_PREM")
        else gcp.blob_exists(bucket_name, blob_name)
    )
    string = json.dumps({"num_tokens": num_tokens}).encode()
    if exists:
        raise dataset_already_exists
    elif os.environ.get("ON_PREM"):
        on_prem.write_json_to_folder(string, bucket_name, blob_name)
    else:
        gcp.upload_json_to_bucket(string, bucket_name, blob_name)


def _store_metadata(
    user_id: str,
    internal_id: str,
    name: str,
    alredy_exists: bool = False,
):
    blob_name = f"{user_id}/{internal_id}/metadata.json"
    exists = (
        on_prem.file_exists(bucket_name, blob_name)
        if os.environ.get("ON_PREM")
        else gcp.blob_exists(bucket_name, blob_name)
    )
    string = json.dumps({"display_name": name}).encode()
    if exists and not alredy_exists:
        raise dataset_already_exists
    elif os.environ.get("ON_PREM"):
        on_prem.write_json_to_folder(string, bucket_name, blob_name)
    else:
        gcp.upload_json_to_bucket(string, bucket_name, blob_name)


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
    name_to_hash = f"{name}_{time.time}".encode("utf-8")
    internal_id = hashlib.shake_128(name_to_hash).hexdigest(8)
    _upload_dataset(request_fastapi.state.user_id, internal_id, file_content)
    num_tokens = get_tokens_in_dataset(file_content)
    _store_num_tokens(request_fastapi.state.user_id, internal_id, num_tokens)
    _store_metadata(request_fastapi.state.user_id, internal_id, name)
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
):
    """
    Downloads a specific dataset from your account.
    """
    if "../" in name or name[0] == "/":
        raise invalid_dataset_name
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(request_fastapi.state.user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(request_fastapi.state.user_id)
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(name, name)
    blob_name = f"{request_fastapi.state.user_id}/{internal_id}/0/dataset.jsonl"
    exists = (
        on_prem.file_exists(bucket_name, blob_name)
        if os.environ.get("ON_PREM")
        else gcp.blob_exists(bucket_name, blob_name)
    )
    if not exists:
        raise dataset_does_not_exist(name)
    else:
        string = (
            on_prem.read_json_from_folder(bucket_name, blob_name, raw=True)
            if os.environ.get("ON_PREM")
            else gcp.read_json_from_bucket(bucket_name, blob_name, raw=True)
        )
        string = "[".encode() + string + "]".encode()
        string = string.replace("}\n{".encode(), "},{".encode())
        return json.loads(string)


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
) -> Dict[str, str]:
    """
    Deletes a previously updated dataset and any relevant artifacts from your account.
    """
    if "../" in name or name[0] == "/":
        raise invalid_dataset_name
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(request_fastapi.state.user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(request_fastapi.state.user_id)
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(name, name)
    _delete_dataset(request_fastapi.state.user_id, internal_id)
    return {"info": "Dataset deleted successfully!"}


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
) -> Dict[str, str]:
    """
    Renames a previously uploaded dataset.

    """
    if "../" in name or name[0] == "/":
        raise invalid_dataset_name
    if "../" in new_name or new_name[0] == "/":
        raise invalid_dataset_name

    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(request_fastapi.state.user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(request_fastapi.state.user_id)

    user_id = request_fastapi.state.user_id
    name_to_id = {name: id_ for id_, name in id_to_name.items()}
    internal_id = name_to_id.get(name, None)
    if not internal_id:
        raise dataset_does_not_exist(name)
    if new_name in name_to_id:
        raise invalid_dataset_name

    _store_metadata(user_id, internal_id, new_name, alredy_exists=True)

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
) -> List[str]:
    """
    Lists all the datasets stored in the user account by name.
    """
    datasets = _list_datasets(request_fastapi.state.user_id)
    if os.environ.get("ON_PREM"):
        id_to_name = on_prem.internal_id_to_displayname(request_fastapi.state.user_id)
    else:
        id_to_name = gcp.internal_id_to_displayname(request_fastapi.state.user_id)
    dataset_names = []
    for d in datasets:
        dataset_names.append(id_to_name.get(d, d))
    return dataset_names
