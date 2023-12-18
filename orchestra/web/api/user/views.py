from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.models.orchestra_models import User
from orchestra.web.api.user.schema import UserModelRequest, UserModelResponse

router = APIRouter()


@router.get("/", response_model=List[UserModelResponse])
async def get_user_models(
    limit: int = 10,
    offset: int = 0,
    user_dao: UserDAO = Depends(),
) -> List[User]:
    """
    Retrieve all user objects from the database.

    :param limit: limit of user objects, defaults to 10.
    :param offset: offset of user objects, defaults to 0.
    :param user_dao: DAO for user models.
    :return: list of user objects from database.
    """
    return await user_dao.get_all_users(limit=limit, offset=offset)


@router.put("/")
async def create_user_model(
    new_user_object: UserModelRequest,
    user_dao: UserDAO = Depends(),
) -> None:
    """
    Creates user model in the database.

    :param new_user_object: new user model item.
    :param user_dao: DAO for user models.
    """
    await user_dao.create_user(id=new_user_object.id, credits=float(0))
