from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from requests import request  # type: ignore

security = HTTPBearer()


async def _auth_api_key(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
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
