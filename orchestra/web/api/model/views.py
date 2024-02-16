import datetime
from typing import List, Optional

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.models.orchestra_models import Model
from orchestra.web.api.model.schema import ModelResponse

router = APIRouter()


@router.get("/models", response_model=List[ModelResponse])
def get_models(
    limit: int = 10,
    offset: int = 0,
    model_dao: ModelDAO = Depends(),
) -> List[Model]:
    """
    Retrieve all model objects from the database.

    :param limit: limit of model objects, defaults to 10.
    :param offset: offset of model objects, defaults to 0.
    :param model_dao: DAO for model models.
    :return: list of model objects from database.
    """
    return model_dao.get_all_models(limit=limit, offset=offset)


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
