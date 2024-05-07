import json
import os
import time
import requests
from typing import Annotated, Any, Dict, List

from google.cloud import storage
from google.cloud.exceptions import NotFound

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.param_functions import Depends

from orchestra.db.dao.dataset_evaluation_task_dao import DatasetEvaluationTaskDAO
from orchestra.db.dao.dataset_evaluation_dao import DatasetEvaluationDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO

from orchestra.db.models.orchestra_models import DatasetEvaluationTask
from orchestra.web.api.eval_batch.schema import EvalBatchResponse, EvalBatchTaskResponse

from orchestra.web.api.utils.generate_points import generate_and_prune_points

from orchestra.db.models.orchestra_models import DatasetEvaluationTask

router = APIRouter()


@router.get("/eval/batch/tasks", response_model=List[EvalBatchTaskResponse])
def eval_batch(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    dataset_evaluation_task_dao: DatasetEvaluationTaskDAO = Depends(),
) -> List[DatasetEvaluationTask]:
    """
    Get eval batch tasks available to the user making the request.
    """
    return dataset_evaluation_task_dao.get_user_datasets(request_fastapi.state.user_id)


@router.post("/eval/batch")
def eval_batch(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    file: Annotated[UploadFile, Form()],
    name: Annotated[str, Form()],
    dataset_evaluation_task_dao: DatasetEvaluationTaskDAO = Depends(),
) -> EvalBatchResponse:
    """
    Compute batch evaluation based on the request.
    """

    if dataset_evaluation_task_dao.filter(name=name):
        raise HTTPException(
            status_code=400,
            detail="A dataset with this name already exists. Please, choose a different one.",
        )

    # Create CustomEvaluation and set status to pending
    dataset_evaluation_task_dao.create_dataset_evaluation_task(
        name, "pending", request_fastapi.state.user_id
    )
    dataset_evaluation_task_dao.session.commit()

    # Define the URL of your server's endpoint
    url = f'{os.getenv("EVAL_SERVER_URL")}/evaluate_prompts'
    # Define the authentication token
    headers = {"Authorization": os.getenv("EVAL_SERVER_PASSWORD")}
    # Define the parameters to send
    data = {
        "name": name,
        "api_key": request_fastapi.headers["authorization"].removeprefix("Bearer "),
        "eval_unique_id": f"{request_fastapi.state.user_id}_{name}",
    }
    # Define the file to upload
    file_content = file.file.read()
    files = {"file": file_content}
    # Make a POST request to the server
    response = requests.post(url, headers=headers, data=data, files=files, verify=False)

    # TODO: Deal with the response code

    return EvalBatchResponse(
        info="List of prompts uploaded succesfully. Your will receive an email soon!"
    )

@router.post("/training")
def training(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    train_file: Annotated[UploadFile, Form()],
    test_file: Annotated[UploadFile, Form()],
    name: Annotated[str, Form()],
) -> EvalBatchResponse:
    """
    Store the file uploaded by a user.
    """

    bucket_name = "training-jobs-temp-storage"
    train_blob_name = f"{request_fastapi.state.user_id}_{name}_train.json"
    test_blob_name = f"{request_fastapi.state.user_id}_{name}_test.json"

    exists = check_file_exists(bucket_name, train_blob_name)
    if exists:
        raise HTTPException(
            status_code=400,
            detail="A training dataset with this name already exists. Please, choose a different one.",
        )
    else:
        train_file_content = train_file.file.read()
        upload_json_to_bucket(train_file_content, bucket_name, train_blob_name)
        test_file_content = test_file.file.read()
        upload_json_to_bucket(test_file_content, bucket_name, test_blob_name)

    return EvalBatchResponse(
        info="Training data uploaded succesfully. Your will receive an email soon!"
    )

def check_file_exists(bucket_name, blob_name):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    try:
        blob.reload()
        return True
    except NotFound:
        return False

def read_json_from_bucket(bucket_name, blob_name):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    json_data = blob.download_as_string()
    return json.loads(json_data.decode('utf-8'))

def upload_json_to_bucket(json_data, bucket_name, destination_blob_name):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_string(json_data, content_type='application/json')

@router.get("/get_dataset_evaluation")
def get_dataset_evaluation(
    request_fastapi: Request,
    dataset_name: str,
    dataset_evaluation_task_dao: DatasetEvaluationTaskDAO = Depends(),
    dataset_evaluation_dao: DatasetEvaluationDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Retrieve specific dataset evaluation object from the database.
    """
    task = dataset_evaluation_task_dao.filter(name=dataset_name)
    if not task or (
        task[0].user_id is not None and task[0].user_id != request_fastapi.state.user_id
    ):
        raise HTTPException(
            status_code=404, detail="Dataset not found in this user account."
        )

    bucket_name = "plot-points-temp-storage"
    blob_name = f"{dataset_name}.json"

    generate_points = False
    exists = check_file_exists(bucket_name, blob_name)
    if exists:
        points = read_json_from_bucket(bucket_name, blob_name)
        # If stored points is empty, try to regenerate
        if points == {}:
            generate_points = True
    else:
        generate_points = True
    
    if generate_points:
        raw_data = dataset_evaluation_dao.filter(dataset_name=dataset_name)
        points = generate_and_prune_points(
            dataset_name, raw_data, endpoint_dao=endpoint_dao, benchmark_run_dao=benchmark_run_dao
        )
        json_str = json.dumps(points)
        upload_json_to_bucket(json_str, bucket_name, blob_name)

    return points
