import os

from fastapi import Depends
from fastapi.routing import APIRouter

from orchestra.web.api import (  # noqa: WPS235
    supported_endpoints,
    llm_queries,
    logging,
    custom_endpoints,
    custom_api_keys,
    dataset,
    evaluators,
    evaluations,
    admin,
    benchmarks,
    docs,
    eval_batch,
    monitoring,
    provider,
    routing,
    credits,
)
from orchestra.web.api.dependencies import auth_admin_key, auth_api_key

API_KEY_AUTH = [Depends(auth_api_key)]
ADMIN_AUTH = [Depends(auth_admin_key)] if not os.environ.get("ON_PREM") else None

api_router = APIRouter()
api_router.include_router(
    supported_endpoints.router,
    tags=["Supported Endpoints"],
    include_in_schema=True,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    llm_queries.router,
    tags=["LLM Queries"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    logging.router,
    tags=["Logging"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    custom_endpoints.router,
    tags=["Custom Endpoints"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    custom_api_keys.router,
    tags=["Custom API keys"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    dataset.router,
    tags=["Datasets"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    evaluators.router,
    tags=["Evaluators"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    evaluations.router,
    tags=["Evaluations"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    benchmarks.router,
    tags=["Benchmarks"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    routing.router,
    tags=["Routing"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    credits.router,
    tags=["Credits"],
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
