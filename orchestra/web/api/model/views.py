import datetime
from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.model_dao import ModelDAO
from orchestra.db.models.orchestra_models import Model
from orchestra.web.api.model.schema import ModelRequest, ModelResponse

router = APIRouter()


@router.get("/", response_model=List[ModelResponse])
async def get_models(
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
    return await model_dao.get_all_models(limit=limit, offset=offset)


@router.put("/")
async def create_model(
    new_model_object: ModelRequest,
    model_dao: ModelDAO = Depends(),
) -> None:
    """
    Creates model model in the database.

    :param new_model_object: new model model item.
    :param model_dao: DAO for model models.
    """
    uploaded_at = datetime.datetime.now()
    await model_dao.create_model(
        mdl_code=new_model_object.mdl_code,
        user_id=new_model_object.user_id,
        uploaded_at=uploaded_at,
        task=new_model_object.task,
        description=new_model_object.description,
        license=new_model_object.license,
        input_args_format=new_model_object.input_args_format,
        output_format=new_model_object.output_format,
        custom_fields=new_model_object.custom_fields,
    )
