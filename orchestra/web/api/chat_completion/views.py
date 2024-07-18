import json
import time
from typing import Any, Dict, Union

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.param_functions import Depends
from fastapi.responses import StreamingResponse
from providers.completion import PROVIDER_CLASSES

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.custom_api_key_dao import CustomApiKeyDAO
from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.custom_router_dao import CustomRouterDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.chat_completion.schema import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    QueryMetricsRequest,
    RouterScoresResponse,
)
from orchestra.web.api.users.views import get_credits
from orchestra.web.api.utils.bg_tasks import db_operations
from orchestra.web.api.utils.dynamic_routing import (
    RouterConfig,
    dynamic_routing,
    get_router_endpoint_id,
    parse_endpoint,
)
from orchestra.web.api.utils.helpers import filter_request_params
from orchestra.web.api.utils.http_responses import (
    insufficient_credits_error,
    invalid_messages,
    invalid_model_str,
)

router = APIRouter()


@router.post("/chat/completions", response_model=ChatCompletionResponse)
def get_completions(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    background_tasks: BackgroundTasks,
    request_fastapi: Request,
    request: ChatCompletionRequest,
    response_fastapi: Response,
    users_dao: UsersDAO = Depends(),
    model_dao: ModelDAO = Depends(),
    provider_dao: ProviderDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
    query_dao: QueryDAO = Depends(),
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
    custom_api_key_dao: CustomApiKeyDAO = Depends(),
    custom_router_dao: CustomRouterDAO = Depends(),
) -> Union[ChatCompletionResponse, StreamingResponse]:
    """
    OpenAI compatible /chat/completions endpoint for LLM inference.
    \f
    :param background_tasks: FastAPI background tasks.
    :param request_fastapi: FastAPI request object.
    :param request: ChatCompletionRequest object.
    :param users_dao: DAO for users models.
    :param model_dao: DAO for model models.
    :param provider_dao: DAO for provider models.
    :param endpoint_dao: DAO for endpoint models.
    :param query_dao: DAO for query models.

    :return: ChatCompletionResponse object.

    :raises HTTPException: when user has insufficient credits.
    """

    try:
        # TODO: Check that model exists
        model_priority_list = []
        for model_tag in request.model.split("->"):
            model_provider = model_tag.split("@")
            assert len(model_provider) == 2
            model_priority_list.append(model_provider)
    except Exception:
        raise invalid_model_str

    try:
        messages = request.messages
    except Exception:
        raise invalid_messages

    # TODO: Add validation of the other parameters if mandatory

    user_id = request_fastapi.state.user_id
    user = get_credits(request_fastapi, users_dao=users_dao)
    available_credits = float(user.credits if user else 0)
    store_prompt = user.store_prompts if user else True
    store_prompt = True if store_prompt is None else store_prompt

    model, provider = model_priority_list[0]
    try_provider = 0
    router_choices = None
    using_router = model.startswith("router")
    router_str = provider if using_router else None
    num_tries = 5

    if using_router:
        # parse router string
        tmp = model.split("_", 1)
        if len(tmp) == 1:
            endpoint_id = get_router_endpoint_id(
                custom_router_dao,
                user_id=None,
                router_name="foundation_router",
            )
        else:
            router_name = tmp[1]
            try:
                endpoint_id = get_router_endpoint_id(
                    custom_router_dao,
                    user_id,
                    router_name,
                )
            except:
                # TODO: add proper error message for this
                raise invalid_model_str

    t0 = time.time()

    while try_provider >= 0 and try_provider < num_tries:
        if provider not in PROVIDER_CLASSES or using_router:
            # Dynamic routing
            if using_router:
                if router_choices is None:
                    rc = RouterConfig(request.model, endpoint_dao, benchmark_run_dao)
                    num_tokens_est = 0
                    for msg in messages:
                        if msg.get("content") is not None:
                            num_tokens_est += len(msg["content"])
                    # 1 token ~ 4 letters + 0.25 safety ratio for different tokenizers
                    # TODO: add error message if the router is not deployed
                    router_choices = rc(
                        messages[-1]["content"],
                        num_tokens_est * 1.25,
                        endpoint_id,
                    )
                    model_priority_list = router_choices
            else:  # Non model routing, TODO: clean up to simplify
                target_metric, metrics_thresholds = parse_endpoint(provider)
                model, provider = dynamic_routing(
                    endpoint_dao,
                    benchmark_run_dao,
                    target_metric,
                    models=(model,),
                    metrics_thresholds=metrics_thresholds,
                )
                # TODO: this is probably still buggye with corner cases,
                # more exhaustive testing is needed.
                model_priority_list[try_provider] = (model, provider)
        if try_provider >= len(model_priority_list):
            break
        model, provider = model_priority_list[try_provider]

        extra_args = tuple()
        if provider == "custom":
            extra_args = (custom_endpoint_dao, custom_api_key_dao, user_id, model)
        lm = PROVIDER_CLASSES[provider](model, *extra_args)
        if available_credits <= 0:
            raise insufficient_credits_error

        stream = request.stream

        filtered_params = filter_request_params(request.model_dump())

        try:
            response, cost = lm(messages=messages, **filtered_params)
            try_provider = -1
        except HTTPException as e:
            if e.status_code == 429 or e.status_code >= 500:
                try_provider += 1
                if try_provider >= num_tries:
                    raise e
            else:
                raise e

    # TODO: Handle when response is None
    if not response:
        return ChatCompletionResponse(
            model=request.model,
            created=0,
            id="",
            choices=[],
            object="chat.completion",
            usage={},
        )

    db_operations_kwargs = {
        "user_id": user_id,
        "secondary_user_id": request.user,
        "model": model,
        "provider": provider,
        "prompt": messages if store_prompt else [],
        "signature": request.signature,
        "used_router": using_router,
        "router": router_str,
        "model_dao": model_dao,
        "provider_dao": provider_dao,
        "endpoint_dao": endpoint_dao,
        "query_dao": query_dao,
        "users_dao": users_dao,
    }

    processing_time = (time.time() - t0) * 1000

    if stream:

        def stream_and_update_db():  # noqa: WPS430 # TODO: Should this be async?
            for part_dict in response.generator():
                part_dict["model"] = f"{model}@{provider}"
                chat_response = ChatCompletionResponse(**part_dict)
                yield f"data: {json.dumps(chat_response.model_dump())}\n\n"  # noqa: WPS237, E501
            processing_time = (time.time() - t0) * 1000
            background_tasks.add_task(
                db_operations,
                cost=response.total_cost,
                processing_time=processing_time,
                usage=chat_response.usage,
                **db_operations_kwargs,
            )

        return StreamingResponse(stream_and_update_db())
    else:
        processing_time = (time.time() - t0) * 1000
        background_tasks.add_task(
            db_operations,
            cost=cost,
            processing_time=processing_time,
            usage=response["usage"],
            **db_operations_kwargs,
        )

    response["model"] = f"{model}@{provider}"
    response["usage"]["cost"] = cost
    processing_time = (time.time() - t0) * 1000
    response_fastapi.headers["openai-processing-ms"] = f"{processing_time:.0f}"
    return ChatCompletionResponse(**response)


