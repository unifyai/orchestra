from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.metric_dao import MetricDAO
from orchestra.db.models.orchestra_models import Metric
from orchestra.web.api.metric.schema import MetricModelRequest, MetricModelResponse

router = APIRouter()


@router.get("/", response_model=List[MetricModelResponse])
async def get_metric_models(
    limit: int = 10,
    offset: int = 0,
    metric_dao: MetricDAO = Depends(),
) -> List[Metric]:
    """
    Retrieve all metric objects from the database.

    :param limit: limit of metric objects, defaults to 10.
    :param offset: offset of metric objects, defaults to 0.
    :param metric_dao: DAO for metric models.
    :return: list of metric objects from database.
    """
    return await metric_dao.get_all_metrics(limit=limit, offset=offset)


@router.put("/")
async def create_metric_model(
    new_metric_object: MetricModelRequest,
    metric_dao: MetricDAO = Depends(),
) -> None:
    """
    Creates metric model in the database.

    :param new_metric_object: new metric model item.
    :param metric_dao: DAO for metric models.
    """
    await metric_dao.create_metric(
        name=new_metric_object.name,
    )
