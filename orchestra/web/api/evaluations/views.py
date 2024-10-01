"""
Includes endpoints related to dataset evaluations.
"""

import json
import os
import re
from typing import Dict, Optional

import requests
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from providers.completion import PROVIDER_CLASSES

from orchestra.db.dao.dataset_dao import DatasetDAO
from orchestra.db.dao.default_prompt_dao import DefaultPromptDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.evaluation_dao import EvaluationDAO
from orchestra.db.dao.evaluator_dao import EvaluatorDAO
from orchestra.db.dao.judgement_dao import JudgementDAO
from orchestra.db.dao.latest_benchmark_dao import LatestBenchmarkDAO
from orchestra.db.dao.router_dao import RouterDAO
from orchestra.db.dao.stored_prompt_dao import StoredPromptDAO
from orchestra.db.dao.stored_prompt_response_dao import StoredPromptResponseDAO
from orchestra.db.dao.stored_prompt_variation_dao import StoredPromptVariationDAO
from orchestra.web.api.utils import gcp, on_prem
from orchestra.web.api.utils.http_responses import (
    dataset_does_not_exist,
    evaluator_not_found,
    invalid_training_endpoints,
)

router = APIRouter()
admin_router = APIRouter()

# utils


def is_csv_of_integers(s):
    pattern = r"^(\d+,)*\d+$"
    return bool(re.match(pattern, s))


# TODO: Move to utils (duplicated in routing)
def dataset_exists(dataset_dao, user_id, name):
    _ids = dataset_dao.get_dataset_id(user_id, name)
    if _ids:
        return True
    return False


def get_dataset_id(dataset_dao, user_id, name):
    _ids = dataset_dao.get_dataset_id(user_id, name)
    if _ids:
        return _ids[0]
    return None


# TODO: Move to utils (duplicated in routing)
def is_standard_endpoint(model: str, provider: str):
    if provider in PROVIDER_CLASSES:
        lm = PROVIDER_CLASSES[provider](model)
        if model in lm.supported_models:
            return True
    return False


# TODO: Move to utils (duplicated in routing)
def find_invalid_endpoints(endpoints):
    invalid_endpoints = []
    for e in endpoints:
        model, provider = e.split("@")
        if "router" in model:
            invalid_endpoints.append(e)
            continue
        if provider == "custom":
            # TODO: Support this properly, probably all providers (including custom one)
            # can have a method to check if the model exists as an endpoint
            # We also need an endpoint to list all public + custom endpoints
            invalid_endpoints.append(e)  # temp
            continue
        if not is_standard_endpoint(model, provider):
            invalid_endpoints.append(e)  # temp
            continue
    return invalid_endpoints


def send_to_dataset_evaluation_server(action, **data):
    project_name = os.environ.get("PUBSUB_PROJECT_NAME", "saas-368716")
    topic = f"projects/{project_name}/topics/" + os.environ.get(
        "PUBSUB_MESSAGING_TOPIC",
        "dataset_evaluation",
    )
    on_prem = os.environ.get("ON_PREM")
    url = "https://api.unify.ai" if not on_prem else os.environ.get("ORCHESTRA_URL")
    if os.getenv("STAGING"):
        topic = f"projects/{project_name}/topics/staging_dataset_evaluation"
        url = "https://orchestra-staging-lz5fmz6i7q-ew.a.run.app"

    msg = {
        "action": action,
        **data,
        "orchestra_url": url,
        "admin_key": os.environ.get("ORCHESTRA_ADMIN_KEY"),
    }
    if on_prem and not os.environ.get("PUBSUB_MESSAGING_TOPIC"):
        on_prem.send_pubsub_msg(topic, msg)
    else:
        gcp.send_pubsub_msg(topic, msg)
    print(f"Published: {str({'action': action, **data, 'orchestra_url': url})}")


def check_dataset_prompt_arg(dataset, prompts):
    if (dataset is None) == (prompts is None):
        raise HTTPException(
            status_code=400,
            detail="You must specify exactly one of `dataset`, `prompts`.",
        )


