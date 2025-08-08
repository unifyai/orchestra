from fastapi import APIRouter

import orchestra.web.api.interface.tab_views as _tab_views
from orchestra.web.api.interface import interface_views as _new_if_views
from orchestra.web.api.interface.interface_views import router as interface_router
from orchestra.web.api.interface.legacy_interface_views import router as legacy_router
from orchestra.web.api.interface.tab_views import router as tab_router
from orchestra.web.api.interface.tile_views import router as tile_router

# Main router with no prefix but with interface tag
router = APIRouter(tags=["interface"])

# Include legacy endpoints at /interface
router.include_router(legacy_router)

# Include new granular endpoints at their respective paths
router.include_router(interface_router)  # Will be at /interfaces
router.include_router(tab_router)  # Will be at /tab
router.include_router(tile_router)  # Will be at /tile
router.include_router(
    interface_router,
    prefix="/interface",
)  # alias for backward compat

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


# Aliases for backward compatibility to match singular /interface routes used in tests


router.add_api_route(
    "/interface",
    _new_if_views.create_interface,
    methods=["POST"],
    tags=["interface"],
)

router.add_api_route(
    "/interface",
    _new_if_views.update_interface,
    methods=["PUT"],
    tags=["interface"],
)
