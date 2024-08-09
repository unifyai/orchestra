import os

from fastapi import Depends
from fastapi.routing import APIRouter

from orchestra.web.api import (  # noqa: WPS235
    admin,
    benchmarks,
    chat_completion,
    custom_endpoint,
    dataset,
    dataset_evaluation,
    docs,
    endpoint,
    eval_batch,
    inference,
    model,
    monitoring,
    provider,
    routing,
    users,
)
from orchestra.web.api.dependencies import auth_admin_key, auth_api_key

API_KEY_AUTH = [Depends(auth_api_key)]
ADMIN_AUTH = [Depends(auth_admin_key)] if not os.environ.get("ON_PREM") else None

api_router = APIRouter()
api_router.include_router(
    users.router,
    tags=["User"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    model.router,
    tags=["Model and Endpoints"],
    include_in_schema=True,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    inference.router,
    tags=["Querying LLMs"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    chat_completion.router,
    tags=["Querying LLMs"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    custom_endpoint.router,
    tags=["Custom Endpoints and API keys"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    dataset.router,
    tags=["Dataset"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    dataset_evaluation.router,
    tags=["Dataset Evaluation"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    routing.router,
    tags=["Routing"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    endpoint.router,
    tags=["Model and Endpoints"],
    include_in_schema=True,
    dependencies=API_KEY_AUTH,
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
    admin.router,
    prefix="/admin",
    tags=["admin"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(monitoring.router)
api_router.include_router(docs.router)
api_router.include_router(
    benchmarks.router,
    tags=["benchmarks"],
    dependencies=API_KEY_AUTH,
)
