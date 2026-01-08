import os

from fastapi import Depends
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRouter

from orchestra.web.api import (  # noqa: WPS235
    admin,
    api_keys,
    context,
    credits,
    interface,
    llm_queries,
    log,
    organization,
    project,
    roles,
    storage,
    supported_endpoints,
    teams,
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
from orchestra.web.api.plot.views import admin_router as plot_admin_router
from orchestra.web.api.plot.views import router as plot_router
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
api_router.include_router(
    plot_admin_router,
    prefix="/admin",
    tags=["Plots"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
# API_KEY_AUTH endpoints

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
    plot_router,
    tags=["Plots"],
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
api_router.include_router(
    roles.router,
    tags=["Roles & Permissions"],
    dependencies=API_KEY_AUTH,
)

api_router.include_router(
    teams.router,
    tags=["Teams & Resource Access"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    api_keys.router,
    tags=["API Keys"],
    dependencies=API_KEY_AUTH,
)

# Storage

api_router.include_router(
    storage.router,
    tags=["Storage"],
    dependencies=API_KEY_AUTH,
)

# NO AUTH

api_router.include_router(stripe_webhooks.router)


# Simple system endpoints (no auth required)
@api_router.get("/health", include_in_schema=False)
def health_check() -> None:
    """Health check endpoint. Returns 200 if the service is healthy."""


@api_router.get("/docs", include_in_schema=False)
def redirect_docs():
    """Redirect to API documentation."""
    return RedirectResponse(url="https://docs.unify.ai/api-reference")