def get_prompt_ids(dataset, prompts, user_id, dataset_dao, stored_prompt_dao):
    check_dataset_prompt_arg(dataset, prompts)
    if dataset:
        dataset_id = get_dataset_id(dataset_dao, user_id, dataset)
        if dataset_id is None:
            raise dataset_does_not_exist(dataset)
        prompt_ids = dataset_dao.fetch_prompts_ids_in_dataset(user_id, dataset)
        prompt_ids = [p["id"] for p in prompt_ids]
        return prompt_ids

    # otherwise it's prompts

    if not is_csv_of_integers(prompts):
        raise HTTPException(
            status_code=400,
            detail="Error parsing `prompts`: "
            "Please ensure `prompts` is a comma-separated string of integers, "
            "with no whitespace.",
        )

    prompt_ids = [int(c) for c in prompts.split(",")]
    missing_ids = stored_prompt_dao.check_ids_valid(user_id, prompt_ids)
    if missing_ids:
        raise HTTPException(
            status_code=400,
            detail=f"The following prompt_ids are invalid: {', '.join(str(_id) for _id in missing_ids)}",
        )
    return prompt_ids


###########################
# endpoints
###########################


@router.post(
    "/evaluation",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Dataset evaluation started! "
                        "You will receive an email soon!",
                    },
                },
            },
        },
        400: {
            "description": "Invalid Endpoints",
            "content": {
                "application/json": {
                    "example": {
                        "detail": (
                            "Invalid input. Couldn't find"
                            " endpoints [model_1@endpoint_1, model_2@endpoint_2]."
                        ),
                    },
                },
            },
        },
        404: {
            "description": "Dataset Not Found",
            "content": {
                "application/json": {
                    "example": {"detail": "This dataset does not exist!"},
                },
            },
        },
    },
)
def trigger_evaluation(
    request_fastapi: Request,
    evaluator: str = Query(
        default="default_evaluator",
        description="Name of the evaluator to use. If not specified, 'default_evaluator' will be used.",
        example="eval1",
    ),
    default_prompt: str = Query(
        default=None,
        description="Name of the default prompt to use.",
        example="default_prompt1",
    ),
    dataset: Optional[str] = Query(
        default=None,
        description="Name of the uploaded dataset to evaluate. Must pass exactly one of `dataset`, `prompts`.",
        example="dataset1",
    ),
    prompts: Optional[str] = Query(
        default=None,
        description="Specify the prompts to evaluate. Pass a string of comma separated integers. Must pass exactly one of `dataset`, `prompts`.",
        example="34,89,127",
    ),
    endpoint: str = Query(
        description=(
            "Name of the endpoint to evaluate."
            " Endpoints must be specified using the `model@provider` format."
        ),
        example="gpt-4o-mini@openai",
    ),
    client_side_scores: UploadFile = File(
        default=None,
        description="An optional file upload for client-side scores. The file must be in JSONL format and the prompts must match the order of the `dataset`. "
        "Each entry should include `prompt_id` and `score` keys, with `score` being a float between 0 and 1. The evaluation corresponding to the `evaluator` must have `client_side=True`.",
        json_schema_extra={"example": "client_scores.jsonl"},
    ),
    dataset_dao: DatasetDAO = Depends(),
    stored_prompt_dao: StoredPromptDAO = Depends(),
    stored_prompt_response_dao: StoredPromptResponseDAO = Depends(),
    evaluator_dao: EvaluatorDAO = Depends(),
    judgement_dao: JudgementDAO = Depends(),
    default_prompt_dao: DefaultPromptDAO = Depends(),
    evaluation_dao: EvaluationDAO = Depends(),
) -> Dict[str, str]:
    """
    Uses the named `evaluator` to trigger an evaluation of quality scores for the
    selected LLM `endpoint` on the selected `dataset`, or selected `prompts` (by
    prompt_id). You can upload custom scores (and bypass the LLM judge entirely) by
    uploading a file via the `client_side_scores` argument. Once the evaluation has
    finished, you can access the scores using the `/v0/evaluation` endpoint. If a custom
    prompt is specified, its fields will overwrite the corresponding fields in each one
    of the evaluated prompts. If a response for a given prompt has already been
    provided for the selected endpoint, during another evaluation, then this response
    will be re-used during the current evaluation.
    """

    user_id = request_fastapi.state.user_id
    user_email = request_fastapi.state.user_email
    api_key = request_fastapi.headers["authorization"].removeprefix("Bearer ")

    # Check that the endpoints are valid
    invalid_endpoints = find_invalid_endpoints([endpoint])
    if invalid_endpoints:
        raise invalid_training_endpoints(invalid_endpoints)

    if prompts and client_side_scores:
        raise HTTPException(
            status_code=400,
            detail=f"Client-side scores for individual prompts not yet supported.",
        )

    prompt_ids = get_prompt_ids(
        dataset=dataset,
        prompts=prompts,
        user_id=user_id,
        dataset_dao=dataset_dao,
        stored_prompt_dao=stored_prompt_dao,
    )

    # evaluator_id
    raw_evaluators = evaluator_dao.filter(name=evaluator)
    if not raw_evaluators or raw_evaluators[0].user_id not in [None, user_id]:
        raise evaluator_not_found(evaluator)
    evaluator_id = raw_evaluators[0].id

    # default_prompt_id
    default_prompt_dict = ""
    default_prompt_id = None
    if default_prompt:
        raw_default_prompt = default_prompt_dao.filter(
            user_id=user_id,
            name=default_prompt,
        )
        if not raw_default_prompt:
            raise HTTPException(
                400,
                detail=f"The default prompt {default_prompt} does not exist in your account",
            )
        default_prompt_dict = raw_default_prompt[0].prompt
        default_prompt_id = raw_default_prompt[0].id

    if client_side_scores:
        file = client_side_scores.file.read()
        # TODO: check whether matches dataset
        try:
            lines = file.decode().split("\n")
            lines = [json.loads(l) for l in lines if l != ""]
            for ix, line in enumerate(lines):
                _expected_keys = set(["prompt_id", "score"])
                _found_keys = set(line.keys())
                _optional_keys = set(["response", "rationale"])
                if _missing := _expected_keys.difference(_found_keys):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Error in line {ix}: missing: {', '.join(_missing)}",
                    )
                if _extra := _found_keys.difference(
                    _expected_keys.union(_optional_keys),
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Error in line {ix}: extra key: {', '.join(_extra)}. Only allowed keys: {', '.join(_expected_keys.union(_optional_keys))}",
                    )
        except HTTPException as e:
            raise e
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail="Error processing uploaded scores",
            )

        # _model, _provider = endpoint.split("@")
        # if _provider != "external":
        #     raise HTTPException(
        #         status_code=400,
        #         detail=f"Error with {endpoint}: "
        #         "When submitting client-side scores, endpoint must be an `external` model, "
        #         "specified as e.g. `model@external`.",
        #     )

        # check whether the evaluator is a client side one:
        if (
            not hasattr(raw_evaluators[0], "client_side")
            or raw_evaluators[0].client_side is not True
        ):
            raise HTTPException(
                status_code=400,
                detail=f"The evaluator {evaluator} is not a client-side evaluator "
                f"(as client_side != True)",
            )
        # upload the data
        for l in lines:
            prompt_id = l["prompt_id"]
            score = l["score"]
            if not isinstance(score, float) or score < 0 or score > 1.0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Error with score from prompt_id: {prompt_id}, score: {score}",
                )
            rationale = l.get("rationale", "")

            num_tokens = 0

            stored_prompt_response_dao.create(
                prompt_id=prompt_id,
                prompt_variation_id=None,
                endpoint_str=endpoint,
                response=l.get("response", ""),
                num_tokens=num_tokens,
            )

            raw_ids = stored_prompt_response_dao.filter(
                prompt_id=prompt_id,
                prompt_variation_id=None,
                endpoint_str=endpoint,
            )
            response_id = raw_ids[0].id
            judge_model = "client_side"

            judgement_dao.create(
                response_id=response_id,
                judge_endpoint_str=judge_model,
                evaluator_id=evaluator_id,
                judgement=rationale,
                judgement_score=score,
            )
            ## add evaluation with the score
            evaluation_dao.create(
                prompt_id=prompt_id,
                prompt_variation_id=None,
                evaluator_id=evaluator_id,
                endpoint_str=endpoint,
                score=score,
            )
        return {"info": "Evaluation uploaded!"}

    # Send train job to the dataset_evaluation server
    send_to_dataset_evaluation_server(
        action="evaluate",
        user_id=user_id,
        user_email=user_email,
        api_key=api_key,
        prompts=prompt_ids,
        endpoint=endpoint,
        evaluator=evaluator,
        evaluator_id=evaluator_id,
        default_prompt=default_prompt_dict,
        default_prompt_id=default_prompt_id,
    )
    return {"info": "Dataset evaluation started! You will receive an email soon!"}


