import os

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from requests import request  # type: ignore
from orchestra.settings import settings

security = HTTPBearer()


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

    if auth_ret.status_code != 200:  # noqa: WPS432
        raise HTTPException(
            status_code=404,  # noqa: WPS432
            detail="Invalid API key",
        )
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
        raise HTTPException(
            status_code=403,  # noqa: WPS432
            detail="admin unauthorized.",
        )
