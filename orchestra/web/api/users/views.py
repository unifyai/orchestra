from typing import Union

from fastapi import APIRouter, Request
from fastapi.param_functions import Depends

from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.models.orchestra_models import Users
from orchestra.web.api.users.schema import CreditsResponse

router = APIRouter()


@router.get("/get_credits", response_model=Union[CreditsResponse, None])
async def get_credits(
    request_fastapi: Request,
    users_dao: UsersDAO = Depends(),
) -> Union[Users, None]:
    """
    Retrieve all credits based on user id from the database.

    :param request_fastapi: FastAPI request object.
    :param users_dao: DAO for users models.
    :return: user instance with credits from database.
    """
    user = await users_dao.filter(id=request_fastapi.state.user_id)
    return user[0] if user else None
