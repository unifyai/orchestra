import os

from fastapi import Depends
from fastapi.routing import APIRouter

from orchestra.web.api import (  # noqa: WPS235
    admin,
    context,
    credits,
    custom_api_keys,
    custom_endpoints,
    dashboard_view,
    docs,
    endpoint_metrics,
    interface,
    llm_queries,
    log,
    logging,
    monitoring,
    organization,
    project,
    provider,
    supported_endpoints,
    users,
)
from orchestra.web.api.assistant import admin_router as assistant_admin_router
from orchestra.web.api.assistant import router as assistant_router
from orchestra.web.api.dependencies import (
    auth_admin_key,
    auth_api_key,
    check_account_not_frozen,
)
from orchestra.web.api.log.views import admin_router as log_admin_router
from orchestra.web.api.project.views import admin_router as project_admin_router
from orchestra.web.api.webhooks import stripe as stripe_webhooks

API_KEY_AUTH = [
    Depends(auth_api_key),
    Depends(check_account_not_frozen),
]
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
api_router.include_router(
    dashboard_view.router,
    prefix="/admin",
    tags=["Users"],
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
api_router.include_router(
    users.router,
    tags=["Query Logging"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    log_admin_router,
    prefix="/admin",
    tags=["Logs"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    project_admin_router,
    prefix="/admin",
    tags=["Projects"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    assistant_admin_router,
    prefix="/admin",
    tags=["Assistants"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
# API_KEY_AUTH endpoints

groupings = {
    "Assistants": [
        "Assistant Management",
        "Voices",
        "Media",
        "Recordings",
    ],
    "Universal API": [
        "Supported Endpoints",
        "LLM Queries",
        "Usage",
        "Custom Endpoints",
        "Custom API keys",
        "Endpoint Metrics",
    ],
    "Logging": [
        "Datasets",
        "Dataset Artifacts",
        "Projects",
        "Project Artifacts",
        "Contexts",
        "Context Artifacts",
        "Logs",
        "Configs",
    ],
    "Account": [
        "Credits",
        "Query Logging",
        "Organizations",
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
    tags=["Usage"],
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
api_router.include_router(
    assistant_router,
    dependencies=API_KEY_AUTH,
)

# Benchmarking)
api_router.include_router(
    context.router,
    tags=["Contexts"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    project.router,
    tags=["Projects"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    log.router,
    tags=["Logs"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    interface.router,
    tags=["Configs"],
    dependencies=API_KEY_AUTH,
)

# Account

api_router.include_router(
    credits.router,
    tags=["Credits"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    organization.router,
    tags=["Organizations"],
    dependencies=API_KEY_AUTH,
)

# NO AUTH

api_router.include_router(monitoring.router)
api_router.include_router(docs.router)
api_router.include_router(stripe_webhooks.router)
