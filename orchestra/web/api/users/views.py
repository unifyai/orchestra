from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.models.orchestra_models import Users
from orchestra.web.api.users.schema import UsersModelRequest, UsersModelResponse

router = APIRouter()


@router.get("/", response_model=List[UsersModelResponse])
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


@router.put("/")
async def create_users_model(
    new_users_object: UsersModelRequest,
    users_dao: UsersDAO = Depends(),
) -> None:
    """
    Creates users model in the database.

    :param new_users_object: new users model item.
    :param users_dao: DAO for users models.
    """
    await users_dao.create_users(id=new_users_object.id, credits=float(0))
