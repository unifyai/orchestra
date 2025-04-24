from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.project_dao import ProjectDAO
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
        id=str(tab.id),
        interface_id=str(tab.interface_id),
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
    project_dao: ProjectDAO = Depends(),
):
    """Create a new tab."""
    # Get the interface, first by ID if provided in the request
    interface = None
    if hasattr(request, "interface_id") and request.interface_id:
        interface = interface_dao.get(request.interface_id)
    # Also support interface_name + project_id
    elif hasattr(request, "project_id") and request.project_id and hasattr(request, "interface_name") and request.interface_name:
        # First verify project exists
        project = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=request.project_id,
        )
        if not project:
            raise HTTPException(
                status_code=404,
                detail=f"Project {request.project_id} not found or you don't have access.",
            )
        # Then get interface
        interface = interface_dao.get_by_project_and_name(
            project_id=project.id,
            name=request.interface_name,
            is_checkpoint=checkpoint
        )
    
    if not interface:
        raise HTTPException(
            status_code=404,
            detail=f"Interface not found. Please provide valid interface_id or project_id+interface_name.",
        )
    
    # Check if tab already exists with the same name in this interface
    existing = tab_dao.get_tab_by_interface_and_name(
        interface_id=interface.id,
        name=request.name,
        is_checkpoint=checkpoint
    )
    
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Tab with name {request.name} already exists for this interface.",
        )
    
    # Create the tab
    tab = tab_dao.create_tab(
        interface_id=interface.id,
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
    "/",
    response_model=TabSchema,
    responses={
        200: {"description": "Tab details retrieved successfully"},
        404: {"description": "Tab not found"},
        400: {"description": "Missing required parameters"},
    },
)
def get_tab(
    request_fastapi: Request,
    interface_name: str = Query(..., description="The interface name the tab belongs to"),
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    name: str = Query(..., description="The name of the tab to retrieve"),
    checkpoint: bool = Query(False, description="Whether to get a checkpoint tab (manual save)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Get a specific tab by interface name, project ID, and tab name."""
    # Verify project exists and user has access
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project_id,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_id} not found or you don't have access.",
        )
    
    # Get interface
    interface = interface_dao.get_by_project_and_name(
        project_id=project.id,
        name=interface_name,
        is_checkpoint=checkpoint
    )
    
    if not interface:
        raise HTTPException(
            status_code=404,
            detail=f"Interface {interface_name} not found in project {project_id}.",
        )
    
    # Get tab
    tab = tab_dao.get_tab_by_interface_and_name(
        interface_id=interface.id,
        name=name,
        is_checkpoint=checkpoint
    )
    
    if not tab:
        raise HTTPException(
            status_code=404, 
            detail=f"Tab {name} not found in interface {interface_name}."
        )
    
    # If checkpoint is requested but tab is not a checkpoint, get the latest checkpoint
    if checkpoint and not tab.is_checkpoint:
        checkpoint_tab = tab_dao.get_latest_checkpoint(interface.id, name)
        if checkpoint_tab:
            tab = checkpoint_tab
    
    # Get all tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=tab.id, is_checkpoint=tab.is_checkpoint)
    
    return _create_tab_response(tab, tiles)


