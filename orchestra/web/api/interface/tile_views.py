from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Path, Request

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
        x=tile.x_position,
        y=tile.y_position,
        width=tile.width,
        height=tile.height,
    )
    
    # Create specialized tile schemas based on type
    table_tile = None
    plot_tile = None
    view_tile = None
    editor_tile = None
    
    # Populate specialized data based on tile type
    if tile.type == "Table" and tile.table_tile:
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
    elif tile.type == "Plot" and tile.plot_tile:
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
    elif tile.type == "View" and tile.view_tile:
        view_tile = ViewTileSchema(
            base_index=tile.view_tile.base_index,
        )
    elif tile.type == "Editor" and tile.editor_tile:
        editor_tile = EditorTileSchema(
            file_path=tile.editor_tile.file_name,
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
    tile_dao: TileDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Create a new tile in a tab."""
    # Check if tab exists
    tab = tab_dao.get_tab(request.tab_id)
    if not tab:
        raise HTTPException(
            status_code=404,
            detail=f"Tab {request.tab_id} not found.",
        )
    
    # Check if tile already exists with the same name in this tab
    existing = tile_dao.get_tile_by_name(
        tab_id=request.tab_id,
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
        "x_position": request.position.x,
        "y_position": request.position.y,
        "width": request.position.width,
        "height": request.position.height,
    }
    
    # Create the tile
    tile = tile_dao.create_tile(
        tab_id=request.tab_id,
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
            tile_id=tile.id,
            table_type=request.table_tile.table_type,
            column_context=request.table_tile.column_context,
            page_number=request.table_tile.page_number,
            column_order=request.table_tile.column_order,
            hidden_columns=request.table_tile.hidden_columns,
            sorting=request.table_tile.sorting,
            grouping=request.table_tile.grouping,
            group_sorting=request.table_tile.group_sorting,
            columns_pin_left=request.table_tile.columns_pin_left,
            columns_pin_right=request.table_tile.columns_pin_right,
            selected=request.table_tile.selected,
        )
    elif request.type == "Plot" and request.plot_tile:
        tile_dao.create_plot_tile(
            tile_id=tile.id,
            plot_type=request.plot_tile.plot_type,
            plot_scale_x=request.plot_tile.plot_scale_x,
            plot_scale_y=request.plot_tile.plot_scale_y,
            plot_aggregate=request.plot_tile.plot_aggregate,
            x_axis=request.plot_tile.x_axis,
            y_axis=request.plot_tile.y_axis,
            plot_group_by=request.plot_tile.plot_group_by,
            plot_group_by_colors=request.plot_tile.plot_group_by_colors,
            bin_count=request.plot_tile.bin_count,
            regression_line=request.plot_tile.regression_line,
        )
    elif request.type == "View" and request.view_tile:
        tile_dao.create_view_tile(
            tile_id=tile.id,
            base_index=request.view_tile.base_index,
        )
    elif request.type == "Editor" and request.editor_tile:
        tile_dao.create_editor_tile(
            tile_id=tile.id,
            file_name=request.editor_tile.file_path,
            file_type=request.editor_tile.file_type,
            content=request.editor_tile.content,
        )
    
    # Get the full tile with all associated data
    created_tile = tile_dao.get_tile(tile.id)
    return _create_tile_response(created_tile)


@router.get(
    "/{tile_id}",
    response_model=TileSchema,
    responses={
        200: {"description": "Tile details retrieved successfully"},
        404: {"description": "Tile not found"},
    },
)
def get_tile(
    request_fastapi: Request,
    tile_id: str = Path(..., description="The ID of the tile to retrieve"),
    checkpoint: bool = Query(False, description="Whether to get a checkpoint tile (manual save)"),
    tile_dao: TileDAO = Depends(),
):
    """Get a specific tile by ID."""
    tile = tile_dao.get_tile(tile_id)
    
    if not tile:
        raise HTTPException(status_code=404, detail=f"Tile {tile_id} not found.")
    
    # If checkpoint is requested but tile is not a checkpoint, get the latest checkpoint
    if checkpoint and not tile.is_checkpoint:
        checkpoint_tile = tile_dao.get_latest_checkpoint(tile.tab_id, tile.name)
        if checkpoint_tile:
            tile = checkpoint_tile
    
    return _create_tile_response(tile)


@router.get(
    "/",
    response_model=List[TileSchema],
    responses={
        200: {"description": "Tiles list retrieved successfully"},
    },
)
def list_tiles(
    request_fastapi: Request,
    tab_id: Optional[str] = Query(None, description="Filter tiles by tab ID"),
    type: Optional[str] = Query(None, description="Filter tiles by type"),
    name: Optional[str] = Query(None, description="Filter tiles by name"),
    checkpoint: bool = Query(False, description="Whether to list checkpoint tiles (manual save)"),
    tile_dao: TileDAO = Depends(),
):
    """List all tiles, optionally filtered by tab ID and/or type."""
    tiles = tile_dao.list_tiles(
        tab_id=tab_id,
        type=type,
        name=name,
        is_checkpoint=checkpoint
    )
    
    return [_create_tile_response(tile) for tile in tiles]


@router.put(
    "/{tile_id}",
    response_model=TileSchema,
    responses={
        200: {"description": "Tile updated successfully"},
        404: {"description": "Tile not found"},
    },
)
def update_tile(
    request_fastapi: Request,
    request: UpdateTileRequest,
    tile_id: str = Path(..., description="The ID of the tile to update"),
    checkpoint: bool = Query(False, description="Whether this is a checkpoint update (manual save)"),
    tile_dao: TileDAO = Depends(),
):
    """Update a tile by ID."""
    tile = tile_dao.get_tile(tile_id)
    
    if not tile:
        raise HTTPException(status_code=404, detail=f"Tile {tile_id} not found.")
    
    # Prepare update parameters
    update_params = {"id": tile_id}
    
    if request.name is not None:
        update_params["name"] = request.name
    
    if request.position is not None:
        update_params["x_position"] = request.position.x
        update_params["y_position"] = request.position.y
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
    
    if checkpoint:
        update_params["is_checkpoint"] = True
    
    # Update the tile
    updated = tile_dao.update_tile(**update_params)
    
    # Update specialized tile data if provided
    if tile.type == "Table" and request.table_tile:
        tile_dao.update_table_tile(
            tile_id=tile_id,
            table_type=request.table_tile.table_type,
            column_context=request.table_tile.column_context,
            page_number=request.table_tile.page_number,
            column_order=request.table_tile.column_order,
            hidden_columns=request.table_tile.hidden_columns,
            sorting=request.table_tile.sorting,
            grouping=request.table_tile.grouping,
            group_sorting=request.table_tile.group_sorting,
            columns_pin_left=request.table_tile.columns_pin_left,
            columns_pin_right=request.table_tile.columns_pin_right,
            selected=request.table_tile.selected,
        )
    elif tile.type == "Plot" and request.plot_tile:
        tile_dao.update_plot_tile(
            tile_id=tile_id,
            plot_type=request.plot_tile.plot_type,
            plot_scale_x=request.plot_tile.plot_scale_x,
            plot_scale_y=request.plot_tile.plot_scale_y,
            plot_aggregate=request.plot_tile.plot_aggregate,
            x_axis=request.plot_tile.x_axis,
            y_axis=request.plot_tile.y_axis,
            plot_group_by=request.plot_tile.plot_group_by,
            plot_group_by_colors=request.plot_tile.plot_group_by_colors,
            bin_count=request.plot_tile.bin_count,
            regression_line=request.plot_tile.regression_line,
        )
    elif tile.type == "View" and request.view_tile:
        tile_dao.update_view_tile(
            tile_id=tile_id,
            base_index=request.view_tile.base_index,
        )
    elif tile.type == "Editor" and request.editor_tile:
        tile_dao.update_editor_tile(
            tile_id=tile_id,
            file_name=request.editor_tile.file_path,
            file_type=request.editor_tile.file_type,
            content=request.editor_tile.content,
        )
    
    # Get the full updated tile with all associated data
    updated_tile = tile_dao.get_tile(tile_id)
    return _create_tile_response(updated_tile)


@router.post(
    "/{tile_id}/checkpoint",
    response_model=TileSchema,
    responses={
        200: {"description": "Tile checkpoint created successfully"},
        404: {"description": "Tile not found"},
    },
)
def create_tile_checkpoint(
    request_fastapi: Request,
    tile_id: str = Path(..., description="The ID of the tile to checkpoint"),
    tile_dao: TileDAO = Depends(),
):
    """Create a manual checkpoint (save) of a tile."""
    # Get the current tile
    tile = tile_dao.get_tile(tile_id)
    if not tile:
        raise HTTPException(status_code=404, detail=f"Tile {tile_id} not found.")
    
    # Create a checkpoint by setting the is_checkpoint flag
    updated = tile_dao.make_checkpoint(tile_id)
    
    return _create_tile_response(updated)


@router.delete(
    "/{tile_id}",
    status_code=204,
    responses={
        204: {"description": "Tile deleted successfully"},
        404: {"description": "Tile not found"},
    },
)
def delete_tile(
    request_fastapi: Request,
    tile_id: str = Path(..., description="The ID of the tile to delete"),
    tile_dao: TileDAO = Depends(),
):
    """Delete a tile by ID."""
    tile = tile_dao.get_tile(tile_id)
    
    if not tile:
        raise HTTPException(status_code=404, detail=f"Tile {tile_id} not found.")
    
    # Delete the tile
    success = tile_dao.delete_tile(tile_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete tile.") 