@admin_router.post("/evals/admin_trigger")
@on_prem.handle_on_prem("/evals/admin_trigger", "none")
def admin_trigger_eval(
    request_fastapi: Request,
    user_id: str = Query(
        ...,
        description="ID of the user that will own the triggered eval.",
        example="clb5hxxxxxxxxx601hooxp3ct",
    ),
    name: str = Query(
        ...,
        description="Name of the eval to use.",
        example="eval1",
    ),
    dataset: str = Query(
        ...,
        description="Name of the uploaded dataset to evaluate.",
        example="dataset1",
    ),
    endpoint: str = Query(
        ...,
        description=(
            "Name of the endpoint to evaluate."
            " Endpoints must be specified using the `model@provider` format."
        ),
        example="gpt-4o-mini@openai",
    ),
) -> Dict[str, str]:
    """
    Behaves like the user-specific endpoint but can be triggered as an admin on behalf of a given user.
    """

    raise NotImplementedError

    # api_key = os.getenv("UNIFY_API_KEY")

    # # Check if the dataset exists
    # if not dataset_exists(user_id, dataset):
    #     raise dataset_does_not_exist(dataset)

    # # Check that the endpoints are valid
    # invalid_endpoints = find_invalid_endpoints([endpoint])
    # if invalid_endpoints:
    #     raise invalid_training_endpoints(invalid_endpoints)
    # id_to_name = (
    #     on_prem.internal_id_to_displayname(user_id)
    #     if os.environ.get("ON_PREM")
    #     else gcp.internal_id_to_displayname(user_id)
    # )
    # name_to_id = {name: id_ for id_, name in id_to_name.items()}
    # internal_id = name_to_id.get(dataset, dataset)
    # # check if the eval name is valid
    # eval_id = eval_name_to_eval_id(user_id, name)

    # # Send train job to the dataset_evaluation server
    # send_to_dataset_evaluation_server(
    #     action="evaluate",
    #     user_id=user_id,
    #     user_email="",
    #     api_key=api_key,
    #     dataset=internal_id,
    #     endpoint=endpoint,
    #     eval_id=eval_id,
    # )
    # return {"info": "Dataset evaluation started!"}


