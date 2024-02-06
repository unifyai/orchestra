import asyncio
import json
from typing import Union

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.param_functions import Depends
from fastapi.responses import StreamingResponse
from litellm.utils import Usage
from models.llm import CompletionsModel

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.chat_completion.schema import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from orchestra.web.api.users.views import get_credits
from orchestra.web.api.utils import db_operations

router = APIRouter()


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def get_completions(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    background_tasks: BackgroundTasks,
    request_fastapi: Request,
    request: ChatCompletionRequest,
    users_dao: UsersDAO = Depends(),
    model_dao: ModelDAO = Depends(),
    provider_dao: ProviderDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
    query_dao: QueryDAO = Depends(),
) -> Union[ChatCompletionResponse, StreamingResponse]:
    """
    Get chat completions based on the request.

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
    user_id = request_fastapi.state.user_id
    user = await get_credits(request_fastapi, users_dao=users_dao)
    available_credits = float(user.credits if user else 0)
    try:
        model, provider = request.model.split("@")
    except Exception:
        raise HTTPException(
            status_code=400,  # noqa: WPS432
            detail="Invalid input format. Expected format: 'model@provider'.",
        )

    language_model = CompletionsModel(
        provider=provider,
        model=model,
    )
    cost_max = language_model.get_cost_max()
    if available_credits < cost_max:
        raise HTTPException(
            status_code=402,  # noqa: WPS432
            detail="Insufficient credits",
        )
    stream = request.stream
    response, cost = language_model.get_completion(
        messages=request.messages,
        temperature=request.temperature,
        stream=stream,
    )
    if not response:
        # TODO: Handle when response is None
        return ChatCompletionResponse(
            model=request.model,
            created=0,
            id="",
            choices=[],
            object="chat.completion",
            usage={},
            _response_ms=0,
        )

    if stream:

        async def stream_and_update_db():  # noqa: WPS430
            async for part_dict in response.generator():
                part_dict["model"] = f"{model}@{provider}"
                yield f"data: {json.dumps(part_dict)}\n\n"  # noqa: WPS237
                await asyncio.sleep(0)
            await users_dao.recharge_credit(user_id, -response.total_cost)
            background_tasks.add_task(
                db_operations,
                user_id,
                response.total_cost,
                model,
                provider,
                model_dao,
                provider_dao,
                endpoint_dao,
                query_dao,
            )

        return StreamingResponse(stream_and_update_db())
    else:
        await users_dao.recharge_credit(user_id, -cost)
        background_tasks.add_task(
            db_operations,
            user_id,
            cost,
            model,
            provider,
            model_dao,
            provider_dao,
            endpoint_dao,
            query_dao,
        )

    if isinstance(response, ChatCompletionResponse):
        response.model = f"{model}@{provider}"
        return response

    if isinstance(response["usage"], Usage):
        usage = response["usage"].model_dump()
    else:
        usage = response["usage"]

    if response.get("choices", None):
        choices = []
        for choice in response.get("choices", None):
            choices.append(choice.model_dump())

    return ChatCompletionResponse(
        model=f"{model}@{provider}",
        created=response.get("created", None),
        id=response.get("id", None),
        choices=choices,
        object=response.get("object", None),
        usage=usage,
        _response_ms=response.get("_response_ms", None),
    )
