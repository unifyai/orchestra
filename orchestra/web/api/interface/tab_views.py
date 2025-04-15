from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.db.dao.tile_dao import TileDAO
from orchestra.web.api.interface.schema import (
    TabSchema,
    CreateTabRequest,
    UpdateTabRequest,
)

router = APIRouter(prefix="/tab", tags=["tab"])


def _create_tab_response(tab, tiles=None) -> TabSchema:
    """Helper function to convert a tab entity to a TabSchema with optional tiles."""
    
    tile_list = []
    if tiles:
        # Format tiles into TileSchema objects
        for tile in tiles:
            # This would call the equivalent function in tile_views.py
            from orchestra.web.api.interface.tile_views import _create_tile_response
            tile_list.append(_create_tile_response(tile))
    
    return TabSchema(
        id=tab.id,
        interface_id=tab.interface_id,
        name=tab.name,
        visible=tab.visible,
        active=tab.active,
        order=tab.order,
        global_context=tab.global_context,
        color=tab.color,
        is_checkpoint=tab.is_checkpoint,
        tiles=tile_list,
        created_at=tab.created_at.isoformat() if tab.created_at else None,
        updated_at=tab.updated_at.isoformat() if tab.updated_at else None,
    )


@router.post(
    "/",
    response_model=TabSchema,
    status_code=201,
    responses={
        201: {"description": "Tab created successfully"},
        404: {"description": "Interface not found"},
        409: {"description": "Tab with this name already exists for this interface"},
    },
)
def create_tab(
    request_fastapi: Request,
    request: CreateTabRequest,
    checkpoint: bool = Query(False, description="Whether to create a checkpoint tab (manual save)"),
    tab_dao: TabDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
):
    """Create a new tab."""
    # Check if interface exists
    interface = interface_dao.get_interface(request.interface_id)
    if not interface:
        raise HTTPException(
            status_code=404,
            detail=f"Interface {request.interface_id} not found.",
        )
    
    # Check if tab already exists with the same name in this interface
    existing = tab_dao.get_tab_by_name(
        interface_id=request.interface_id,
        name=request.name,
        is_checkpoint=checkpoint
    )
    
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Tab with name {request.name} already exists for this interface.",
        )
    
    # TODO: Check if user has access to create tabs
    
    # Create the tab
    tab = tab_dao.create_tab(
        interface_id=request.interface_id,
        name=request.name,
        visible=request.visible,
        active=request.active,
        order=request.order,
        global_context=request.global_context,
        color=request.color,
        is_checkpoint=checkpoint,
    )
    
    return _create_tab_response(tab)


@router.get(
    "/{tab_id}",
    response_model=TabSchema,
    responses={
        200: {"description": "Tab details retrieved successfully"},
        404: {"description": "Tab not found"},
    },
)
def get_tab(
    request_fastapi: Request,
    tab_id: str = Path(..., description="The ID of the tab to retrieve"),
    checkpoint: bool = Query(False, description="Whether to get a checkpoint tab (manual save)"),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Get a specific tab by ID."""
    tab = tab_dao.get_tab(tab_id)
    
    if not tab:
        raise HTTPException(status_code=404, detail=f"Tab {tab_id} not found.")
    
    # If checkpoint is requested but tab is not a checkpoint, get the latest checkpoint
    if checkpoint and not tab.is_checkpoint:
        checkpoint_tab = tab_dao.get_latest_checkpoint(tab.interface_id, tab.name)
        if checkpoint_tab:
            tab = checkpoint_tab
    
    # Get all tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=tab.id, is_checkpoint=tab.is_checkpoint)
    
    return _create_tab_response(tab, tiles)


@router.get(
    "/",
    response_model=List[TabSchema],
    responses={
        200: {"description": "Tabs list retrieved successfully"},
    },
)
def list_tabs(
    request_fastapi: Request,
    interface_id: Optional[str] = Query(None, description="Filter tabs by interface ID"),
    name: Optional[str] = Query(None, description="Filter tabs by name"),
    checkpoint: bool = Query(False, description="Whether to list checkpoint tabs (manual save)"),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """List all tabs, optionally filtered by interface ID."""
    # TODO: Check if user has access to list tabs
    
    tabs = tab_dao.list_tabs(
        interface_id=interface_id,
        name=name,
        is_checkpoint=checkpoint
    )
    
    result = []
    for tab in tabs:
        # Get all tiles for this tab
        tiles = tile_dao.list_tiles_by_tab(tab_id=tab.id, is_checkpoint=tab.is_checkpoint)
        result.append(_create_tab_response(tab, tiles))
    
    return result


@router.put(
    "/{tab_id}",
    response_model=TabSchema,
    responses={
        200: {"description": "Tab updated successfully"},
        404: {"description": "Tab not found"},
    },
)
def update_tab(
    request_fastapi: Request,
    request: UpdateTabRequest,
    tab_id: str = Path(..., description="The ID of the tab to update"),
    checkpoint: bool = Query(False, description="Whether this is a checkpoint update (manual save)"),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Update a tab by ID."""
    tab = tab_dao.get_tab(tab_id)
    
    if not tab:
        raise HTTPException(status_code=404, detail=f"Tab {tab_id} not found.")
    
    # TODO: Check if user has access to this tab
    
    # Prepare update parameters
    update_params = {"id": tab_id}
    
    if request.name is not None:
        update_params["name"] = request.name
    
    if request.visible is not None:
        update_params["visible"] = request.visible
    
    if request.active is not None:
        update_params["active"] = request.active
    
    if request.order is not None:
        update_params["order"] = request.order
    
    if request.global_context is not None:
        update_params["global_context"] = request.global_context
    
    if request.color is not None:
        update_params["color"] = request.color
    
    if checkpoint:
        update_params["is_checkpoint"] = True
    
    # Update the tab
    updated = tab_dao.update_tab(**update_params)
    
    # Get all tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=updated.id, is_checkpoint=updated.is_checkpoint)
    
    return _create_tab_response(updated, tiles)


@router.post(
    "/{tab_id}/checkpoint",
    response_model=TabSchema,
    responses={
        200: {"description": "Tab checkpoint created successfully"},
        404: {"description": "Tab not found"},
    },
)
def create_tab_checkpoint(
    request_fastapi: Request,
    tab_id: str = Path(..., description="The ID of the tab to checkpoint"),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Create a manual checkpoint (save) of a tab and all its tiles."""
    # Get the current tab
    tab = tab_dao.get_tab(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail=f"Tab {tab_id} not found.")
    
    # Create a checkpoint by setting the is_checkpoint flag
    updated = tab_dao.make_checkpoint(tab_id)
    
    # Get all tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=updated.id, is_checkpoint=updated.is_checkpoint)
    
    return _create_tab_response(updated, tiles)


@router.delete(
    "/{tab_id}",
    status_code=204,
    responses={
        204: {"description": "Tab deleted successfully"},
        404: {"description": "Tab not found"},
    },
)
def delete_tab(
    request_fastapi: Request,
    tab_id: str = Path(..., description="The ID of the tab to delete"),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Delete a tab by ID."""
    tab = tab_dao.get_tab(tab_id)
    
    if not tab:
        raise HTTPException(status_code=404, detail=f"Tab {tab_id} not found.")
    
    # TODO: Check if user has access to delete this tab
    
    # First delete all tiles associated with this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=tab_id)
    for tile in tiles:
        tile_dao.delete_tile(tile.id)
    
    # Delete the tab
    success = tab_dao.delete_tab(tab_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete tab.") 