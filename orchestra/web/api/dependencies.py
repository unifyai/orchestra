import os

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from requests import request  # type: ignore

security = HTTPBearer()


async def auth_api_key(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
    """
    Authenticate an API key.

    :param credentials: current authorisation credentials.
    :raises HTTPException: when api key is invalid.
    """
    apikey = credentials.credentials
    auth_ret = request(
        "GET",
        f"https://cloud-db-gateway-94jg94af.ew.gateway.dev/apikey/{apikey}",
        headers={},
    )
    if auth_ret.status_code != 200:  # noqa: WPS432
        raise HTTPException(
            status_code=404,  # noqa: WPS432
            detail="api key is not valid.",
        )


async def auth_admin_key(
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