def get_grouped_evaluations(
    prompt_ids: list[int],
    per_prompt: bool,
    evaluation_dao: EvaluationDAO,
):
    """Get the score for one dataset grouped by endpoint + evaluator (optionally per_prompt)"""
    scores = evaluation_dao.fetch_evaluation_scores(
        prompt_ids=prompt_ids,
        per_prompt=per_prompt,
    )
    return scores


def get_rationales(
    prompt_ids: list[int],
    endpoint: str,
    evaluator: str,
    evaluation_dao: EvaluationDAO,
    per_prompt: bool,
    responses: bool,
    rationales: bool,
    sub_scorers: bool,
    num_judges: int,
):
    rationales = evaluation_dao.fetch_rationales(
        prompt_ids=prompt_ids,
        endpoint=endpoint,
        evaluator=evaluator,
        per_prompt=per_prompt,
        responses=responses,
        rationales=rationales,
        sub_scorers=sub_scorers,
        num_judges=num_judges,
    )
    return rationales


@router.get("/evaluation")
def get_evaluations(
    request_fastapi: Request,
    dataset: Optional[str] = Query(
        default=None,
        description="Name of the uploaded dataset to get evaluations for. Must pass exactly one of `dataset`, `prompts`.",
        example="dataset1",
    ),
    prompts: Optional[str] = Query(
        default=None,
        description="Specify the prompts to get evaluations for. Pass a string of comma separated integers. Must pass exactly one of `dataset`, `prompts`.",
        example="34,89,127",
    ),
    endpoint: str = Query(
        default=None,
        description="The endpoint to fetch the evaluation for. "
        "If `None`, returns all available evaluations for the dataset and evaluator pair.",
        example="gpt-4o-mini@openai",
    ),
    evaluator: str = Query(
        default=None,
        description="Name of the evaluator to fetch the evaluation for. "
        "If `None`, returns all available evaluations for the dataset and "
        "endpoint pair.",
        example="eval1",
    ),
    per_prompt: bool = Query(
        default=False,
        description="If `True`, returns the scores on a per-prompt level. "
        "By default set to `False`. If `True` requires an endpoint "
        "and evaluator to be set.",
        example=False,
    ),
    include_runtime: bool = Query(
        default=False,
        description="If `True`, returns additional metrics regarding the runtime "
        "of the endpoint (ITL, TTFT, cost). By default set to `False`. ",
        example=False,
    ),
    return_response: bool = Query(
        default=False,
        description="If `True`, returns the LLM response to the prompt. This argument requires `per_prompt=True`."
        "By default set to `False`.",
        example=False,
    ),
    return_rationale: bool = Query(
        default=False,
        description="If `True`, returns the reasoning behind the score. This argument requires `per_prompt=True`."
        "By default set to `False`.",
        example=False,
    ),
    sub_scorers: bool = Query(
        default=False,
        description="If `True`, returns more in-depth summary statistics of the evaluation. "
        "Requires specification of both endpoint and evaluator, and per_prompt must be set to false.",
    ),
    ignore_missing: bool = Query(
        default=True,
        description="If `True`, then an empty dict is returned in cases where the dataset, agent or evaluator "
        "do not exist on the platform. If False, then an exception is raised if any of these do not exist.",
    ),
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
    evaluator_dao: EvaluatorDAO = Depends(),
    evaluation_dao: EvaluationDAO = Depends(),
    stored_prompt_dao: StoredPromptDAO = Depends(),
    latest_benchmark_dao: LatestBenchmarkDAO = Depends(),
) -> Dict:
    """
    Fetches evaluation results on a given dataset or for specific prompts, for a specific endpoint (optional)
    based on a specific evaluator (optional). If no `evaluator` is provided, then scores
    are returned for all valid evaluators. Similarly, if no `endpoint` is provided, then
    scores are returned for all valid endpoints.
    """
    # ToDo: implement the logic where the endpoint (required) is considered in the input
    user_id = request_fastapi.state.user_id

    try:
        prompt_ids = get_prompt_ids(
            dataset=dataset,
            prompts=prompts,
            user_id=user_id,
            dataset_dao=dataset_dao,
            stored_prompt_dao=stored_prompt_dao,
        )
    except HTTPException:
        if ignore_missing:
            return {}
        raise HTTPException(
            status_code=404,
            detail="The dataset and/or prompts requesting evaluations for do not exist.",
        )

    if return_rationale and not per_prompt:
        raise HTTPException(
            status_code=404,
            detail="If return_rationale=True, need to also have per_prompt=True.",
        )
    if return_response and not per_prompt:
        raise HTTPException(
            status_code=404,
            detail="If return_response=True, need to also have per_prompt=True.",
        )
    if per_prompt:
        if not endpoint or not evaluator:
            raise HTTPException(
                status_code=404,
                detail="If per_prompt=True, need to specify both endpoint and evaluator",
            )
    if sub_scorers:
        if per_prompt or not evaluator or not endpoint:
            raise HTTPException(
                status_code=404,
                detail="If sub_scorers=True, need to specify both endpoint and evaluator, and per_prompt must be false.",
            )

    if evaluator:
        raw_evaluators = evaluator_dao.filter(name=evaluator)
        if not raw_evaluators or raw_evaluators[0].user_id not in [None, user_id]:
            if ignore_missing:
                return {}
            raise evaluator_not_found(evaluator)

    if endpoint:
        invalid_endpoints = find_invalid_endpoints([endpoint])
        if invalid_endpoints:
            if ignore_missing:
                return {}
            raise HTTPException(
                status_code=400,
                detail=f"Could not find endpoint: {'.'.join(invalid_endpoints)}",
            )

    ret = {}

    if per_prompt or sub_scorers:
        num_judges = len(json.loads(raw_evaluators[0].judge_models))
        rationales = get_rationales(
            prompt_ids=prompt_ids,
            endpoint=endpoint,
            evaluator=evaluator,
            evaluation_dao=evaluation_dao,
            per_prompt=per_prompt,
            responses=return_response,
            rationales=return_rationale,
            sub_scorers=sub_scorers,
            num_judges=num_judges,
        )
        ret = {evaluator: {endpoint: rationales}}
        return ret
    else:
        # TODO: This doesn't account for prompt
        # variations / default prompts when per_prompt=True
        eval_results = get_grouped_evaluations(
            prompt_ids=prompt_ids,
            per_prompt=per_prompt,
            evaluation_dao=evaluation_dao,
        )

    latest_benchmarks = []
    if include_runtime:
        if os.environ.get("ON_PREM"):
            request_url = os.environ.get("PUBLIC_ORCHESTRA_URL", "") + "/benchmark"
            headers = {
                key: value
                for key, value in request_fastapi._headers.items()
                if key in ["content-type", "authorization"]
            }
            response = requests.get(request_url, headers=headers)
            latest_benchmarks = response.json()
            if response.status_code != 200:
                raise HTTPException(response.status_code, latest_benchmarks["detail"])
        else:
            latest_benchmarks = latest_benchmark_dao.get_benchmark_with_endpoints()

    acc = {}  # stores scores to aggregate
    endpoints = set()
    num_prompts = len(prompt_ids)

    for er in eval_results:
        if evaluator is not None and er.evaluator != evaluator:
            continue
        if endpoint is not None and er.endpoint_str != endpoint:
            continue

        if er.evaluator not in ret:  # check evaluator_name
            ret[er.evaluator] = {}
            acc[er.evaluator] = {}

        if er.endpoint_str not in ret[er.evaluator]:  # check endpoint_str
            ret[er.evaluator][er.endpoint_str] = {}
            acc[er.evaluator][er.endpoint_str] = []
            endpoints.add(er.endpoint_str)

        if not per_prompt:
            ret[er.evaluator][er.endpoint_str]["score"] = er.score  # add score
            ret[er.evaluator][er.endpoint_str]["progress"] = (
                100 * er.num_scores / num_prompts
            )
        if per_prompt:
            if "per_prompt" not in ret[er.evaluator][er.endpoint_str]:
                ret[er.evaluator][er.endpoint_str]["per_prompt"] = []
            per_prompt_score = {"id": er.prompt_id, "score": er.score}
            ret[er.evaluator][er.endpoint_str]["per_prompt"].append(per_prompt_score)
            acc[er.evaluator][er.endpoint_str].append(er.score)
            ret[er.evaluator][er.endpoint_str]["score"] = sum(
                acc[er.evaluator][er.endpoint_str],
            ) / len(acc[er.evaluator][er.endpoint_str])
            ret[er.evaluator][er.endpoint_str]["progress"] = (
                100 * len(acc[er.evaluator][er.endpoint_str]) / num_prompts
            )

        for _evaluator in ret:
            for lb in latest_benchmarks:
                on_prem = os.environ.get("ON_PREM")
                endpoint_str = lb["endpoint"] if on_prem else lb.endpoint_str
                input_cost = lb["input_cost"] if on_prem else lb.input_cost
                output_cost = lb["output_cost"] if on_prem else lb.output_cost
                ttft = lb["ttft"] if on_prem else lb.ttft
                itl = lb["itl"] if on_prem else lb.itl
                if endpoint_str in ret[_evaluator]:
                    ret[_evaluator][endpoint_str]["itl"] = float(itl)
                    ret[_evaluator][endpoint_str]["ttft"] = float(ttft)
                    ret[_evaluator][endpoint_str]["input_cost"] = float(input_cost)
                    ret[_evaluator][endpoint_str]["output_cost"] = float(output_cost)

    return ret


