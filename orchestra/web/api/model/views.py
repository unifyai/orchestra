import datetime
import time
from typing import List, Optional

from fastapi import APIRouter, Query
from fastapi.param_functions import Depends

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.models.orchestra_models import Model
from orchestra.web.api.model.schema import ModelResponse
from orchestra.web.api.utils.on_prem import handle_on_prem

router = APIRouter()
public_router = APIRouter()

_model_list_cache = {}


@public_router.get(
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
def list_models(
    provider: str = Query(
        default=None,
        description="Provider to get available models from.",
    ),
    model_dao: ModelDAO = Depends(),
    endpoint_dao: EndpointDAO = Depends(),
) -> List[Model]:
    """
    Returns a list of every LLM available through the Unify API.
    \f
    :return: list of active model names from database.
    """
    if time.time() - _model_list_cache.get("ts", 0) > 3600:
        raw = endpoint_dao.get_endpoints_of(only_from=(provider,))
        ret = [r.Model.mdl_code for r in raw]
        ret.sort()
        _model_list_cache["models"] = ret
        _model_list_cache["ts"] = time.time()
    return _model_list_cache["models"]


@router.get("/get_model", response_model=List[ModelResponse])
def get_model(  # noqa: WPS211, C901
    id: Optional[int] = None,  # noqa: WPS125
    mdl_code: Optional[str] = None,
    user_id: Optional[str] = None,
    uploaded_at: Optional[datetime.datetime] = None,
    task: Optional[str] = None,
    description: Optional[str] = None,
    license: Optional[str] = None,
    active: Optional[bool] = None,
    input_args_format: Optional[str] = None,
    output_format: Optional[str] = None,
    custom_fields: Optional[str] = None,
    model_dao: ModelDAO = Depends(),
) -> List[Model]:
    """
    Retrieve specific model object from the database.
    \f
    :param id: id of model instance.
    :param mdl_code: mdl_code of model instance.
    :param user_id: user_id of model instance.
    :param uploaded_at: uploaded_at of model instance.
    :param task: task of model instance.
    :param description: description of model instance.
    :param license: license of model instance.
    :param active: is model instance active.
    :param input_args_format: input_args_format of model instance.
    :param output_format: output_format of model instance.
    :param custom_fields: custom_fields of model instance.
    :param model_dao: DAO for model models.
    :return: list of model objects from database.
    """
    return model_dao.filter(
        id=id,
        mdl_code=mdl_code,
        user_id=user_id,
        uploaded_at=uploaded_at,
        task=task,
        description=description,
        license=license,
        active=active,
        input_args_format=input_args_format,
        output_format=output_format,
        custom_fields=custom_fields,
    )
