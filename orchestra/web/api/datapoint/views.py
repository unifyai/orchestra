import datetime
from typing import List, Optional

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.datapoint_dao import DatapointDAO
from orchestra.db.models.orchestra_models import Datapoint
from orchestra.web.api.datapoint.schema import DatapointModelResponse

router = APIRouter()


@router.get("/get_all_datapoints", response_model=List[DatapointModelResponse])
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


@router.get("/get_datapoint", response_model=List[DatapointModelResponse])
async def get_datapoint(
    endpoint_id: Optional[int] = None,
    measured_at: Optional[datetime.datetime] = None,
    metric_name: Optional[str] = None,
    value: Optional[float] = None,
    datapoint_dao: DatapointDAO = Depends(),
) -> List[Datapoint]:
    """
    Retrieve specific datapoint object from the database.

    :param endpoint_id: endpoint_id of datapoint object.
    :param measured_at: measured_at of datapoint object.
    :param metric_name: metric_name of datapoint object.
    :param value: value of datapoint object.
    :param datapoint_dao: DAO for datapoint models.
    :return: datapoint object from database.
    """
    return await datapoint_dao.filter(
        endpoint_id=endpoint_id,
        measured_at=measured_at,
        metric_name=metric_name,
        value=value,
    )
