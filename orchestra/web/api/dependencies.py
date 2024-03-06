import logging
import os

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from requests import request  # type: ignore

from orchestra.settings import settings
from orchestra.web.api.utils.http_responses import (
    invalid_api_key,
    admin_not_authorized,
    server_error_with_digest,
)

security = HTTPBearer()
logger = logging.getLogger(__name__)


def auth_api_key(
    request_fastapi: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
    """
    Authenticate an API key.

    :param request_fastapi: FastAPI request object.
    :param credentials: current authorisation credentials.
    :raises HTTPException: when api key is invalid.
    """
    apikey = credentials.credentials
    auth_ret = request(
        "GET",
        f"{settings.cloud_db_gateway}/hubapikey/{apikey}",
        headers={},
    )

    # TODO: This may be missleading and should have a different http code
    # (db-connector issue)
    if auth_ret.status_code == 404:
        raise invalid_api_key
    elif auth_ret.status_code != 200:  # noqa: WPS432
        error, digest = server_error_with_digest(auth_ret.text)
        logger.error(f"Digest {digest}: {auth_ret.text}")
        raise error
    request_fastapi.state.user_id = auth_ret.json()["user_id"]


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
