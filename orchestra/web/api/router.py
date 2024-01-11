from fastapi import Depends
from fastapi.routing import APIRouter

from orchestra.web.api import (  # noqa: WPS235
    admin,
    chat_completion,
    endpoint,
    inference,
    license,
    metric,
    modality,
    model,
    monitoring,
    provider,
    task,
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
api_router.include_router(endpoint.router, prefix="/endpoint", tags=["endpoint"])
api_router.include_router(license.router, tags=["license"])
api_router.include_router(metric.router, tags=["metric"])
api_router.include_router(modality.router, tags=["modality"])
api_router.include_router(model.router, tags=["model"])
api_router.include_router(provider.router, tags=["provider"])
api_router.include_router(task.router, tags=["task"])
api_router.include_router(
    inference.router,
    tags=["inference"],
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
    dependencies=ADMIN_AUTH,
)
api_router.include_router(monitoring.router)