@router.delete(
    "/evaluation",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": {"info": "Evaluation deleted successfully!"},
                },
            },
        },
    },
)
def delete_evaluations(
    request_fastapi: Request,
    dataset: Optional[str] = Query(
        default=None,
        description="Name of the uploaded dataset to get delete evaluations for. Must pass exactly one of `dataset`, `prompts`.",
        example="dataset1",
    ),
    prompts: Optional[str] = Query(
        default=None,
        description="Specify the prompts to delete evaluations for. Pass a string of comma separated integers. Must pass exactly one of `dataset`, `prompts`.",
        example="34,89,127",
    ),
    endpoint: str = Query(
        default=None,
        description="The endpoint to delete the evaluation for. "
        "If `None`, deletes the evaluations for all endpoints.",
        example="gpt-4o-mini@openai",
    ),
    evaluator: str = Query(
        default=None,
        description="Name of the evaluator to delete the evaluation for. "
        "If `None`, deletes all available evaluations for the dataset and "
        "endpoint pair.",
        example="eval1",
    ),
    dataset_dao: DatasetDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
    evaluator_dao: EvaluatorDAO = Depends(),
    evaluation_dao: EvaluationDAO = Depends(),
    stored_prompt_dao: StoredPromptDAO = Depends(),
):
    """
    Deletes evaluations on a given dataset, for a specific endpoint (optional) based on
    a specific evaluator (optional). If no `evaluator` is provided, then evaluations for
    all valid evaluators are deleted. Similarly, if no `endpoint` is provided, then
    evaluations for all valid endpoints are deleted.
    """
    user_id = request_fastapi.state.user_id

    prompt_ids = get_prompt_ids(
        dataset=dataset,
        prompts=prompts,
        user_id=user_id,
        dataset_dao=dataset_dao,
        stored_prompt_dao=stored_prompt_dao,
    )

    # check endpoint and evaluator are valid
    if endpoint:
        invalid_endpoints = find_invalid_endpoints([endpoint])
        if invalid_endpoints:
            raise HTTPException(
                status_code=404,
                detail=f"Could not find endpoint: {'.'.join(invalid_endpoints)}",
            )
    if evaluator:
        raw_evaluators = evaluator_dao.filter(name=evaluator)
        if not raw_evaluators or raw_evaluators[0].user_id not in [None, user_id]:
            raise evaluator_not_found(evaluator)

    try:
        result = evaluation_dao.delete_evaluations(
            prompt_ids=prompt_ids,
            endpoint=endpoint,
            evaluator=evaluator,
        )
        return {
            "info": f"Evaluation deleted successfully. You deleted {result} evaluations.",
        }
    except:
        raise HTTPException(
            status_code=400,
            detail="An unknown error occured when deleting evaluations",
        )


