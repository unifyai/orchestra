import os
import requests
from typing import Annotated, List

from fastapi import APIRouter, File, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.dataset_evaluation_task_dao import DatasetEvaluationTaskDAO
from orchestra.db.models.orchestra_models import DatasetEvaluationTask
from orchestra.web.api.eval_batch.schema import EvalBatchResponse, EvalBatchTaskResponse

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
    file: Annotated[bytes, File()],
    name: str,
    dataset_evaluation_task_dao: DatasetEvaluationTaskDAO = Depends(),
) -> EvalBatchResponse:
    """
    Compute batch evaluation based on the request.
    """
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
    files = {"file": file}
    # Make a POST request to the server
    response = requests.post(url, headers=headers, data=data, files=files, verify=False)

    # TODO: Deal with the response code

    return EvalBatchResponse(
        info="List of prompts uploaded succesfully. Your will receive an email soon!"
    )
