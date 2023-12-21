from fastapi import Depends
from fastapi.routing import APIRouter

from orchestra.web.api import (  # noqa: WPS235
    chat_completion,
    datapoint,
    endpoint,
    license,
    metric,
    modality,
    model,
    models,
    monitoring,
    predict,
    provider,
    query,
    recharge,
    recharge_type,
    task,
    users,
)
from orchestra.web.api.dependencies import auth_api_key

AUTH = [Depends(auth_api_key)]

api_router = APIRouter()
api_router.include_router(monitoring.router)

api_router.include_router(users.router, prefix="/users", tags=["users"])

api_router.include_router(datapoint.router, prefix="/datapoint", tags=["datapoint"])
api_router.include_router(endpoint.router, prefix="/endpoint", tags=["endpoint"])
api_router.include_router(license.router, prefix="/license", tags=["license"])
api_router.include_router(metric.router, prefix="/metric", tags=["metric"])
api_router.include_router(modality.router, prefix="/modality", tags=["modality"])
api_router.include_router(model.router, prefix="/model", tags=["model"])
api_router.include_router(provider.router, prefix="/provider", tags=["provider"])
api_router.include_router(query.router, prefix="/query", tags=["query"])
api_router.include_router(recharge.router, prefix="/recharge", tags=["recharge"])
api_router.include_router(
    recharge_type.router,
    prefix="/recharge_type",
    tags=["recharge_type"],
)
api_router.include_router(task.router, prefix="/task", tags=["task"])

api_router.include_router(models.router, tags=["models"], dependencies=AUTH)
api_router.include_router(predict.router, tags=["predict"], dependencies=AUTH)
api_router.include_router(
    chat_completion.router,
    tags=["chat_completion"],
    dependencies=AUTH,
)
