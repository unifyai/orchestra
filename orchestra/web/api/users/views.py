from typing import List

from fastapi import APIRouter, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.models.orchestra_models import Users
from orchestra.web.api.users.schema import CreditsResponse, UsersModelResponse

router = APIRouter()


@router.get("/get_all_users", response_model=List[UsersModelResponse])
async def get_users_models(
    limit: int = 10,
    offset: int = 0,
    users_dao: UsersDAO = Depends(),
) -> List[Users]:
    """
    Retrieve all users objects from the database.

    :param limit: limit of users objects, defaults to 10.
    :param offset: offset of users objects, defaults to 0.
    :param users_dao: DAO for users models.
    :return: list of users objects from database.
    """
    return await users_dao.get_all_users(limit=limit, offset=offset)


@router.get("/get_user", response_model=List[UsersModelResponse])
async def get_user(
    id: str,  # noqa: WPS125
    users_dao: UsersDAO = Depends(),
) -> List[Users]:
    """
    Retrieve specific users object from the database.

    :param id: id of users instance.
    :param users_dao: DAO for users models.
    :return: list of users objects from database.
    """
    return await users_dao.filter(id=id)


@router.get("/get_credits", response_model=List[CreditsResponse])
async def get_credits(
    request_fastapi: Request,
    users_dao: UsersDAO = Depends(),
) -> List[Users]:
    """
    Retrieve all credits based on user id from the database.

    :param request_fastapi: FastAPI request object.
    :param users_dao: DAO for users models.
    :return: user instance with credits from database.
    """
    return await users_dao.filter(id=request_fastapi.state.user_id)
