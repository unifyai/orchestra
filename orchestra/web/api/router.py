from fastapi import Depends
from fastapi.routing import APIRouter

from orchestra.web.api import dummy, echo, monitoring
from orchestra.web.api.dependencies import auth_api_key

AUTH = [Depends(auth_api_key)]

api_router = APIRouter()
api_router.include_router(monitoring.router)
api_router.include_router(echo.router, prefix="/echo", tags=["echo"], dependencies=AUTH)
api_router.include_router(
    dummy.router,
    prefix="/dummy",
    tags=["dummy"],
    dependencies=AUTH,
)
