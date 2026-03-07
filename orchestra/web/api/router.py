import os

from fastapi import Depends
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRouter

from orchestra.web.api import (  # noqa: WPS235
    admin,
    api_keys,
    auth,
    context,
    credits,
    interface,
    log,
    organization,
    project,
    roles,
    storage,
    teams,
    users,
)
from orchestra.web.api.assistant import admin_router as assistant_admin_router
from orchestra.web.api.assistant import demo_router as assistant_demo_router
from orchestra.web.api.assistant import router as assistant_router
from orchestra.web.api.context.views import admin_router as context_admin_router
from orchestra.web.api.dependencies import (
    auth_admin_key,
    auth_api_key,
    check_account_not_frozen,
)
from orchestra.web.api.desktop import router as desktop_router
from orchestra.web.api.log.views import admin_router as log_admin_router
from orchestra.web.api.messages import admin_router as messages_admin_router
from orchestra.web.api.messages import router as messages_router
from orchestra.web.api.organization import admin_router as organization_admin_router
from orchestra.web.api.plot.views import admin_router as plot_admin_router
from orchestra.web.api.plot.views import router as plot_router
from orchestra.web.api.project.views import admin_router as project_admin_router
from orchestra.web.api.table_view.views import admin_router as table_view_admin_router
from orchestra.web.api.table_view.views import router as table_view_router
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
    auth.admin_router,
    prefix="/admin",
    tags=["Auth"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    auth.router,
    tags=["Auth"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    context_admin_router,
    prefix="/admin",
    tags=["Contexts"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
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
api_router.include_router(
    table_view_admin_router,
    prefix="/admin",
    tags=["Table Views"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    organization_admin_router,
    prefix="/admin",
    tags=["Organizations"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    messages_admin_router,
    prefix="/admin",
    tags=["Messages"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
# API_KEY_AUTH endpoints

api_router.include_router(
    assistant_router,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    assistant_demo_router,
    prefix="/demo",
    tags=["Demo Assistants"],
    include_in_schema=False,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    desktop_router,
    tags=["Desktops"],
    dependencies=API_KEY_AUTH,
)
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
    table_view_router,
    tags=["Table Views"],
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

# Messages

api_router.include_router(
    messages_router,
    tags=["Messages"],
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