### admin functions


@admin_router.post("/evaluations/upload_responses")
def upload_responses(
    request_fastapi: Request,
    prompt_id: int,
    endpoint_str: str,
    response: str,
    num_tokens: int,
    prompt_variation_id: Optional[int] = None,
    stored_prompt_response_dao: StoredPromptResponseDAO = Depends(),
):
    stored_prompt_response_dao.create(
        prompt_id=prompt_id,
        prompt_variation_id=prompt_variation_id,
        endpoint_str=endpoint_str,
        response=response,
        num_tokens=num_tokens,
    )


@admin_router.post("/evaluations/upload_judgements")
def upload_judgements(
    request_fastapi: Request,
    prompt_id: int,
    endpoint_str: str,
    evaluator_id: str,
    judge_model_list: list = Body(),
    judgement_list: list = Body(),
    judgement_scores: list = Body(),
    cache_hits: list = Body(),
    prompt_variation_id: Optional[int] = None,
    stored_prompt_response_dao: StoredPromptResponseDAO = Depends(),
    judgement_dao: JudgementDAO = Depends(),
    evaluator_dao: EvaluatorDAO = Depends(),
    evaluation_dao: EvaluationDAO = Depends(),
):

    try:
        raw_ids = stored_prompt_response_dao.filter(
            prompt_id=prompt_id,
            prompt_variation_id=prompt_variation_id,
            endpoint_str=endpoint_str,
        )
        response_id = raw_ids[0].id
    except Exception as e:
        raise e

    for judge_model, judgement, score in zip(
        judge_model_list,
        judgement_list,
        judgement_scores,
    ):
        if judge_model in cache_hits:
            continue
        judgement_dao.create(
            response_id=response_id,
            judge_endpoint_str=judge_model,
            evaluator_id=evaluator_id,
            judgement=judgement,
            judgement_score=score,
        )

    mean_score = sum(judgement_scores) / len(judgement_scores)

    # TODO: check if it's in rather than trying to add blindly.
    existing_evaluation = evaluation_dao.filter(
        prompt_id=prompt_id,
        prompt_variation_id=prompt_variation_id,
        evaluator_id=evaluator_id,
        endpoint_str=endpoint_str,
    )
    if not existing_evaluation:
        evaluation_dao.create(
            prompt_id=prompt_id,
            prompt_variation_id=prompt_variation_id,
            evaluator_id=evaluator_id,
            endpoint_str=endpoint_str,
            score=mean_score,
        )


