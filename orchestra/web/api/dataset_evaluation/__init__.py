"""Dataset evaluation API."""

from orchestra.web.api.dataset_evaluation.views import admin_router, router

__all__ = ["router", "admin_router"]
