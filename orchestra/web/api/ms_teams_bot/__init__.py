"""Admin endpoints for the multi-tenant MS Teams (Bot Framework) bot."""

from orchestra.web.api.ms_teams_bot.views import admin_router

__all__ = ["admin_router"]
