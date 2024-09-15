import copy
import json
from typing import Annotated, Dict, List, Union

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
from orchestra.db.dao.dataset_prompt_dao import DatasetPromptDAO
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
    invalid_dataset_name,
)

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

    user_id = request_fastapi.state.user_id
    user_datasets = dataset_dao.filter(name=name)
    user_datasets = [d for d in user_datasets if d.user_id in [None, user_id]]
    if user_datasets:
        raise HTTPException(400, detail=f"Dataset {name} already exists.")

    dataset_dao.create(user_id=user_id, name=name)

    try:
        for entry in file_content.decode().split("\n"):
            if not entry.strip():
                continue
            prompt_data = json.loads(entry.strip())
            dataset_dao.add_prompt_to_dataset(
                user_id=user_id,
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
    entries = dataset_dao.fetch_dataset(
        user_id=request_fastapi.state.user_id,
        name=name,
    )
    if not entries:
        raise dataset_does_not_exist(name)
    formatted_entries = []
    for e in entries:
        formatted_entry = copy.copy(e)
        if formatted_entry["ref_answer"] is None:
            formatted_entry.pop("ref_answer")

        system_msg = formatted_entry.pop("system_msg")
        if system_msg:
            formatted_entry["messages"].insert(
                0,
                {"role": "system", "content": system_msg},
            )

        formatted_entry["prompt"] = formatted_entry.pop("prompt_kwargs")
        formatted_entry["prompt"]["messages"] = formatted_entry.pop("messages")

        formatted_entries.append(formatted_entry)
    return formatted_entries


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
            "description": "Can't Delete Public Dataset",
            "content": {
                "application/json": {
                    "example": {"detail": "You can't delete a public dataset."},
                },
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "The dataset <dataset> does not exist."},
                },
            },
        },
    },
)
def delete_dataset(
    request_fastapi: Request,
    name: str = Query(description="Name of the dataset.", example="dataset1"),
    dataset_dao: DatasetDAO = Depends(),
    dataset_prompt_dao: DatasetPromptDAO = Depends(),
) -> Dict[str, str]:
    """
    Deletes a previously updated dataset and any relevant artifacts from your account.
    """
    user_id = request_fastapi.state.user_id
    dataset_id = dataset_dao.filter(user_id=user_id, name=name)
    if not dataset_id:
        all_datasets = dataset_dao.filter(name=name)
        if all_datasets and [d for d in all_datasets if d.user_id is None]:
            raise HTTPException(
                status_code=400,
                detail="You can't delete a public dataset.",
            )
        raise dataset_does_not_exist(name)
    dataset_prompt_dao.delete(dataset_id=dataset_id[0].id)
    return dataset_dao.delete_dataset(user_id=user_id, name=name)


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
    user_id = request_fastapi.state.user_id
    dataset_info = dataset_dao.filter()
    return [d.name for d in dataset_info if d.user_id in [None, user_id]]


@router.delete(
    "/dataset/data",
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
def delete_data(
    request_fastapi: Request,
    name: str = Query(
        description="Name of the dataset for prompt to be deleted from.",
        example="dataset1",
    ),
    data_ids: Union[str, List[str]] = Query(
        description="Unique ids for the data to be removed.",
        example=["001", "002", "003"],
    ),
    dataset_dao: DatasetDAO = Depends(),
):
    rets = list()
    for datum_id in data_ids:
        rets.append(
            dataset_dao.remove_prompt_from_dataset(
                user_id=request_fastapi.state.user_id,
                dataset_name=name,
                prompt_id=datum_id,
            )
        )
    error_rets = [ret["error"] for ret in rets if "error" in ret]
    if error_rets:
        return {"error": "\n".join(error_rets)}
    return rets[0]


@router.post(
    "/dataset/data",
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
def add_data(
    request_fastapi: Request,
    name: str = Body(
        description="Name of the dataset to add to",
        json_schema_extra={"example": "dataset_1"},
    ),
    data: Union[Dict, List[Dict]] = Body(
        description="JSON object containing the Datum dict to upload, "
                    "or a list of Datum dicts to upload",
        json_schema_extra={
            "example": [
                {
                    "prompt": {
                        "messages": [
                            {"role": "user",
                             "content": "What is the capital of Spain?"},
                        ],
                    },
                    "ref_answer": "Madrid",
                },
                {
                    "prompt": {
                        "messages": [
                            {"role": "user",
                             "content": "What is the capital of England?"},
                        ],
                    },
                    "ref_answer": "London",
                }
            ],
        },
    ),
    dataset_dao: DatasetDAO = Depends(),
):
    rets = list()
    if isinstance(data, dict):
        rets.append(
            dataset_dao.add_prompt_to_dataset(
                user_id=request_fastapi.state.user_id,
                dataset_name=name,
                prompt_data=data,
            )
        )
    elif isinstance(data, list):
        for datum in data:
            rets.append(
                dataset_dao.add_prompt_to_dataset(
                    user_id=request_fastapi.state.user_id,
                    dataset_name=name,
                    prompt_data=datum,
                )
            )
    error_rets = [ret["error"] for ret in rets if "error" in ret]
    if error_rets:
        return {"error": "\n".join(error_rets)}
    return {"info": "Data added successfully"}
