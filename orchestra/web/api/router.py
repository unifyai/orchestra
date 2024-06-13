from fastapi import Depends
from fastapi.routing import APIRouter

from orchestra.web.api import (  # noqa: WPS235
    admin,
    chat_completion,
    eval_batch,
    endpoint,
    model,
    monitoring,
    provider,
    users,
)
from orchestra.web.api.dependencies import auth_admin_key, auth_api_key

API_KEY_AUTH = [Depends(auth_api_key)]
ADMIN_AUTH = [Depends(auth_admin_key)]

api_router = APIRouter()
api_router.include_router(
    users.router,
    tags=["users"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    model.public_router,
    tags=["model"],
    include_in_schema=True,
)
api_router.include_router(
    endpoint.public_router,
    tags=["model"],
    include_in_schema=True,
)
api_router.include_router(
    model.router,
    prefix="/admin",
    tags=["model"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    endpoint.router,
    prefix="/admin",
    tags=["endpoint"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    provider.router,
    prefix="/admin",
    tags=["provider"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    eval_batch.router,
    include_in_schema=False,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    chat_completion.router,
    tags=["chat_completion"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    admin.router,
    prefix="/admin",
    tags=["admin"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(monitoring.router)
