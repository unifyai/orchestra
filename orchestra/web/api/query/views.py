from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends
from sqlalchemy.ext.asyncio import AsyncSession

# Async DAOs
from orchestra.db.dao.async_query_dao import AsyncQueryDAO
from orchestra.db.dependencies import get_async_db_session
from orchestra.db.models.orchestra_models import Query
from orchestra.web.api.query.schema import QueryModelRequest, QueryModelResponse

router = APIRouter()


@router.get("/", response_model=List[QueryModelResponse])
async def get_query_models(
    limit: int = 10,
    offset: int = 0,
    session: AsyncSession = Depends(get_async_db_session),
) -> List[Query]:
    """
    Retrieve all query objects from the database.
    \f
    :param limit: limit of query objects, defaults to 10.
    :param offset: offset of query objects, defaults to 0.
    :param query_dao: DAO for query models.
    :return: list of query objects from database.
    """
    query_dao = AsyncQueryDAO(session)
    return query_dao.get_all_queries(limit=limit, offset=offset)


@router.put("/")
async def create_query_model(
    new_query_object: QueryModelRequest,
    session: AsyncSession = Depends(get_async_db_session),
) -> None:
    """
    Creates query model in the database.
    \f
    :param new_query_object: new query model item.
    :param query_dao: DAO for query models.
    """
    query_dao = AsyncQueryDAO(session)
    at = datetime.now(timezone.utc)
    query_dao.create_query(
        user_id=new_query_object.user_id,
        at=at,
        model_provider_str=new_query_object.model_provider_str,
        endpoint_id=new_query_object.endpoint_id,
        custom_endpoint_id=new_query_object.custom_endpoint_id,
        local_endpoint_id=new_query_object.local_endpoint_id,
        credits=new_query_object.credits,
        query_body=new_query_object.query_body,
        response_body=new_query_object.response_body,
        signature=new_query_object.signature,
        used_router=new_query_object.used_router,
        router=new_query_object.router,
        tags=new_query_object.tags,
        status_code=new_query_object.status_code,
    )
