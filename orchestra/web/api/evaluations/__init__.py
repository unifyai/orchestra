"""Evaluations API."""

from orchestra.web.api.evaluations.views import admin_router, router

__all__ = ["router", "admin_router"]
