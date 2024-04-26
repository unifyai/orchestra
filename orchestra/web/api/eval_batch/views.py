import json
import os
import subprocess
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
    # transform file to have the expected format
    lines = file.decode().split("\n")
    parsed_data = [json.loads(line) for line in lines if line.strip()]
    data_with_ids = [{"id_": i, **d} for i, d in enumerate(parsed_data)]

    # TODO: If not parsed properly, return error

    # TODO: If the name exists for the user, return error

    # Create CustomEvaluation and set status to pending
    dataset_evaluation_task_dao.create_dataset_evaluation_task(
        name, "pending", request_fastapi.state.user_id
    )
    dataset_evaluation_task_dao.session.commit()

    # store received file in a common directory
    eval_unique_id = f"{request_fastapi.state.user_id}_{name}"
    if not os.path.isdir(f"batch_eval/{eval_unique_id}"):
        os.mkdir(f"batch_eval/{eval_unique_id}")
    with open(f"batch_eval/{eval_unique_id}/prompts.jsonl", "w") as file:
        for item in data_with_ids:
            json_line = json.dumps(item)
            file.write(json_line + "\n")

    # send the root directory and the file name
    subprocess.Popen(
        [
            "python",
            "batch_eval/run.py",
            f"batch_eval/{eval_unique_id}/run",
            f"batch_eval/{eval_unique_id}/prompts.jsonl",
            request_fastapi.headers["authorization"].removeprefix("Bearer "),
            name,
        ]
    )
    return EvalBatchResponse(
        info="List of prompts updated succesfully. Your will receive an email soon!"
    )
