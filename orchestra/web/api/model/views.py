from typing import List

from fastapi import APIRouter, Query, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.custom_endpoint_dao import CustomEndpointDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.models.orchestra_models import Model
from orchestra.web.api.utils.on_prem import handle_on_prem

router = APIRouter()


@router.get(
    "/models",
    response_model=List[str],
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": ["model_a", "model_b", "..."],
                },
            },
        },
    },
)
@handle_on_prem(endpoint="/models", method="get")
def get_models(
    fastapi_request: Request,
    provider: str = Query(
        default=None,
        description="Provider to get available models from.",
        example="openai",
    ),
    endpoint_dao: EndpointDAO = Depends(),
    custom_endpoint_dao: CustomEndpointDAO = Depends(),
) -> List[Model]:
    """
    Lists available models. If a provider is specified, returns the models that the provider supports.
    """
    user_id = fastapi_request.state.user_id

    raw = endpoint_dao.get_endpoints_of((None,), (provider,))
    models = list(set([r.Model.mdl_code for r in raw]))

    private_endpoints_raw = custom_endpoint_dao.get_user_endpoints(user_id=user_id)
    private_models = [e[0] for e in private_endpoints_raw]

    if provider and provider != "custom":
        private_models = []

    return sorted(models + private_models)