@router.get(
    "/list",
    response_model=List[TabSchema],
    responses={
        200: {"description": "Tabs list retrieved successfully"},
        404: {"description": "Interface or project not found"},
    },
)
def list_tabs(
    request_fastapi: Request,
    interface_name: Optional[str] = Query(None, description="The interface name to filter tabs by"),
    project_id: str = Query(..., description="The project ID to filter tabs by"),
    name: Optional[str] = Query(None, description="Filter tabs by name"),
    checkpoint: bool = Query(False, description="Whether to list checkpoint tabs (manual save)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """List all tabs, optionally filtered by interface name and project ID."""
    # Verify project exists and user has access
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project_id,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_id} not found or you don't have access.",
        )
    
    # If interface name is provided, get the interface
    interface_id = None
    if interface_name:
        interface = interface_dao.get_by_project_and_name(
            project_id=project.id,
            name=interface_name,
            is_checkpoint=checkpoint
        )
        
        if not interface:
            raise HTTPException(
                status_code=404,
                detail=f"Interface {interface_name} not found in project {project_id}.",
            )
        
        interface_id = interface.id
    
    # List all interfaces in the project if no interface name provided
    interfaces = []
    if not interface_name:
        interfaces = interface_dao.get_interfaces(project_id=project.id, is_checkpoint=checkpoint)
    else:
        interfaces = [interface]
    
    # Collect tabs from all interfaces
    result = []
    for interface in interfaces:
        # Get tabs for this interface
        tabs = tab_dao.list_tabs_by_interface(
            interface_id=interface.id,
            is_checkpoint=checkpoint
        )
        
        # Filter by name if provided
        if name:
            tabs = [tab for tab in tabs if tab.name == name]
        
        # Get tiles for each tab
        for tab in tabs:
            tiles = tile_dao.list_tiles_by_tab(tab_id=tab.id, is_checkpoint=tab.is_checkpoint)
            result.append(_create_tab_response(tab, tiles))
    
    return result


@router.put(
    "/",
    response_model=TabSchema,
    responses={
        200: {"description": "Tab updated successfully"},
        404: {"description": "Tab not found"},
        400: {"description": "Missing required parameters"},
    },
)
def update_tab(
    request_fastapi: Request,
    request: UpdateTabRequest,
    interface_name: str = Query(..., description="The interface name the tab belongs to"),
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    name: str = Query(..., description="The name of the tab to update"),
    checkpoint: bool = Query(False, description="Whether this is a checkpoint update (manual save)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Update a tab by interface name, project ID, and tab name."""
    # Verify project exists and user has access
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project_id,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_id} not found or you don't have access.",
        )
    
    # Get interface
    interface = interface_dao.get_by_project_and_name(
        project_id=project.id,
        name=interface_name,
        is_checkpoint=checkpoint
    )
    
    if not interface:
        raise HTTPException(
            status_code=404,
            detail=f"Interface {interface_name} not found in project {project_id}.",
        )
    
    # Get tab
    tab = tab_dao.get_tab_by_interface_and_name(
        interface_id=interface.id,
        name=name,
        is_checkpoint=checkpoint
    )
    
    if not tab:
        raise HTTPException(
            status_code=404, 
            detail=f"Tab {name} not found in interface {interface_name}."
        )
    
    # Prepare update parameters
    update_params = {
        "interface_id": interface.id,
        "name": name,
        "is_checkpoint": checkpoint
    }
    
    # Add new name if provided
    if request.name is not None:
        update_params["new_name"] = request.name
    
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
    
    # Update the tab
    updated = tab_dao.update_tab_by_name(**update_params)
    
    # Get all tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=updated.id, is_checkpoint=updated.is_checkpoint)
    
    return _create_tab_response(updated, tiles)


@router.post(
    "/checkpoint",
    response_model=TabSchema,
    responses={
        200: {"description": "Tab checkpoint created successfully"},
        404: {"description": "Tab not found"},
        400: {"description": "Missing required parameters"},
    },
)
def create_tab_checkpoint(
    request_fastapi: Request,
    interface_name: str = Query(..., description="The interface name the tab belongs to"),
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    name: str = Query(..., description="The name of the tab to checkpoint"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Create a manual checkpoint (save) of a tab and all its tiles."""
    # Verify project exists and user has access
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project_id,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_id} not found or you don't have access.",
        )
    
    # Get interface
    interface = interface_dao.get_by_project_and_name(
        project_id=project.id,
        name=interface_name,
        is_checkpoint=False  # We're looking for the active interface
    )
    
    if not interface:
        raise HTTPException(
            status_code=404,
            detail=f"Interface {interface_name} not found in project {project_id}.",
        )
    
    # Get tab
    tab = tab_dao.get_tab_by_interface_and_name(
        interface_id=interface.id,
        name=name,
        is_checkpoint=False  # We're looking for the active tab
    )
    
    if not tab:
        raise HTTPException(
            status_code=404, 
            detail=f"Tab {name} not found in interface {interface_name}."
        )
    
    # Create a checkpoint
    updated = tab_dao.make_checkpoint_by_name(
        interface_id=interface.id,
        name=name
    )
    
    # Get all tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=updated.id, is_checkpoint=updated.is_checkpoint)
    
    return _create_tab_response(updated, tiles)


@router.delete(
    "/",
    status_code=204,
    responses={
        204: {"description": "Tab deleted successfully"},
        404: {"description": "Tab not found"},
        400: {"description": "Missing required parameters"},
    },
)
def delete_tab(
    request_fastapi: Request,
    interface_name: str = Query(..., description="The interface name the tab belongs to"),
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    name: str = Query(..., description="The name of the tab to delete"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Delete a tab by interface name, project ID, and tab name."""
    # Verify project exists and user has access
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project_id,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_id} not found or you don't have access.",
        )
    
    # Get interface
    interface = interface_dao.get_by_project_and_name(
        project_id=project.id,
        name=interface_name,
        is_checkpoint=False  # We're looking for the active interface
    )
    
    if not interface:
        raise HTTPException(
            status_code=404,
            detail=f"Interface {interface_name} not found in project {project_id}.",
        )
    
    # Get tab
    tab = tab_dao.get_tab_by_interface_and_name(
        interface_id=interface.id,
        name=name,
        is_checkpoint=False  # We're looking for the active tab
    )
    
    if not tab:
        raise HTTPException(
            status_code=404, 
            detail=f"Tab {name} not found in interface {interface_name}."
        )
    
    # First delete all tiles associated with this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=tab.id)
    for tile in tiles:
        tile_dao.delete_tile_by_name(tab_id=tab.id, name=tile.name)
    
    # Delete the tab
    success = tab_dao.delete_tab_by_name(interface_id=interface.id, name=name)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete tab.") 