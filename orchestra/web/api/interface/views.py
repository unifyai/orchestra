from fastapi import APIRouter

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
