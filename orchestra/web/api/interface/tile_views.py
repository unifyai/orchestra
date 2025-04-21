from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Body

from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.db.dao.tile_dao import TileDAO
from orchestra.web.api.interface.schema import (
    TileSchema,
    CreateTileRequest,
    UpdateTileRequest,
    TilePosition,
    TableTileSchema,
    PlotTileSchema,
    ViewTileSchema,
    EditorTileSchema,
)

router = APIRouter(prefix="/tile", tags=["tile"])


def _create_tile_response(tile) -> TileSchema:
    """Helper function to convert a tile entity to a TileSchema."""
    position = TilePosition(
        x=tile.position_x,
        y=tile.position_y,
        width=tile.width,
        height=tile.height,
    )
    
    # Create specialized tile schemas based on type
    table_tile = None
    plot_tile = None
    view_tile = None
    editor_tile = None
    
    # Populate specialized data based on tile type
    if tile.type == "Table" and hasattr(tile, "table_tile") and tile.table_tile:
        table_tile = TableTileSchema(
            table_type=tile.table_tile.table_type,
            column_context=tile.table_tile.column_context,
            page_number=tile.table_tile.page_number,
            column_order=tile.table_tile.column_order,
            hidden_columns=tile.table_tile.hidden_columns,
            sorting=tile.table_tile.sorting,
            grouping=tile.table_tile.grouping,
            group_sorting=tile.table_tile.group_sorting,
            columns_pin_left=tile.table_tile.columns_pin_left,
            columns_pin_right=tile.table_tile.columns_pin_right,
            selected=tile.table_tile.selected,
        )
    elif tile.type == "Plot" and hasattr(tile, "plot_tile") and tile.plot_tile:
        plot_tile = PlotTileSchema(
            plot_type=tile.plot_tile.plot_type,
            plot_scale_x=tile.plot_tile.plot_scale_x,
            plot_scale_y=tile.plot_tile.plot_scale_y,
            plot_aggregate=tile.plot_tile.plot_aggregate,
            x_axis=tile.plot_tile.x_axis,
            y_axis=tile.plot_tile.y_axis,
            plot_group_by=tile.plot_tile.plot_group_by,
            plot_group_by_colors=tile.plot_tile.plot_group_by_colors,
            bin_count=tile.plot_tile.bin_count,
            regression_line=tile.plot_tile.regression_line,
        )
    elif tile.type == "View" and hasattr(tile, "view_tile") and tile.view_tile:
        view_tile = ViewTileSchema(
            base_index=tile.view_tile.base_index,
        )
    elif tile.type == "Editor" and hasattr(tile, "editor_tile") and tile.editor_tile:
        editor_tile = EditorTileSchema(
            file_path=tile.editor_tile.file_path,
            file_type=tile.editor_tile.file_type,
            content=tile.editor_tile.content,
        )
    
    # Create and return the TileSchema
    return TileSchema(
        id=tile.id,
        tab_id=tile.tab_id,
        name=tile.name,
        position=position,
        type=tile.type,
        min_width=tile.min_width,
        min_height=tile.min_height,
        visible=tile.visible,
        locked=tile.locked,
        moved=tile.moved,
        static=tile.static,
        context=tile.context,
        table=tile.table,
        auto_update=tile.auto_update,
        freeze=tile.freeze,
        filters=tile.filters,
        common_filter=tile.common_filter,
        metric=tile.metric,
        is_checkpoint=tile.is_checkpoint,
        table_tile=table_tile,
        plot_tile=plot_tile,
        view_tile=view_tile,
        editor_tile=editor_tile,
        created_at=tile.created_at.isoformat() if tile.created_at else None,
        updated_at=tile.updated_at.isoformat() if tile.updated_at else None,
    )


