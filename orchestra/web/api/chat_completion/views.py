import json
from typing import Union

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.param_functions import Depends
from fastapi.responses import StreamingResponse
from providers.completion import PROVIDER_CLASSES

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.datapoint_dao import DatapointDAO
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
    performance_based_routing,
)

router = APIRouter()


@router.post("/chat/completions", response_model=ChatCompletionResponse)
def get_completions(  # noqa: C901, WPS210, WPS231, WPS211, WPS217, WPS238
    background_tasks: BackgroundTasks,
    request_fastapi: Request,
    request: ChatCompletionRequest,
    users_dao: UsersDAO = Depends(),
    model_dao: ModelDAO = Depends(),
    provider_dao: ProviderDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
    query_dao: QueryDAO = Depends(),
    benchmark_run_dao: BenchmarkRunDAO = Depends(),
    datapoint_dao: DatapointDAO = Depends(),
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
    user = get_credits(request_fastapi, users_dao=users_dao)
    available_credits = float(user.credits if user else 0)

    if provider.split("-")[0] in ["lowest", "highest"]:
        provider = performance_based_routing(
            model,
            provider,
            model_dao,
            benchmark_run_dao,
            datapoint_dao,
        )

    lm = PROVIDER_CLASSES[provider](model)
    if available_credits < lm.max_cost:
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

    response, cost = lm(messages=messages, **filtered_params)

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

    if stream:

        def stream_and_update_db():  # noqa: WPS430 # TODO: Should this be async?
            for part_dict in response.generator():
                part_dict["model"] = f"{model}@{provider}"
                chat_response = ChatCompletionResponse(**part_dict)
                yield f"data: {json.dumps(chat_response.model_dump())}\n\n"  # noqa: WPS237, E501
            background_tasks.add_task(
                db_operations,
                cost=response.total_cost,
                **db_operations_kwargs,
            )

        return StreamingResponse(stream_and_update_db())
    else:
        background_tasks.add_task(db_operations, cost=cost, **db_operations_kwargs)

    response["model"] = f"{model}@{provider}"
    response["usage"]["cost"] = cost
    return ChatCompletionResponse(**response)