@admin_router.get("/dataset/load_prompt")
def load_prompt(
    prompt_id: str,
    stored_prompt_dao: StoredPromptDAO = Depends(),
):
    ret = stored_prompt_dao.filter(id=prompt_id)
    return ret


@admin_router.get("/dataset/load_response")
def load_response(
    prompt_id: str,
    endpoint_str: str,
    prompt_variation_id: Optional[str] = None,
    stored_prompt_response_dao: StoredPromptResponseDAO = Depends(),
):

    ret = stored_prompt_response_dao.filter(
        prompt_id=prompt_id,
        prompt_variation_id=prompt_variation_id,
        endpoint_str=endpoint_str,
    )
    return ret


@admin_router.get("/dataset/load_judgement")
def load_judgement(
    request_fastapi: Request,
    prompt_id: str,
    endpoint_str: str,
    evaluator_id: str,
    judge_endpoint_str: str,
    prompt_variation_id: Optional[str] = None,
    judgement_dao: JudgementDAO = Depends(),
):

    ret = judgement_dao.find_judgement_response(
        prompt_id=prompt_id,
        prompt_variation_id=int(prompt_variation_id) if prompt_variation_id else None,
        endpoint_str=endpoint_str,
        evaluator_id=evaluator_id,
        judge_endpoint_str=judge_endpoint_str,
    )
    return ret


