from typing import List

from fastapi import APIRouter
from fastapi.param_functions import Depends

from orchestra.db.dao.users_dao import UsersDAO
from orchestra.db.models.orchestra_models import Users
from orchestra.web.api.credits.schema import CreditsResponse

router = APIRouter()


@router.get("/get_credits", response_model=List[CreditsResponse])
async def get_credits(
    id: str = "",  # noqa: WPS125
    users_dao: UsersDAO = Depends(),
) -> List[Users]:
    """
    Retrieve all credits based on user id from the database.

    :param id: id of the user.
    :param users_dao: DAO for users models.
    :return: user instance with credits from database.
    """
    return await users_dao.filter(id=id)
