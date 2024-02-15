from typing import Callable

from fastapi import HTTPException

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.endpoint.views import get_endpoint
from orchestra.web.api.model.views import get_model
from orchestra.web.api.provider.views import get_provider
from orchestra.web.api.query.schema import QueryModelRequest
from orchestra.web.api.query.views import create_query_model

# HTTP responses

insufficient_credits_error = HTTPException(
    status_code=402,
    detail=(
        "Whoops! It seems like this account doesn't have enough credits. "
        "To get a recharge, visit https://console.unify.ai/"
    ),
)

# Background tasks

# HTTP responses

insufficient_credits_error = HTTPException(
    status_code=402,
    detail=(
        "Whoops! It seems like this account doesn't have enough credits. "
        "To get a recharge, visit https://console.unify.ai/"
    ),
)

# Background tasks


def db_operations(  # noqa: WPS211, WPS217, WPS210
    user_id: str,
    cost_deferred_fn: Callable,
    model: str,
    provider: str,
    model_dao: ModelDAO,
    provider_dao: ProviderDAO,
    endpoint_dao: EndpointDAO,
    query_dao: QueryDAO,
    users_dao: UsersDAO,
):
    """
    Perform database operations.

    :param user_id: user id.
    :param cost_deferred_fn: deferred cost computation of the operation.
    :param model: model name.
    :param provider: provider name.
    :param model_dao: DAO for model models.
    :param provider_dao: DAO for provider models.
    :param endpoint_dao: DAO for endpoint models.
    :param query_dao: DAO for query models.
    :param users_dao: DAO for users models.

    :raises HTTPException: when endpoint is not found.
    """
    model_id = int(get_model(mdl_code=model, model_dao=model_dao)[0].id)
    provider_id = int(get_provider(name=provider, provider_dao=provider_dao)[0].id)
    endpoint_ids = get_endpoint(
        mdl_id=model_id,
        provider_id=provider_id,
        endpoint_dao=endpoint_dao,
        model_dao=model_dao,
        provider_dao=provider_dao,
    )
    endpoint_id = next(
        (
            int(endpoint.endpoint_id)
            for endpoint in endpoint_ids
            if endpoint.provider_id == provider_id
        ),
        None,
    )
    if endpoint_id is None:
        raise HTTPException(
            status_code=500,  # noqa: WPS432
            detail="Endpoint not found",
        )
    cost = cost_deferred_fn()
    query_model_request = QueryModelRequest(
        user_id=user_id,
        endpoint_id=endpoint_id,
        credits=cost,  # type: ignore
    )
    users_dao.recharge_credit(user_id, -cost)
    create_query_model(query_model_request, query_dao=query_dao)


def filter_request_params(arguments):
    """
    Filter argument parameters.

    :param arguments: arguments object.

    :return: dictionary of filtered parameters.
    """
    openai_params = [
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "max_tokens",
        "n",
        "presence_penalty",
        "response_format",
        "seed",
        "stop",
        "stream",
        "temperature",
        "top_p",
        "tools",
        "tool_choice",
        "user",
        "function_call",
        "functions",
        "stream",
    ]
    return {
        param: arguments.get(param)
        for param in openai_params
        if arguments.get(param) is not None
    }
