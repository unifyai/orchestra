import json
from typing import Callable, Dict, List, Optional
import logging

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
from orchestra.web.api.utils.helpers import recharge_and_generate_invoice
from orchestra.web.api.utils.http_responses import internal_endpoint_not_found


def db_operations(  # noqa: WPS211, WPS217, WPS210
    user_id: str,
    cost: float,
    model: str,
    provider: str,
    prompt: List[Dict[str, str]],
    model_dao: ModelDAO,
    provider_dao: ProviderDAO,
    endpoint_dao: EndpointDAO,
    query_dao: QueryDAO,
    users_dao: UsersDAO,
    signature: Optional[str],
    used_router: Optional[bool],
):
    """
    Perform database operations.

    :param user_id: user id.
    :param cost: cost of the operation.
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
        raise internal_endpoint_not_found
    query_model_request = QueryModelRequest(
        user_id=user_id,
        endpoint_id=endpoint_id,
        credits=cost,  # type: ignore
        prompt=json.dumps(prompt),
        signature=signature,
        used_router=used_router,
    )
    users_dao.recharge_credit(user_id, -cost)
    create_query_model(query_model_request, query_dao=query_dao)

    user = users_dao.get_user_with_id(user_id)
    if (
        user.autorecharge
        and user.credits <= user.autorecharge_threshold
        and user.autorecharge_qty > 0
    ):
        recharge_and_generate_invoice(user, users_dao)
