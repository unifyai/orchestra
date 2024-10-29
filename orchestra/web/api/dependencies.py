import logging
import os

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orchestra.db.dao.api_key_dao import ApiKeyDAO
from orchestra.web.api.utils.http_responses import admin_not_authorized, invalid_api_key

security = HTTPBearer()
logger = logging.getLogger(__name__)


def auth_api_key(
    request_fastapi: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    api_key_dao: ApiKeyDAO = Depends(),
) -> None:
    """
    Authenticate an API key.

    :param request_fastapi: FastAPI request object.
    :param credentials: current authorisation credentials.
    :raises HTTPException: when api key is invalid.
    """
    apikey = credentials.credentials

    db_response = api_key_dao.get_user_id_and_mail(apikey)
    if db_response:
        request_fastapi.state.user_id = db_response[0][0]
        request_fastapi.state.user_email = db_response[0][1]
        return
    raise invalid_api_key


def auth_admin_key(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
    """
    Authenticate an admin key.

    :param credentials: current authorisation credentials.
    :raises HTTPException: when admin key is invalid.
    """
    admin_key = credentials.credentials
    if admin_key != os.environ["ORCHESTRA_ADMIN_KEY"]:
        raise admin_not_authorized
