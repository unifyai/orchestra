import asyncio
import json
from typing import Union

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.param_functions import Depends
from fastapi.responses import StreamingResponse
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
from orchestra.web.api.utils import (
    db_operations,
    filter_request_params,
    insufficient_credits_error,
)

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
    try:
        model, provider = request.model.split("@")
    except Exception:
        raise HTTPException(
            status_code=400,  # noqa: WPS432
            detail=(
                "Invalid model. The expected format is <model-id>@<provider>. "
                "See https://unify.ai/docs/hub/concepts/models.html "
                "for more information."
            ),
        )

    try:
        messages = request.messages
    except Exception:
        raise HTTPException(
            status_code=400,  # noqa: WPS432
            detail="Invalid input. Messages not in input.",
        )

    # TODO: Add validation of the other parameters if mandatory

    user_id = request_fastapi.state.user_id
    user = await get_credits(request_fastapi, users_dao=users_dao)
    available_credits = float(user.credits if user else 0)

    language_model = CompletionsModel(provider=provider, model=model)
    cost_max = language_model.get_cost_max()
    if available_credits < cost_max:
        raise insufficient_credits_error

    stream = request.stream

    filtered_params = filter_request_params(request.model_dump())

    db_operations_kwargs = {
        "user_id": user_id,
        "model": model,
        "provider": provider,
        "model_dao": model_dao,
        "provider_dao": provider_dao,
        "endpoint_dao": endpoint_dao,
        "query_dao": query_dao,
        "users_dao": users_dao,
    }

    response, cost = language_model.get_completion(
        messages=messages,
        **filtered_params,
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
        )

    if stream:

        async def stream_and_update_db():  # noqa: WPS430
            async for part_dict in response.generator():
                part_dict["model"] = f"{model}@{provider}"
                yield f"data: {json.dumps(part_dict)}\n\n"  # noqa: WPS237
                await asyncio.sleep(0)
            background_tasks.add_task(
                db_operations,
                cost_coroutine=response.total_cost,
                **db_operations_kwargs,
            )

        return StreamingResponse(stream_and_update_db())
    else:
        background_tasks.add_task(
            db_operations, cost_coroutine=cost, **db_operations_kwargs
        )

    response.model = f"{model}@{provider}"
    return response  # TODO: Why not ChatcompletionResponse?
