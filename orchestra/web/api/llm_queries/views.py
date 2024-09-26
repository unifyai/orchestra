import json
import os
import time
from typing import List, Union

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.param_functions import Depends
from fastapi.responses import JSONResponse, StreamingResponse
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
from orchestra.web.api.llm_queries.schema import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    RouterScoresResponse,
)
from orchestra.web.api.utils.bg_tasks import db_operations
from orchestra.web.api.utils.dynamic_routing import (
    NeuralRouter,
    Router,
    get_router_endpoint_id,
)
from orchestra.web.api.utils.helpers import filter_orchestra_only_args
from orchestra.web.api.utils.http_responses import (
    custom_api_key_not_found,
    insufficient_credits_error,
    invalid_messages,
    invalid_model_str,
)
from orchestra.web.api.utils.on_prem import handle_on_prem

router = APIRouter()


@router.post("/chat/completions", response_model=ChatCompletionResponse)
def chat_completions(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    background_tasks: BackgroundTasks,
    request_fastapi: Request,
    request: Union[ChatCompletionRequest, List[ChatCompletionRequest]],
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
    OpenAI compatible `/chat/completions` endpoint for LLM inference.
    Check the OpenAI
    [API reference](https://platform.openai.com/docs/api-reference/chat)
    for the most updated documentation. The ground truth is always the latest OpenAI API
    Reference. The arguments below are copied for convenience, but might not be fully
    up-to-date at all times.
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
    if isinstance(request, list):
        request_priority_list = request
    else:
        request_priority_list = [request]

    try_request = 0
    num_tries = max(5, len(request_priority_list))
    region = None

    request_failed, err = False, None

    while try_request >= 0 and try_request < num_tries:
        request = request_priority_list[try_request]

        if not request.tools:
            request.parallel_tool_calls = None

        try:
            # TODO: Check that model exists
            model_priority_list = []
            sublist_type = 0
            for idx, model_tag in enumerate(request.model.split("->")):
                model_provider = model_tag.split("@")
                if len(model_provider) == 1:
                    # add placeholder <model> and <provider> where missing
                    if idx == 0 or sublist_type == -1:
                        sublist_type = -1
                        model_provider = [model_provider[0], "<provider>"]
                    elif idx != 0 or sublist_type == 1:
                        sublist_type = 1
                        model_provider = ["<model>", model_provider[0]]
                else:
                    sublist_type = 0
                model_priority_list.append(model_provider)

            current_model, current_provider = None, None
            for idx, model_provider in enumerate(model_priority_list):
                # replace <model> by relevant model while moving forward
                if "<model>" in model_provider and current_model is not None:
                    model_provider[0] = current_model
                elif "<model>" not in model_provider:
                    current_model = model_provider[0]

                # replace <provider> by relevant provider while moving backward
                reverse_idx = -1 - idx
                reverse_model_provider = model_priority_list[reverse_idx]
                if (
                    "<provider>" in reverse_model_provider
                    and current_provider is not None
                ):
                    reverse_model_provider[1] = current_provider
                elif "<provider>" not in reverse_model_provider:
                    current_provider = reverse_model_provider[1]

                # check that there are no placeholders left
                assert "<model>" not in model_provider
                assert "<provider>" not in reverse_model_provider
        except Exception:
            raise invalid_model_str

        model_params = request.model_dump()
        region = model_params.pop("region", None)
        region_str = region if region is not None else ""
        model_region_priority_list = []
        for idx, region_tag in enumerate(region_str.split("->")):
            region_tag = region_tag if region_tag else None
            model_region_priority_list += [
                [*model, region_tag] for model in model_priority_list
            ]

        try:
            messages = request.messages
        except Exception:
            raise invalid_messages

        # TODO: Add validation of the other parameters if mandatory
        on_prem = os.environ.get("ON_PREM")
        user_id = request_fastapi.state.user_id
        use_custom_keys = request.use_custom_keys
        user = users_dao.get_user_with_id(user_id)
        store_prompt = user.store_prompts if user else True
        store_prompt = True if store_prompt is None else store_prompt
        store_query_body, store_response_body = False, False
        if store_prompt:
            store_query_body = True if request.log_query_body else False
            store_response_body = True if request.log_response_body else False
        if not on_prem:
            available_credits = float(user.credits if user else 0)

        model, provider, region = model_region_priority_list[0]
        try_provider = 0
        router_choices = None
        using_router = model.startswith("router")
        router_str = provider if using_router else None
        num_tries_provider = min(5, len(model_region_priority_list))

        custom_api_key = None
        if use_custom_keys:
            try:
                custom_api_key = custom_api_key_dao.filter(
                    user_id=user_id,
                    key=provider,
                )[0].value
            except IndexError:
                raise custom_api_key_not_found

        if using_router:
            if os.environ.get("ON_PREM"):
                endpoint_id = 1
            else:
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

        try:
            while try_provider >= 0 and try_provider < num_tries_provider:
                if provider not in PROVIDER_CLASSES or using_router:
                    # 1 token ~ 4 letters + 0.25 safety ratio for different tokenizers
                    num_tokens_est = 0
                    for msg in messages:
                        if msg.get("content") is not None:
                            num_tokens_est += len(msg["content"])
                    input_tokens = num_tokens_est * 1.25

                    # neural routing
                    if using_router:
                        if router_choices is None:
                            router = NeuralRouter(
                                request.model,
                                request_fastapi,
                                endpoint_dao,
                                benchmark_run_dao,
                            )
                            # TODO: add error message if the router is not deployed
                            router_choices = router(
                                messages[-1]["content"],
                                endpoint_id,
                                input_tokens=input_tokens,
                            )
                            model_region_priority_list = router_choices
                    # performance routing
                    else:
                        model, provider, _ = Router(
                            request.model,
                            request_fastapi,
                            endpoint_dao,
                            benchmark_run_dao,
                        )(input_tokens=input_tokens)
                        model_region_priority_list[try_provider] = (
                            model,
                            provider,
                            region,
                        )

                model, provider, region = model_region_priority_list[try_provider]

                extra_args = tuple()
                if provider == "custom":
                    extra_args = (custom_endpoint_dao, custom_api_key_dao, user_id)
                lm = PROVIDER_CLASSES[provider](
                    model, *extra_args, custom_api_key=custom_api_key
                )
                if not on_prem and available_credits <= 0 and not use_custom_keys:
                    raise insufficient_credits_error

                stream = request.stream

                filtered_params = filter_orchestra_only_args(request.model_dump())
                if region:
                    filtered_params["region"] = region
                try:
                    response, cost = lm(messages=messages, **filtered_params)
                    try_provider = try_request = -1
                except HTTPException as e:
                    if e.status_code in [400, 403, 404, 429] or e.status_code >= 500:
                        try_provider += 1
                        if try_provider >= num_tries_provider:
                            raise e
                    else:
                        raise e
            # raise HTTPException(400, "some error")
        except HTTPException as e:
            if e.status_code in [400, 403, 404, 429] or e.status_code >= 500:
                try_request += 1
                if try_request >= num_tries:
                    request_failed, err = True, e
                    break
                # raise e
            else:
                request_failed, err = True, e
                break
                # raise e

    # convert str to list
    tags = request.tags
    if tags and isinstance(request.tags, str):
        tags = [tags]

    request_body = request.model_dump()
    if region:
        request_body["region"] = region
    db_operations_kwargs = {
        "user_id": user_id,
        "secondary_user_id": request.user,
        "model": model,
        "provider": provider,
        "query_body": json.dumps(request_body) if store_query_body else "",
        "signature": request.signature,
        "used_router": using_router,
        "router": router_str,
        "tags": tags,
        "model_dao": model_dao,
        "provider_dao": provider_dao,
        "endpoint_dao": endpoint_dao,
        "custom_endpoint_dao": custom_endpoint_dao,
        "query_dao": query_dao,
        "users_dao": users_dao,
    }

    if request_failed:
        background_tasks.add_task(
            db_operations,
            cost=0,
            processing_time=0,
            usage=0,
            response_body=json.dumps({"detail": err.detail}),
            status_code=err.status_code,
            **db_operations_kwargs,
        )
        return JSONResponse(
            status_code=err.status_code,
            content={"detail": err.detail},
        )

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
                cost=response.total_cost if not use_custom_keys else 0,
                processing_time=processing_time,
                usage=chat_response.usage,
                response_body=(
                    json.dumps(
                        chat_response.model_dump() if store_response_body else "",
                    )
                    if store_prompt
                    else ""
                ),  # TODO this isn't the whole response
                status_code=200,
                **db_operations_kwargs,
            )

        return StreamingResponse(stream_and_update_db())
    else:
        processing_time = (time.time() - t0) * 1000
        background_tasks.add_task(
            db_operations,
            cost=cost if not use_custom_keys else 0,
            processing_time=processing_time,
            usage=response["usage"],
            response_body=json.dumps(response) if store_response_body else "",
            status_code=200,
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
@handle_on_prem("/router/scores", "none")
def get_completions(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    request_fastapi: Request,
    request: ChatCompletionRequest,
    endpoint_dao: EndpointDAO = Depends(),
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
) -> RouterScoresResponse:
    rc = NeuralRouter(request.model, endpoint_dao, benchmark_run_dao)
    scores = rc(request_fastapi, request.messages[-1]["content"], debug=True)
    return RouterScoresResponse(scores=scores)
