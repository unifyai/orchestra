import os

from fastapi import Depends
from fastapi.routing import APIRouter

from orchestra.web.api import (  # noqa: WPS235
    admin,
    credits,
    custom_api_keys,
    custom_endpoints,
    datasets,
    docs,
    efficiency_benchmarks,
    eval_batch,
    evaluations,
    evaluators,
    llm_queries,
    logging,
    monitoring,
    provider,
    router_configurations,
    router_deployment,
    router_training,
    supported_endpoints,
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
    datasets.router,
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
    efficiency_benchmarks.router,
    tags=["Efficiency Benchmarks"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    router_training.router,
    tags=["Router Training"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    router_deployment.router,
    tags=["Router Deployment"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    router_configurations.router,
    tags=["Router Configurations"],
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
