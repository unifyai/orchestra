from fastapi import APIRouter

import orchestra.web.api.interface.tab_views as _tab_views
from orchestra.web.api.interface.interface_views import router as interface_router
from orchestra.web.api.interface.tab_views import router as tab_router
from orchestra.web.api.interface.tile_views import router as tile_router

# Main router with no prefix but with interface tag
router = APIRouter(tags=["interface"])

# Include new granular endpoints at their respective paths
router.include_router(interface_router)  # Will be at /interfaces
router.include_router(tab_router)  # Will be at /tab
router.include_router(tile_router)  # Will be at /tile

# Aliases for tab endpoints without trailing slash to match client expectations

router.add_api_route(
    "/tab",
    _tab_views.create_tab,
    methods=["POST"],
    tags=["tab"],
)

router.add_api_route(
    "/tab",
    _tab_views.update_tab,
    methods=["PUT"],
    tags=["tab"],
)