@router.post(
    "/router/scores",
    include_in_schema=False,
    response_model=RouterScoresResponse,
)
def get_completions(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request: ChatCompletionRequest,
    endpoint_dao: EndpointDAO = Depends(),
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
) -> RouterScoresResponse:
    rc = RouterConfig(request.model, endpoint_dao, benchmark_run_dao)
    scores = rc(request.messages[-1]["content"], debug=True)
    return RouterScoresResponse(scores=scores)


@router.get("/query_metrics")
def get_query_metrics(
    request_fastapi: Request,
    request: QueryMetricsRequest,
) -> Dict[str, Any]:
    """
    Returns aggregated telemetry data from previous queries to the /chat/completions
    endpoint.
    """
    import requests

    response = requests.get(
        "https://api.airfold.co/v1/pipes/queries_metrics.json",
        # TODO: mb will rotate this tomorrow
        headers={
            "Authorization": "Bearer aft_mpbZHI19EHe8CsRnUGsQ4f2ALIJ.KW4wY9z6u21Lbdnm8FS58Lqh6U3rTpsmo3FFeAsubCY",
        },
        params={
            "user_id": request_fastapi.state.user_id,
            "secondary_user_id": request.secondary_user_id,
            "start_time": request.start_time,
            "end_time": request.end_time,
            "models": request.models,
            "providers": request.providers,
            "interval": request.interval,
        },
    )

    if response.status_code == 200:
        data = response.json()
        return data
    else:
        # TODO: meaningful errors
        print("Error:", response.status_code, response.text)
