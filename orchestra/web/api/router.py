import os

from fastapi import Depends
from fastapi.routing import APIRouter

from orchestra.web.api import (  # noqa: WPS235
    admin,
    artifact,
    credits,
    custom_api_keys,
    custom_endpoints,
    datasets,
    datasetv2,
    default_prompt,
    docs,
    endpoint_metrics,
    evaluations,
    evaluators,
    llm_queries,
    log,
    logging,
    monitoring,
    project,
    provider,
    router_configurations,
    router_deployment,
    router_training,
    supported_endpoints,
    users,
)
from orchestra.web.api.dependencies import auth_admin_key, auth_api_key

API_KEY_AUTH = [Depends(auth_api_key)]
ADMIN_AUTH = [Depends(auth_admin_key)] if not os.environ.get("ON_PREM") else None

api_router = APIRouter()

# ADMIN_AUTH endpoints

api_router.include_router(
    admin.router,
    prefix="/admin",
    tags=["admin"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(  # CLEANUP: Delete this
    evaluations.admin_router,
    tags=["Evaluations"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(  # CLEANUP: Delete this? Check if it's being used
    provider.router,
    prefix="/admin",
    tags=["provider"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    users.admin_router,
    prefix="/admin",
    tags=["Users"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)

# API_KEY_AUTH endpoints

groupings = {
    "Universal API": [
        "Supported Endpoints",
        "LLM Queries",
        "Logging",
        "Custom Endpoints",
        "Custom API keys",
        "Endpoint Metrics",
    ],
    "Benchmarking": [
        "DatasetsV2",
        "Projects",
        "Artifacts",
        "Evals",
    ],
    "Routing": [
        "Router Training",
        "Router Deployment",
        "Router Configurations",
    ],
    "Account": [
        "Credits",
    ],
}

# Universal API

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
    custom_api_keys.router,
    tags=["Custom API keys"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    custom_endpoints.router,
    tags=["Custom Endpoints"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    endpoint_metrics.router,
    tags=["Endpoint Metrics"],
    dependencies=API_KEY_AUTH,
)

# Benchmarking

api_router.include_router(  # CLEANUP: Delete this
    datasets.router,
    tags=["Datasets"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(  # TODO: Change this to dataset
    datasetv2.router,
    tags=["DatasetsV2"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    project.router,
    tags=["Projects"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    artifact.router,
    tags=["Artifacts"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    log.router,
    tags=["Evals"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(  # CLEANUP: Delete this
    evaluators.router,
    tags=["Evaluators"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(  # CLEANUP: Delete this
    default_prompt.router,
    tags=["Default Prompts"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(  # CLEANUP: Delete this
    evaluations.router,
    tags=["Evaluations"],
    dependencies=API_KEY_AUTH,
)

# Routing

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

# Account

api_router.include_router(
    credits.router,
    tags=["Credits"],
    dependencies=API_KEY_AUTH,
)

# NO AUTH

api_router.include_router(monitoring.router)
api_router.include_router(docs.router)