@admin_router.get("/prompt_variation")
def load_prompt_variation(
    prompt_id: str,
    default_prompt_id: str,
    stored_prompt_variation_dao: StoredPromptVariationDAO = Depends(),
):
    ret = stored_prompt_variation_dao.filter(
        prompt_id=prompt_id,
        default_prompt_id=default_prompt_id,
    )
    return ret


@admin_router.post("/prompt_variation")
def create_prompt_variation(
    prompt_id: str,
    default_prompt_id: str,
    stored_prompt_variation_dao: StoredPromptVariationDAO = Depends(),
):
    stored_prompt_variation_dao.create(
        prompt_id=prompt_id,
        default_prompt_id=default_prompt_id,
    )


@admin_router.get("/get_prompts")
def load_prompts(
    prompt_ids: str,
    user_id,
    stored_prompt_dao: StoredPromptDAO = Depends(),
):
    prompt_ids = [int(i) for i in prompt_ids.split(",") if i]
    ret = stored_prompt_dao.get_prompts(prompt_ids=prompt_ids, user_id=user_id)
    return ret


@admin_router.post("/update_router_trained")
def update_router_trained(
    router_id: str,
    user_id,
    router_dao: RouterDAO = Depends(),
):
    router_dao.update(id=router_id, trained=True)
    return {"info": "Success"}


@admin_router.post("/update_router_deployed")
def update_router_deployed(
    user_id: str,
    router_id: str,
    gcp_router_id: str,
    router_dao: RouterDAO = Depends(),
):
    router_dao.update(id=router_id, gcp_router_id=int(gcp_router_id))
    return {"info": "Success"}
