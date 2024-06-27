import json
from typing import Annotated, Dict, List

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from google.cloud import storage
from google.cloud.exceptions import NotFound

from orchestra.web.api.utils.http_responses import (
    dataset_already_exists,
    dataset_does_not_exist,
    invalid_dataset_name,
)

router = APIRouter()

bucket_name = "uploaded_datasets"


# utils
# TODO: Remove duplication in batch_eval endpoints


def blob_exists(bucket_name: str, blob_name: str) -> bool:
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    try:
        blob.reload()
    except NotFound:
        return False
    return True


def dir_exists(bucket_name: str, dir_name: str) -> bool:
    bucket = storage.Client().bucket(bucket_name)
    blobs = list(bucket.list_blobs(prefix=dir_name))
    return len(blobs) > 0


def delete_dir(bucket_name: str, dir_name: str) -> None:
    bucket = storage.Client().bucket(bucket_name)

    # Ensure the directory_name ends with a slash
    if not dir_name.endswith("/"):
        dir_name += "/"

    # List all blobs with the directory_name prefix
    blobs = bucket.list_blobs(prefix=dir_name)

    # Delete each blob
    for blob in blobs:
        blob.delete()


def read_json_from_bucket(bucket_name, blob_name, raw=False):
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    json_data = blob.download_as_bytes()
    if raw:
        return json_data
    return json.loads(json_data.decode("utf-8"))


def upload_json_to_bucket(json_data: Dict[str, str], bucket_name: str, blob_name: str):
    blob = storage.Client().bucket(bucket_name).blob(blob_name)
    blob.upload_from_string(json_data, content_type="application/json")


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
    bucket = storage.Client().bucket(bucket_name)
    # List blobs with the specified prefix
    blobs = list(bucket.list_blobs(prefix=user_id))
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


# endpoints


@router.post("/dataset")
def upload_dataset(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    file: Annotated[UploadFile, Form()],
    name: Annotated[str, Form()],
) -> Dict[str, str]:
    """
    Uploads a dataset.
    """
    if "/" in name:
        raise invalid_dataset_name
    file_content = file.file.read()
    _upload_dataset(request_fastapi.state.user_id, name, file_content)
    return {"info": "Dataset uploaded succesfully!"}


# delete dataset
@router.delete("/dataset")
def delete_dataset(
    request_fastapi: Request,
    name: str,
) -> Dict[str, str]:
    """
    Deletes a dataset.
    """
    if "/" in name:
        raise invalid_dataset_name
    _delete_dataset(request_fastapi.state.user_id, name)
    return {"info": "Dataset deleted succesfully!"}


# list datasets
@router.get("/dataset/list")
def list_datasets(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
) -> List[str]:
    """
    Lists the user datasets.
    """
    datasets = _list_datasets(request_fastapi.state.user_id)
    return datasets


# download dataset
# TODO: This probably should get a URL that the user can cURL instead
@router.get("/dataset")
def download_dataset(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    name: str,
) -> List[Dict[str, str]]:
    """
    Download a dataset.
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
