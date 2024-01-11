import json
from typing import Union

from fastapi import APIRouter, HTTPException, Request
from fastapi.param_functions import Depends
from fastapi.responses import StreamingResponse
from litellm.utils import Usage
from models.llm import CompletionsModel

from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.chat_completion.schema import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from orchestra.web.api.users.views import get_credits

router = APIRouter()


@router.post("/chat/completion", response_model=ChatCompletionResponse)
async def get_completions(  # noqa: C901, WPS210, WPS231
    request_fastapi: Request,
    request: ChatCompletionRequest,
    users_dao: UsersDAO = Depends(),
) -> Union[ChatCompletionResponse, StreamingResponse]:
    """
    Get chat completions based on the request.

    :param request_fastapi: FastAPI request object.
    :param request: ChatCompletionRequest object.
    :param users_dao: DAO for users models.

    :return: ChatCompletionResponse object.

    :raises HTTPException: when user has insufficient credits.
    """
    user_id = request_fastapi.state.user_id
    users = await get_credits(request_fastapi, users_dao=users_dao)
    available_credits = float(users[0].credits if users else 0)
    model, provider = request.model.split("@")
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
        )

    if stream:

        async def stream_and_update_db():  # noqa: WPS430
            async for part_dict in response.generator():
                part_dict["model"] = f"{model}@{provider}"
                yield json.dumps(part_dict)
            await users_dao.recharge_credit(user_id, -response.total_cost)

        return StreamingResponse(stream_and_update_db())
    else:
        await users_dao.recharge_credit(user_id, -cost)

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
    )
