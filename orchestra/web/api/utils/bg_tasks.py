import json
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.dao.users_dao import UsersDAO
from orchestra.web.api.query.schema import QueryModelRequest
from orchestra.web.api.query.views import create_query_model
from orchestra.web.api.utils.gcp import send_pubsub_msg
from orchestra.web.api.utils.helpers import recharge_and_generate_invoice
from orchestra.web.api.utils.http_responses import internal_endpoint_not_found


def telemetry_to_pub_sub(
    user_id,
    secondary_user_id,
    model,
    provider,
    router,
    processing_time,
    usage,
    signature,
    prompt,
):
    topic = "projects/saas-368716/topics/orchestra-telemetry"

    req_tokens = usage.get("prompt_tokens", 0)
    resp_tokens = usage.get("completion_tokens", 0)

    msg = {
        "user_id": user_id,
        "secondary_user_id": secondary_user_id,
        "response_id": "0",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "provider": provider,
        "router": router,
        "group_id": 0,
        "processing_time": str(int(processing_time)),
        "req_tokens": str(req_tokens),
        "resp_tokens": str(resp_tokens),
        "signature": signature,
        "prompt": prompt,
    }

    send_pubsub_msg(topic, msg)


def db_operations(  # noqa: WPS211, WPS217, WPS210
    user_id: str,
    cost: float,
    model: str,
    provider: str,
    query_body: str,
    response_body: str,
    status_code: int,
    model_dao: ModelDAO,
    provider_dao: ProviderDAO,
    endpoint_dao: EndpointDAO,
    custom_endpoint_dao: CustomEndpointDAO,
    query_dao: QueryDAO,
    users_dao: UsersDAO,
    secondary_user_id: Optional[str] = None,
    signature: Optional[str] = "",
    used_router: Optional[bool] = None,
    router: Optional[str] = None,
    processing_time: Optional[float] = 0,
    usage: Optional[Dict] = None,
    tags: Optional[list[str]] = None,
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
    if usage is None:
        usage = {}
    if secondary_user_id is None:
        secondary_user_id = ""
    if router is None:
        router = ""

    if "custom" in provider:
        endpoint_id = None
        try:
            custom_endpoint_id = int(
                custom_endpoint_dao.filter(
                    user_id=user_id,
                    name=model,
                )[0].id,
            )
        except IndexError:
            raise internal_endpoint_not_found
    else:
        model_id = int(model_dao.filter(mdl_code=model)[0].id)
        provider_id = int(provider_dao.filter(name=provider)[0].id)
        try:
            endpoint_id = int(
                endpoint_dao.filter(mdl_id=model_id, provider_id=provider_id)[0].id,
            )
            custom_endpoint_id = None
        except IndexError:
            raise internal_endpoint_not_found
    query_model_request = QueryModelRequest(
        user_id=user_id,
        model_provider_str=f"{model}@{provider}",
        endpoint_id=endpoint_id,
        custom_endpoint_id=custom_endpoint_id,
        local_endpoint_id=None,
        credits=cost,  # type: ignore
        query_body=query_body,
        response_body=response_body,
        signature=signature,
        used_router=used_router,
        router=router,
        tags=tags,
        status_code=status_code,
    )

    create_query_model(query_model_request, query_dao=query_dao)
    user = users_dao.get_user_with_id(user_id)

    if not os.environ.get("ON_PREM") and status_code == 200:
        users_dao.recharge_credit(user_id, -cost)
        if (
            user.autorecharge
            and user.credits <= user.autorecharge_threshold
            and user.autorecharge_qty > 0
        ):
            recharge_and_generate_invoice(user, users_dao)

        telemetry_to_pub_sub(
            user_id,
            secondary_user_id,
            model,
            provider,
            router,
            processing_time,
            usage,
            signature,
            json.dumps(query_body),
        )
