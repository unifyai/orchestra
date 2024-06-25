import datetime
import json
from typing import Dict, List, Optional

from google.cloud import pubsub_v1

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

# from google.oauth2 import service_account


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
    # TODO: Make sure this sends msgs correctly in staging/local
    # TODO: change telemetry during CI tests
    # key_path = "./archive/pubsub_2_clickhouse.json"
    # credentials = service_account.Credentials.from_service_account_file(key_path)
    # publisher = pubsub_v1.PublisherClient(credentials=credentials)
    publisher = pubsub_v1.PublisherClient()
    topic_name = "projects/saas-368716/topics/orchestra-telemetry"

    req_tokens = usage.get("prompt_tokens", 0)
    resp_tokens = usage.get("completion_tokens", 0)

    msg = json.dumps(
        {
            "user_id": user_id,
            "secondary_user_id": secondary_user_id,
            "response_id": "0",
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model": model,
            "provider": provider,
            "router": router,
            "group_id": 0,
            "processing_time": str(int(processing_time)),
            "req_tokens": str(req_tokens),
            "resp_tokens": str(resp_tokens),
            "signature": signature,
            "prompt": prompt,
        },
    ).encode()

    future = publisher.publish(topic_name, msg)
    future.result()


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
    secondary_user_id: Optional[str] = None,
    signature: Optional[str] = "",
    used_router: Optional[bool] = None,
    router: Optional[str] = None,
    processing_time: Optional[float] = 0,
    usage: Optional[Dict] = None,
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
        router=router,
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

    telemetry_to_pub_sub(
        user_id,
        secondary_user_id,
        model,
        provider,
        router,
        processing_time,
        usage,
        signature,
        json.dumps(prompt),
    )
