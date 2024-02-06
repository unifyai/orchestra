from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.dao.provider_dao import ProviderDAO
from orchestra.db.dao.query_dao import QueryDAO

from orchestra.web.api.endpoint.views import get_endpoint
from orchestra.web.api.model.views import get_model
from orchestra.web.api.provider.views import get_provider
from orchestra.web.api.query.schema import QueryModelRequest
from orchestra.web.api.query.views import create_query_model

async def db_operations(
    user_id: str,
    cost: float,
    model: str,
    provider: str,
    model_dao: ModelDAO,
    provider_dao: ProviderDAO,
    endpoint_dao: EndpointDAO,
    query_dao: QueryDAO,
):
    model_id = int((await get_model(mdl_code=model, model_dao=model_dao))[0].id)
    provider_id = int((await get_provider(name=provider, provider_dao=provider_dao))[0].id)
    endpoint_ids = await get_endpoint(
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

    query_model_request = QueryModelRequest(
        user_id=user_id,
        endpoint_id=endpoint_id,
        credits=cost,
    )
    await create_query_model(query_model_request, query_dao=query_dao)