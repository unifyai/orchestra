import datetime
import time
from typing import List, Optional

from fastapi import APIRouter, Query
from fastapi.param_functions import Depends

from orchestra.db.dao.endpoint_dao import EndpointDAO
from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.models.orchestra_models import Model
from orchestra.web.api.model.schema import ModelResponse

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
    uploaded_at: Optional[datetime.datetime] = None,
    task: Optional[str] = None,
    active: Optional[bool] = None,
    model_dao: ModelDAO = Depends(),
) -> List[Model]:
    """
    Retrieve specific model object from the database.
    \f
    :param id: id of model instance.
    :param mdl_code: mdl_code of model instance.
    :param uploaded_at: uploaded_at of model instance.
    :param task: task of model instance.
    :param active: is model instance active.
    :param model_dao: DAO for model models.
    :return: list of model objects from database.
    """
    return model_dao.filter(
        id=id,
        mdl_code=mdl_code,
        uploaded_at=uploaded_at,
        task=task,
        active=active,
    )
