"""log API."""

from orchestra.web.api.log.federated_views import router as federated_router
from orchestra.web.api.log.views import router

router.include_router(federated_router)

__all__ = ["router"]
