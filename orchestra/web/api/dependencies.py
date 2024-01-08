from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from requests import request  # type: ignore

security = HTTPBearer()


async def auth_api_key(
    request_fastapi: Request,
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
    request_fastapi.state.user_id = auth_ret.json()["user_id"]
    if auth_ret.status_code != 200:  # noqa: WPS432
        raise HTTPException(
            status_code=404,  # noqa: WPS432
            detail="api key is not valid.",
        )
