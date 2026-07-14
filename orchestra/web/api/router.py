from fastapi import Depends
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRouter

from orchestra.services.universal_unity_discord import get_universal_unity_discord_bot
from orchestra.settings import settings
from orchestra.web.api import (  # noqa: WPS235
    admin,
    api_keys,
    auth,
    billing,
    context,
    credits,
    integrations,
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
from orchestra.web.api.assistant import router as assistant_router
from orchestra.web.api.context.views import admin_router as context_admin_router
from orchestra.web.api.dashboard.views import admin_router as dashboard_admin_router
from orchestra.web.api.dashboard.views import router as dashboard_router
from orchestra.web.api.dependencies import (
    auth_admin_key,
    auth_api_key,
    check_account_not_frozen,
)
from orchestra.web.api.desktop import router as desktop_router
from orchestra.web.api.discord import admin_router as discord_admin_router
from orchestra.web.api.email import admin_router as email_admin_router
from orchestra.web.api.log.views import admin_router as log_admin_router
from orchestra.web.api.messages import admin_router as messages_admin_router
from orchestra.web.api.messages import router as messages_router
from orchestra.web.api.ms_teams_bot import admin_router as ms_teams_bot_admin_router
from orchestra.web.api.org_chat import admin_router as org_chat_admin_router
from orchestra.web.api.org_chat import router as org_chat_router
from orchestra.web.api.organization import admin_router as organization_admin_router
from orchestra.web.api.phone import admin_router as phone_admin_router
from orchestra.web.api.plot.views import admin_router as plot_admin_router
from orchestra.web.api.plot.views import router as plot_router
from orchestra.web.api.project.views import admin_router as project_admin_router
from orchestra.web.api.slack import admin_router as slack_admin_router
from orchestra.web.api.table_view.views import admin_router as table_view_admin_router
from orchestra.web.api.table_view.views import router as table_view_router
from orchestra.web.api.tasks import router as tasks_router
from orchestra.web.api.utils.assistant_infra import fetch_comms_features
from orchestra.web.api.webhooks import provider_triggers as provider_trigger_webhooks
from orchestra.web.api.webhooks import stripe as stripe_webhooks
from orchestra.web.api.whatsapp import admin_router as whatsapp_admin_router
from orchestra.web.api.whatsapp import router as whatsapp_router

API_KEY_AUTH = [
    Depends(auth_api_key),
    Depends(check_account_not_frozen),
]
ADMIN_AUTH = [Depends(auth_admin_key)]

groupings = {
    "Assistants": [
        "Assistant Management",
        "Messages",
        "Tasks",
        "Voices",
        "Media",
    ],
    "Projects": [
        "Projects",
        "Contexts",
        "Logs",
    ],
    "Account": [
        "User",
        "API Keys",
        "Credits",
        "Billing",
    ],
    "Organizations": [
        "Organizations",
        "Roles & Permissions",
        "Teams & Resource Access",
    ],
}

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
    tags=["User"],
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
    include_in_schema=False,
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
    dashboard_admin_router,
    prefix="/admin",
    tags=["Dashboards"],
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
api_router.include_router(
    whatsapp_admin_router,
    prefix="/admin",
    tags=["WhatsApp"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    whatsapp_router,
    tags=["WhatsApp"],
    include_in_schema=False,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    email_admin_router,
    prefix="/admin",
    tags=["Email"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    phone_admin_router,
    prefix="/admin",
    tags=["Phone"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    discord_admin_router,
    prefix="/admin",
    tags=["Discord"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    slack_admin_router,
    prefix="/admin",
    tags=["Slack"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    ms_teams_bot_admin_router,
    prefix="/admin",
    tags=["MS Teams Bot"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    integrations.admin_router,
    prefix="/admin",
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
# API_KEY_AUTH endpoints

api_router.include_router(
    assistant_router,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    desktop_router,
    tags=["Desktops"],
    include_in_schema=False,
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
    storage.public_router,
    tags=["Storage"],
)
api_router.include_router(
    storage.router,
    tags=["Storage"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    dashboard_router,
    tags=["Dashboards"],
    include_in_schema=False,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    plot_router,
    tags=["Plots"],
    include_in_schema=False,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    table_view_router,
    tags=["Table Views"],
    include_in_schema=False,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    interface.router,
    tags=["Configs"],
    include_in_schema=False,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    integrations.router,
    dependencies=API_KEY_AUTH,
)

# Account

api_router.include_router(
    credits.router,
    tags=["Credits"],
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    billing.router,
    tags=["Billing"],
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
    org_chat_router,
    tags=["Org Chat"],
    include_in_schema=False,
    dependencies=API_KEY_AUTH,
)
api_router.include_router(
    org_chat_admin_router,
    prefix="/admin",
    tags=["Org Chat"],
    include_in_schema=False,
    dependencies=ADMIN_AUTH,
)
api_router.include_router(
    api_keys.router,
    tags=["API Keys"],
    dependencies=API_KEY_AUTH,
)

# Messages

api_router.include_router(
    messages_router,
    tags=["Messages"],
    dependencies=API_KEY_AUTH,
)

api_router.include_router(
    tasks_router,
    tags=["Tasks"],
    dependencies=API_KEY_AUTH,
)

# NO AUTH

api_router.include_router(stripe_webhooks.router)
api_router.include_router(provider_trigger_webhooks.router)
# White-label OAuth redirect proxy (provider browsers hit this with no auth).
api_router.include_router(integrations.public_router)


# Simple system endpoints (no auth required)
@api_router.get("/health", include_in_schema=False)
def health_check() -> None:
    """Health check endpoint. Returns 200 if the service is healthy."""


@api_router.get("/features", tags=["System"], summary="Deployment capability flags")
async def get_features() -> dict[str, bool]:
    """Capability flags derived from this deployment's configured credentials.

    Cross-service features are owned by whichever service holds the
    authoritative credentials. Console (and other consumers) read these flags
    rather than re-deriving them from their own partial env, so a feature is
    only surfaced when the owning service can actually fulfil it. Phone/WhatsApp
    credentials live in the communication layer, so we fold in its ``/features``
    probe; Discord is Orchestra-owned and derived directly. No auth: the response
    carries no secrets, only on/off capability bits.
    """
    channels = await fetch_comms_features()
    return {
        # Manual-top-up deployments (staging) surface the full billing UI even
        # without Stripe configured, since credits are replenished for free.
        "billing": settings.billing_enabled or settings.manual_topup,
        "manual_topup": settings.manual_topup,
        "account_reset": settings.account_reset,
        "workspace_google": settings.workspace_google_enabled,
        "workspace_microsoft": settings.workspace_microsoft_enabled,
        # Phone / WhatsApp credentials (Twilio) live in the communication layer,
        # so those flags come from its probe. Absent keys mean the comms layer is
        # unreachable or the channel isn't configured; either way the channel is
        # treated as unavailable downstream.
        "contact_phone": bool(channels.get("phone", False)),
        "contact_whatsapp": bool(channels.get("whatsapp", False)),
        # Discord is Orchestra-owned: the coordinator bot ID/token are configured
        # via Secret Manager on Orchestra only and pulled down by the gateway's
        # shared-pool sync, so availability is derived here rather than probed.
        "contact_discord": bool(get_universal_unity_discord_bot()),
        "managed_desktop": bool(channels.get("managed_desktop", False)),
    }


@api_router.get("/docs", include_in_schema=False)
def redirect_docs():
    """Redirect to API documentation."""
    return RedirectResponse(url="https://docs.unify.ai/api-reference")
