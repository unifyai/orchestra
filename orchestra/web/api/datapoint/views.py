from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.datapoint_dao import DatapointDAO
from orchestra.db.models.orchestra_models import Datapoint
from orchestra.web.api.datapoint.schema import (
    DatapointModelRequest,
    DatapointModelResponse,
)

router = APIRouter()


@router.get("/", response_model=List[DatapointModelResponse])
async def get_datapoint_models(
    limit: int = 10,
    offset: int = 0,
    datapoint_dao: DatapointDAO = Depends(),
) -> List[Datapoint]:
    """
    Retrieve all datapoint objects from the database.

    :param limit: limit of datapoint objects, defaults to 10.
    :param offset: offset of datapoint objects, defaults to 0.
    :param datapoint_dao: DAO for datapoint models.
    :return: list of datapoint objects from database.
    """
    return await datapoint_dao.get_all_datapoints(limit=limit, offset=offset)


@router.put("/")
async def create_datapoint_model(
    new_datapoint_object: DatapointModelRequest,
    datapoint_dao: DatapointDAO = Depends(),
) -> None:
    """
    Creates datapoint model in the database.

    :param new_datapoint_object: new datapoint model item.
    :param datapoint_dao: DAO for datapoint models.
    """
    await datapoint_dao.create_datapoint(
        endpoint_id=new_datapoint_object.endpoint_id,
        measured_at=new_datapoint_object.measured_at,
        metric_name=new_datapoint_object.metric_name,
        value=new_datapoint_object.value,
    )
