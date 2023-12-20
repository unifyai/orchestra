import datetime
from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.query_dao import QueryDAO
from orchestra.db.models.orchestra_models import Query
from orchestra.web.api.query.schema import QueryModelRequest, QueryModelResponse

router = APIRouter()


@router.get("/", response_model=List[QueryModelResponse])
async def get_query_models(
    limit: int = 10,
    offset: int = 0,
    query_dao: QueryDAO = Depends(),
) -> List[Query]:
    """
    Retrieve all query objects from the database.

    :param limit: limit of query objects, defaults to 10.
    :param offset: offset of query objects, defaults to 0.
    :param query_dao: DAO for query models.
    :return: list of query objects from database.
    """
    return await query_dao.get_all_queries(limit=limit, offset=offset)


@router.put("/")
async def create_query_model(
    new_query_object: QueryModelRequest,
    query_dao: QueryDAO = Depends(),
) -> None:
    """
    Creates query model in the database.

    :param new_query_object: new query model item.
    :param query_dao: DAO for query models.
    """
    at = datetime.datetime.now()
    await query_dao.create_query(
        user_id=new_query_object.user_id,
        at=at,
        endpoint_id=new_query_object.endpoint_id,
        credits=new_query_object.credits,
    )
