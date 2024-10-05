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
from pydantic import ValidationError
from unify import Prompt

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
        " It must be a jsonl file where each line has a `prompt` key."
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
                if kw == "id":
                    raise HTTPException(
                        status_code=400,
                        detail=f"You can't have an extra field with the name `id`.",
                    )
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
) -> Dict[str, List[int]]:
    """
    Uploads a custom dataset to your account.

    The uploaded file must be a JSONL file where each line contains **at least** a `prompt` key as follows:

    ```
    {"prompt": "This is the first prompt"}
    {"prompt": "This is the second prompt"}
    {"prompt": "This is the third prompt"}
    ```

    Additionally, you can include arbitrary extra keys, which may be used downstream (e.g. in evaluations).
    For example, you could include a reference answer to each prompt as follows:

    ```
    {"prompt": "This is the first prompt", "ref_answer": "First reference answer"}
    {"prompt": "This is the second prompt", "ref_answer": "Second reference answer"}
    {"prompt": "This is the third prompt", "ref_answer": "Third reference answer"}
    ```

    Returns:
        The list of unique prompt ids in the dataset.
    """

    if "../" in name or name[0] == "/":
        raise invalid_dataset_name
    file_content = file.file.read()
    check_file_content(file_content)

    user_id = request_fastapi.state.user_id
    user_datasets = dataset_dao.get_dataset_id(user_id, name)
    if user_datasets:
        raise HTTPException(400, detail=f"Dataset {name} already exists.")

    dataset_dao.create(user_id=user_id, name=name)

    data_to_upload = []
    try:
        for entry in file_content.decode().split("\n"):
            if not entry.strip():
                continue
            datum = json.loads(entry.strip())
            data_to_upload.append(datum)
    except:
        raise HTTPException(400, detail=f"Incorrect data format")

    prompt_ids = _add_data(
        dataset_dao=dataset_dao,
        user_id=user_id,
        dataset_name=name,
        data=data_to_upload,
        ignore_duplicates=False,
    )
    return prompt_ids


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
    limit: int = Query(
        100,
        description="The number of entries to return.",
        example="100",
    ),
    offset: int = Query(
        0,
        description="The number of entries to skip before starting to return results.",
        example="0",
    ),
    dataset_dao: DatasetDAO = Depends(),
):
    """
    Downloads a specific dataset from your account.
    """
    if "../" in name or name[0] == "/":
        raise invalid_dataset_name

    entries = dataset_dao.fetch_dataset(
        user_id=request_fastapi.state.user_id, name=name, limit=limit, offset=offset
    )
    if not entries:
        raise dataset_does_not_exist(name)
    formatted_entries = []
    for e in entries:
        formatted_entry = copy.copy(e)

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
    if name == "all_data":
        raise HTTPException(
            status_code=400,
            detail="You can't delete the all_data dataset, "
                   "which contains all data in your account.",
        )
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
    user_id = request_fastapi.state.user_id

    existing_dataset = dataset_dao.filter(user_id=user_id, name=name)
    if not existing_dataset:
        raise HTTPException(
            status_code=400,
            detail=f"You don't have a dataset named {name}",
        )

    if name == new_name:
        return {"info": "Dataset name updated succesfully!"}

    clash_name = dataset_dao.filter(user_id=user_id, name=new_name)
    if clash_name:
        raise HTTPException(
            status_code=400,
            detail=f"You already have a dataset named {new_name}.",
        )

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
    dataset_info = dataset_dao.filter(user_id=[None, user_id])
    return sorted([d.name for d in dataset_info])


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
    if (isinstance(data_ids, list) and not data_ids) or data_ids == "":
        return {"info": "data_ids argument was empty. Nothing to delete."}
    rets = list()
    for datum_id in data_ids:
        rets.append(
            dataset_dao.remove_prompt_from_dataset(
                user_id=request_fastapi.state.user_id,
                dataset_name=name,
                prompt_id=datum_id,
            ),
        )
    error_rets = [ret["error"] for ret in rets if "error" in ret]
    if error_rets:
        return {"error": "\n".join(error_rets)}
    return rets[0]


def _add_data(
    dataset_dao: DatasetDAO,
    user_id: str,
    dataset_name: str,
    data: Union[dict, list[dict]],
    ignore_duplicates: bool,
) -> Dict[str, List[int]]:
    if dataset_name != "all_data":
        usr_datasets = \
            [d.name for d in dataset_dao.filter() if d.user_id in [None, user_id]]
        if "all_data" not in usr_datasets:
            dataset_dao.create(user_id=user_id, name="all_data")
        # ToDo: remove this code above once this default dataset is hardcoded
        _add_data(
            dataset_dao,
            user_id,
            "all_data",
            data,
            True
        )

    if isinstance(data, dict):
        data = [data]

    prompt_ids_added = []
    prompt_ids_already_present = []
    failures = {}
    for ix, datum in enumerate(data):
        # validate the pydantic
        try:
            Prompt.parse_obj(datum["prompt"])
        except ValidationError as e:
            failures[ix] = e
            continue
        except:
            failures[ix] = "An unknown error occured"
        prompt_id_or_error = dataset_dao.add_prompt_to_dataset(
            user_id=user_id,
            dataset_name=dataset_name,
            prompt_data=datum,
        )
        if isinstance(prompt_id_or_error, int):
            prompt_ids_added.append(prompt_id_or_error)
            continue
        if isinstance(prompt_id_or_error, dict) and "error" in prompt_id_or_error:
            if ignore_duplicates and prompt_id_or_error["error"] == (
                "This prompt is already in the dataset"
            ):
                prompt_ids_already_present.append(prompt_id_or_error["prompt_id"])
            else:
                failures[ix] = prompt_id_or_error["error"]
        else:
            failures[ix] = None

    if not failures:
        return {
                "already_present": prompt_ids_already_present,
                "added": prompt_ids_added
        }

    error_msg_formatted = "Errors:\n"
    for ix, msg in failures.items():
        if msg is not None:
            error_msg_formatted += f"Error with prompt {ix+1}: {msg}\n"

    if prompt_ids_added:
        msg = (
            f"There was an error while adding some of the prompts.\n"
            f"There were {len(prompt_ids_added)} prompts added successfuly, and {len(failures)} errors.\n"
        )
    else:
        if len(failures) == 1:
            msg = f"There was an error adding the prompt.\n"
        else:
            msg = (
                f"There was an error adding all of the prompts.\n"
                f"There were {len(failures)} errors.\n"
            )

    msg += error_msg_formatted

    raise HTTPException(status_code=400, detail=msg)


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
                            {
                                "role": "user",
                                "content": "What is the capital of Spain?",
                            },
                        ],
                    },
                    "ref_answer": "Madrid",
                },
                {
                    "prompt": {
                        "messages": [
                            {
                                "role": "user",
                                "content": "What is the capital of England?",
                            },
                        ],
                    },
                    "ref_answer": "London",
                },
            ],
        },
    ),
    ignore_duplicates: bool = Body(
        description="Whether to ignore attempted duplicate entries. If False, an "
        "exception is raised when attempting to add duplicate data.",
        json_schema_extra={"example": False},
        default=True,
    ),
    dataset_dao: DatasetDAO = Depends(),
) -> Dict[str, List[int]]:

    user_id = request_fastapi.state.user_id

    prompt_ids = _add_data(
        dataset_dao=dataset_dao,
        user_id=user_id,
        dataset_name=name,
        data=data,
        ignore_duplicates=ignore_duplicates,
    )
    return prompt_ids