@router.post(
    "/",
    response_model=TileSchema,
    status_code=201,
    responses={
        201: {"description": "Tile created successfully"},
        404: {"description": "Tab not found"},
        409: {"description": "Tile with this name already exists for this tab"},
    },
)
def create_tile(
    request_fastapi: Request,
    request: CreateTileRequest,
    checkpoint: bool = Query(False, description="Whether to create a checkpoint tile (manual save)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Create a new tile in a tab."""
    # Get the tab, either directly by ID if provided in the request
    tab = None
    if hasattr(request, "tab_id") and request.tab_id:
        tab = tab_dao.get_tab(request.tab_id)
    # Or by project_id + interface_name + tab_name if provided
    elif (hasattr(request, "project_id") and request.project_id and 
          hasattr(request, "interface_name") and request.interface_name and 
          hasattr(request, "tab_name") and request.tab_name):
        # First get the project
        project = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=request.project_id,
        )
        if not project:
            raise HTTPException(
                status_code=404,
                detail=f"Project {request.project_id} not found or you don't have access.",
            )
        
        # Then get the interface
        interface = interface_dao.get_by_project_and_name(
            project_id=project.id,
            name=request.interface_name,
            is_checkpoint=checkpoint
        )
        if not interface:
            raise HTTPException(
                status_code=404,
                detail=f"Interface {request.interface_name} not found in project {request.project_id}.",
            )
        
        # Then get the tab
        tab = tab_dao.get_tab_by_interface_and_name(
            interface_id=interface.id,
            name=request.tab_name,
            is_checkpoint=checkpoint
        )
    
    if not tab:
        raise HTTPException(
            status_code=404,
            detail=f"Tab not found. Please provide valid tab_id or project_id+interface_name+tab_name.",
        )
    
    # Check if tile already exists with the same name in this tab
    existing = tile_dao.get_tile_by_tab_and_name(
        tab_id=tab.id,
        name=request.name,
        is_checkpoint=checkpoint
    )
    
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Tile with name {request.name} already exists for this tab.",
        )
    
    # Create tile with position from the request
    position_data = {
        "position_x": request.position.x,
        "position_y": request.position.y,
        "width": request.position.width,
        "height": request.position.height,
    }
    
    # Create the tile
    tile = tile_dao.create_tile(
        tab_id=tab.id,
        name=request.name,
        type=request.type,
        min_width=request.min_width,
        min_height=request.min_height,
        visible=request.visible,
        locked=request.locked,
        moved=request.moved,
        static=request.static,
        context=request.context,
        table=request.table,
        auto_update=request.auto_update,
        freeze=request.freeze,
        filters=request.filters,
        common_filter=request.common_filter,
        metric=request.metric,
        is_checkpoint=checkpoint,
        **position_data
    )
    
    # Handle specialized tile data based on type
    if request.type == "Table" and request.table_tile:
        tile_dao.create_table_tile(
            tab_id=tab.id,
            name=request.name,
            headers=request.table_tile.headers if hasattr(request.table_tile, "headers") else None,
            rows=request.table_tile.rows if hasattr(request.table_tile, "rows") else None,
            **position_data
        )
    elif request.type == "Plot" and request.plot_tile:
        tile_dao.create_plot_tile(
            tab_id=tab.id,
            name=request.name,
            plot_data=request.plot_tile.plot_data if hasattr(request.plot_tile, "plot_data") else None,
            **position_data
        )
    elif request.type == "View" and request.view_tile:
        tile_dao.create_view_tile(
            tab_id=tab.id,
            name=request.name,
            view_type=request.view_tile.view_type if hasattr(request.view_tile, "view_type") else "markdown",
            view_data=request.view_tile.view_data if hasattr(request.view_tile, "view_data") else None,
            **position_data
        )
    elif request.type == "Editor" and request.editor_tile:
        tile_dao.create_editor_tile(
            tab_id=tab.id,
            name=request.name,
            content=request.editor_tile.content if hasattr(request.editor_tile, "content") else "",
            language=request.editor_tile.language if hasattr(request.editor_tile, "language") else "python",
            **position_data
        )
    
    # Get the full tile with all associated data
    created_tile = tile_dao.get_tile_by_tab_and_name(tab_id=tab.id, name=request.name, is_checkpoint=checkpoint)
    return _create_tile_response(created_tile)


