import json
import os
import time
from typing import List, Union

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.param_functions import Depends
from fastapi.responses import JSONResponse, StreamingResponse
from providers.completion import PROVIDER_CLASSES

from orchestra.db.dao.custom_api_key_dao import CustomApiKeyDAO
from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.users_dao import UsersDAO

# Async DAOs
from orchestra.db.dao.async_custom_api_key_dao import AsyncCustomApiKeyDAO
from orchestra.db.dao.async_custom_endpoint_dao import AsyncCustomEndpointDAO
from orchestra.db.dao.async_users_dao import AsyncUsersDAO
from sqlalchemy.ext.asyncio import AsyncSession
from orchestra.db.dependencies import get_async_db_session, get_db_session
from orchestra.settings import settings
from orchestra.web.api.dependencies import _ro_session
from orchestra.web.api.llm_queries.schema import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from orchestra.web.api.utils.bg_tasks import db_operations
from orchestra.web.api.utils.helpers import filter_orchestra_only_args
from orchestra.web.api.utils.http_responses import (
    insufficient_credits_error,
    invalid_messages,
    invalid_model_str,
    not_found,
)

router = APIRouter()


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    background_tasks: BackgroundTasks,
    request_fastapi: Request,
    request: Union[ChatCompletionRequest, List[ChatCompletionRequest]],
    response_fastapi: Response,
    session: AsyncSession = Depends(get_async_db_session),
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
    :param endpoint_dao: DAO for endpoint models.
    :param benchmark_run_dao: DAO for benchmark run models.
    :param custom_endpoint_dao: DAO for custom endpoint models.
    :param custom_api_key_dao: DAO for custom api key models.
    :param custom_router_dao: DAO for custom router models.
    :param router_dao: DAO for router models.

    :return: ChatCompletionResponse object.

    :raises HTTPException: when user has insufficient credits.
    """
    custom_endpoint_dao = AsyncCustomEndpointDAO(session)
    custom_api_key_dao = AsyncCustomAsyncApiKeyDAO(session)

    if isinstance(request, list):
        request_priority_list = request
    else:
        request_priority_list = [request]

    try_request = 0
    num_tries = min(5, len(request_priority_list))
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
        with _ro_session() as ro_sess:
            user = UsersDAO(ro_sess).get_user_with_id(user_id)
        store_prompt = user.store_prompts if user else True
        store_prompt = True if store_prompt is None else store_prompt
        store_query_body, store_response_body = False, False
        if store_prompt:
            store_query_body = True if request.log_query_body else False
            store_response_body = True if request.log_response_body else False
        if not on_prem:
            available_credits = float(user.credits if user else 0)

        model, provider, region = model_region_priority_list[0]
        provider_str = (
            provider if provider == "custom" else provider.replace("custom-", "")
        )
        try_provider = 0
        using_router = model.startswith("router")
        router_str = provider if using_router else None
        num_tries_provider = min(5, len(model_region_priority_list))

        if using_router:
            raise HTTPException(
                status_code=400,
                detail="Router functionality has been removed.",
            )

        t0 = time.time()
        try:
            while try_provider >= 0 and try_provider < num_tries_provider:
                # get the current model, provider and region from the list
                model, provider, region = model_region_priority_list[try_provider]
                provider_str = (
                    provider
                    if provider == "custom"
                    else provider.replace("custom_", "")
                )

                # fetch custom api key
                custom_api_key, custom_endpoint = None, None
                if use_custom_keys:
                    # the request is made to a regular endpoint
                    # but using custom keys with the provider
                    if "custom" not in provider:
                        try:
                            custom_api_key = await custom_api_key_dao.filter(
                                user_id=user_id,
                                key=provider,
                            )[0].value
                        except IndexError:
                            raise not_found("Custom API Key")
                    # the request is made to a custom endpoint
                    # either to an existing provider or a custom provider
                    else:
                        try:
                            custom_endpoint = await custom_endpoint_dao.filter(
                                user_id=user_id,
                                name=model,
                            )[0]
                        except IndexError:
                            raise not_found("Custom endpoint")
                        try:
                            custom_api_key = await custom_api_key_dao.filter(
                                id=custom_endpoint.key_id,
                            )[0].value
                        except IndexError:
                            raise not_found("Custom API Key")

                # get the provider class
                lm = PROVIDER_CLASSES[provider_str](
                    model,
                    custom_endpoint=custom_endpoint,
                    custom_api_key=custom_api_key,
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

    # Get organization context from request state (None = personal query)
    organization_id = getattr(request_fastapi.state, "organization_id", None)

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
        "organization_id": organization_id,
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
            msg = ""
            for part_dict in response.generator():
                part_dict["model"] = f"{model}@{provider}"
                chat_response = ChatCompletionResponse(**part_dict)
                content = chat_response.choices[0]["delta"]["content"]
                msg += content if content else ""
                yield f"data: {json.dumps(chat_response.model_dump())}\n\n"  # noqa: WPS237, E501
            processing_time = (time.time() - t0) * 1000
            chat_response.choices[0]["delta"]["content"] = msg
            cost = (
                response.total_cost * settings.chat_completions_markup_rate
                if not use_custom_keys
                else 0
            )
            background_tasks.add_task(
                db_operations,
                cost=cost,
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

        return StreamingResponse(stream_and_update_db(), media_type="text/event-stream")
    else:
        processing_time = (time.time() - t0) * 1000
        cost = (
            cost * settings.chat_completions_markup_rate if not use_custom_keys else 0
        )
        background_tasks.add_task(
            db_operations,
            cost=cost,
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