@router.get(
    "/",
    response_model=List[TileSchema],
    responses={
        200: {"description": "Tiles list retrieved successfully"},
        404: {"description": "Project, interface, tab, or tile not found"},
        400: {"description": "Missing required parameters"},
    },
)
def get_tile(
    request_fastapi: Request,
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    interface_name: str = Query(..., description="The interface name the tab belongs to"),
    tab_name: str = Query(..., description="The tab name the tile belongs to"),
    name: Optional[str] = Query(None, description="The name of the tile to retrieve (optional)"),
    type: Optional[str] = Query(None, description="Filter tiles by type (Table, Plot, View, Editor)"),
    checkpoint: bool = Query(False, description="Whether to get checkpoint tiles (manual save)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """
    Get tiles by project, interface, tab, and optionally tile name.
    
    Returns a list of tiles for the specified tab, or a single tile if name is provided.
    """
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
        name=tab_name,
        is_checkpoint=checkpoint
    )
    
    if not tab:
        raise HTTPException(
            status_code=404, 
            detail=f"Tab {tab_name} not found in interface {interface_name}."
        )
    
    # If looking for a specific tile by name
    if name:
        tile = tile_dao.get_tile_by_tab_and_name(
            tab_id=tab.id,
            name=name,
            is_checkpoint=checkpoint
        )
        if not tile:
            raise HTTPException(
                status_code=404,
                detail=f"Tile {name} not found in tab {tab_name}."
            )
        
        # If checkpoint is requested but tile is not a checkpoint, get the latest checkpoint
        if checkpoint and not tile.is_checkpoint:
            checkpoint_tile = tile_dao.get_latest_checkpoint(tab.id, name)
            if checkpoint_tile:
                tile = checkpoint_tile
                
        return [_create_tile_response(tile)]
    
    # Otherwise list all tiles for the tab
    tiles = tile_dao.list_tiles_by_tab(
        tab_id=tab.id,
        is_checkpoint=checkpoint
    )
    
    # Apply type filter if provided
    if type and tiles:
        tiles = [tile for tile in tiles if tile.type == type]
    
    return [_create_tile_response(tile) for tile in tiles]


@router.put(
    "/",
    response_model=TileSchema,
    responses={
        200: {"description": "Tile updated successfully"},
        404: {"description": "Tile not found"},
        400: {"description": "Missing required parameters"},
    },
)
def update_tile(
    request_fastapi: Request,
    request: UpdateTileRequest,
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    interface_name: str = Query(..., description="The interface name the tab belongs to"),
    tab_name: str = Query(..., description="The tab name the tile belongs to"),
    name: str = Query(..., description="The name of the tile to update"),
    checkpoint: bool = Query(False, description="Whether this is a checkpoint update (manual save)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Update a tile by project ID, interface name, tab name, and tile name."""
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
        name=tab_name,
        is_checkpoint=checkpoint
    )
    
    if not tab:
        raise HTTPException(
            status_code=404, 
            detail=f"Tab {tab_name} not found in interface {interface_name}."
        )
    
    # Get tile
    tile = tile_dao.get_tile_by_tab_and_name(
        tab_id=tab.id,
        name=name,
        is_checkpoint=checkpoint
    )
    
    if not tile:
        raise HTTPException(
            status_code=404, 
            detail=f"Tile {name} not found in tab {tab_name}."
        )
    
    # Prepare update parameters
    update_params = {
        "tab_id": tab.id,
        "name": name,
        "is_checkpoint": checkpoint
    }
    
    # Add new name if provided
    if request.name is not None:
        update_params["new_name"] = request.name
    
    if request.position is not None:
        update_params["position_x"] = request.position.x
        update_params["position_y"] = request.position.y
        update_params["width"] = request.position.width
        update_params["height"] = request.position.height
    
    if request.min_width is not None:
        update_params["min_width"] = request.min_width
        
    if request.min_height is not None:
        update_params["min_height"] = request.min_height
        
    if request.visible is not None:
        update_params["visible"] = request.visible
        
    if request.locked is not None:
        update_params["locked"] = request.locked
        
    if request.context is not None:
        update_params["context"] = request.context
        
    if request.table is not None:
        update_params["table"] = request.table
        
    if request.auto_update is not None:
        update_params["auto_update"] = request.auto_update
        
    if request.freeze is not None:
        update_params["freeze"] = request.freeze
        
    if request.filters is not None:
        update_params["filters"] = request.filters
        
    if request.common_filter is not None:
        update_params["common_filter"] = request.common_filter
        
    if request.metric is not None:
        update_params["metric"] = request.metric
    
    # Update the tile
    updated = tile_dao.update_tile_by_name(**update_params)
    
    # Update specialized tile data if provided
    tile_type = tile.type  # Use the original tile's type
    
    if tile_type == "Table" and request.table_tile:
        # Use the same params but with the specialized tile update method
        specialized_params = {
            "tab_id": tab.id,
            "name": name,
            "headers": request.table_tile.headers if hasattr(request.table_tile, "headers") else None,
            "rows": request.table_tile.rows if hasattr(request.table_tile, "rows") else None,
        }
        
        # Call the specialized update method
        tile_dao.update_table_tile_by_name(**specialized_params)
             
    elif tile_type == "Plot" and request.plot_tile:
        specialized_params = {
            "tab_id": tab.id,
            "name": name,
            "plot_data": request.plot_tile.plot_data if hasattr(request.plot_tile, "plot_data") else None,
        }
        
        tile_dao.update_plot_tile_by_name(**specialized_params)
             
    elif tile_type == "View" and request.view_tile:
        specialized_params = {
            "tab_id": tab.id,
            "name": name,
            "view_type": request.view_tile.view_type if hasattr(request.view_tile, "view_type") else None,
            "view_data": request.view_tile.view_data if hasattr(request.view_tile, "view_data") else None,
        }
        
        tile_dao.update_view_tile_by_name(**specialized_params)
             
    elif tile_type == "Editor" and request.editor_tile:
        specialized_params = {
            "tab_id": tab.id,
            "name": name,
            "content": request.editor_tile.content if hasattr(request.editor_tile, "content") else None,
            "language": request.editor_tile.language if hasattr(request.editor_tile, "language") else None,
        }
        
        tile_dao.update_editor_tile_by_name(**specialized_params)
    
    # Get the full updated tile with all associated data
    updated_tile = tile_dao.get_tile_by_tab_and_name(
        tab_id=tab.id,
        name=request.name if request.name else name,
        is_checkpoint=checkpoint
    )
        
    return _create_tile_response(updated_tile)


@router.post(
    "/checkpoint",
    response_model=TileSchema,
    responses={
        200: {"description": "Tile checkpoint created successfully"},
        404: {"description": "Tile not found"},
        400: {"description": "Missing required parameters"},
    },
)
def create_tile_checkpoint(
    request_fastapi: Request,
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    interface_name: str = Query(..., description="The interface name the tab belongs to"),
    tab_name: str = Query(..., description="The tab name the tile belongs to"),
    name: str = Query(..., description="The name of the tile to checkpoint"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """
    Create a manual checkpoint (save) of a tile.
    
    Identifies the tile by project ID, interface name, tab name, and tile name.
    """
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
        name=tab_name,
        is_checkpoint=False  # We're looking for the active tab
    )
    
    if not tab:
        raise HTTPException(
            status_code=404, 
            detail=f"Tab {tab_name} not found in interface {interface_name}."
        )
    
    # Get tile
    tile = tile_dao.get_tile_by_tab_and_name(
        tab_id=tab.id,
        name=name,
        is_checkpoint=False  # We're looking for the active tile
    )
    
    if not tile:
        raise HTTPException(
            status_code=404, 
            detail=f"Tile {name} not found in tab {tab_name}."
        )
    
    # Create a checkpoint
    updated = tile_dao.make_checkpoint_by_name(
        tab_id=tab.id,
        name=name
    )
    
    return _create_tile_response(updated)


@router.delete(
    "/",
    status_code=204,
    responses={
        204: {"description": "Tile deleted successfully"},
        404: {"description": "Tile not found"},
        400: {"description": "Missing required parameters"},
    },
)
def delete_tile(
    request_fastapi: Request,
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    interface_name: str = Query(..., description="The interface name the tab belongs to"),
    tab_name: str = Query(..., description="The tab name the tile belongs to"),
    name: str = Query(..., description="The name of the tile to delete"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """
    Delete a tile by project ID, interface name, tab name, and tile name.
    """
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
        name=tab_name,
        is_checkpoint=False  # We're looking for the active tab
    )
    
    if not tab:
        raise HTTPException(
            status_code=404, 
            detail=f"Tab {tab_name} not found in interface {interface_name}."
        )
    
    # Get tile
    tile = tile_dao.get_tile_by_tab_and_name(
        tab_id=tab.id,
        name=name,
        is_checkpoint=False  # We're looking for the active tile
    )
    
    if not tile:
        raise HTTPException(
            status_code=404, 
            detail=f"Tile {name} not found in tab {tab_name}."
        )
    
    # Delete the tile
    success = tile_dao.delete_tile_by_name(tab_id=tab.id, name=name)
    
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Tile with name {name} not found in tab {tab_name}."
        )


@router.patch(
    "/",
    response_model=TileSchema,
    responses={
        200: {"description": "Tile patched successfully"},
        404: {"description": "Tile not found"},
        400: {"description": "Missing required parameters"},
    },
)
def patch_tile(
    request_fastapi: Request,
    update_data: Dict[str, Any],
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    interface_name: str = Query(..., description="The interface name the tab belongs to"),
    tab_name: str = Query(..., description="The tab name the tile belongs to"),
    name: str = Query(..., description="The name of the tile to patch"),
    checkpoint: bool = Query(False, description="Whether this is a checkpoint update (manual save)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """
    Partially update a tile by project ID, interface name, tab name, and tile name.
    
    Only the fields included in the request body will be updated.
    """
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
        name=tab_name,
        is_checkpoint=checkpoint
    )
    
    if not tab:
        raise HTTPException(
            status_code=404, 
            detail=f"Tab {tab_name} not found in interface {interface_name}."
        )
    
    # Get tile
    tile = tile_dao.get_tile_by_tab_and_name(
        tab_id=tab.id,
        name=name,
        is_checkpoint=checkpoint
    )
    
    if not tile:
        raise HTTPException(
            status_code=404, 
            detail=f"Tile {name} not found in tab {tab_name}."
        )
    
    # Apply the patch
    updated = tile_dao.patch_tile(
        update_data=update_data,
        tab_id=tab.id,
        name=name,
        is_checkpoint=checkpoint
    )
    
    if not updated:
        raise HTTPException(
            status_code=500,
            detail="Failed to patch tile."
        )
    
    # Get the full updated tile with all associated data
    patched_tile = tile_dao.get_tile_by_tab_and_name(
        tab_id=tab.id,
        name=update_data.get("name", name),  # Use new name if it was updated
        is_checkpoint=checkpoint
    )
    
    return _create_tile_response(patched_tile)


@router.patch("/specialized", response_model=TileSchema)
async def patch_specialized_tile(
    tile_type: str = Query(..., description="Type of tile to patch (Table, Plot, View, Editor)"),
    tab_id: Optional[str] = None,
    tile_id: Optional[str] = None,
    name: Optional[str] = None,
    update_data: Dict[str, Any] = Body(...),
    tile_dao: TileDAO = Depends(),
):
    """
    Generic endpoint to patch any specialized tile type.
    
    The tile_type parameter determines which specialized tile type to update.
    Valid values are: Table, Plot, View, Editor
    """
    # Validate the tile type
    valid_types = ["Table", "Plot", "View", "Editor"]
    if tile_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tile_type. Must be one of {', '.join(valid_types)}"
        )
    
    # Validate we have either tile_id or (tab_id and name)
    if not tile_id and (not tab_id or not name):
        raise HTTPException(
            status_code=400,
            detail="Must provide either tile_id or both tab_id and name"
        )
    
    # Format update data to include the specialized tile key if not already present
    specialized_key = f"{tile_type.lower()}_tile"
    if specialized_key not in update_data:
        # Handle fields at the root level that should be in the specialized tile
        # This allows for a more flexible API where specialized fields
        # can be included directly in the update_data
        update_data = {specialized_key: update_data}
    
    # Use the patch_tile DAO method with the specified tile_type
    result = tile_dao.patch_tile(
        update_data=update_data,
        id=tile_id,
        tab_id=tab_id,
        name=name,
        tile_type=tile_type
    )
    
    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"{tile_type} tile not found"
        )
    
    return _create_tile_response(result)
