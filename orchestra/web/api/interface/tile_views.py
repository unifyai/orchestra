from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.db.dao.tile_dao import TileDAO
from orchestra.web.api.interface.schema import (
    CreateTileRequest,
    EditorTileSchema,
    PlotTileSchema,
    TableTileSchema,
    TileSchema,
    UpdateTileRequest,
    ViewTileSchema,
)

router = APIRouter(prefix="/tile", tags=["tile"])


def _create_tile_response(tile) -> TileSchema:
    """Helper function to convert a tile entity to a TileSchema with specialized tile data."""

    # Create specialized tile data schemas if they exist
    table_tile_data = None
    plot_tile_data = None
    view_tile_data = None
    editor_tile_data = None

    if hasattr(tile, "table_tile") and tile.table_tile:
        table_tile_data = TableTileSchema(
            id=str(tile.table_tile.id),
            tile_id=str(tile.table_tile.tile_id),
            table_type=tile.table_tile.table_type,
            page_number=tile.table_tile.page_number,
            column_order=tile.table_tile.column_order,
            hidden_columns=tile.table_tile.hidden_columns,
            sorting=tile.table_tile.sorting,
            group_sorting=tile.table_tile.group_sorting,
            columns_pin_left=tile.table_tile.columns_pin_left,
            columns_pin_right=tile.table_tile.columns_pin_right,
            selected=tile.table_tile.selected,
        )

    if hasattr(tile, "plot_tile") and tile.plot_tile:
        plot_tile_data = PlotTileSchema(
            id=str(tile.plot_tile.id),
            tile_id=str(tile.plot_tile.tile_id),
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

    if hasattr(tile, "view_tile") and tile.view_tile:
        view_tile_data = ViewTileSchema(
            id=str(tile.view_tile.id),
            tile_id=str(tile.view_tile.tile_id),
            base_index=tile.view_tile.base_index,
        )

    if hasattr(tile, "editor_tile") and tile.editor_tile:
        editor_tile_data = EditorTileSchema(
            id=str(tile.editor_tile.id),
            tile_id=str(tile.editor_tile.tile_id),
            file_path=tile.editor_tile.file_path,
            file_type=tile.editor_tile.file_type,
            content=tile.editor_tile.content,
        )

    position = {
        "x": tile.x_position,
        "y": tile.y_position,
        "width": tile.width,
        "height": tile.height,
    }

    return TileSchema(
        id=str(tile.id),
        tab_id=str(tile.tab_id),
        name=tile.name,
        type=tile.type,
        position=position,
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
        column_context=tile.column_context,
        grouping=tile.grouping,
        is_checkpoint=tile.is_checkpoint,
        table_tile=table_tile_data,
        plot_tile=plot_tile_data,
        view_tile=view_tile_data,
        editor_tile=editor_tile_data,
        created_at=tile.created_at.isoformat() if tile.created_at else None,
        updated_at=tile.updated_at.isoformat() if tile.updated_at else None,
    )


def _get_tile(
    tile_id: Optional[str],
    tab_id: Optional[str],
    name: Optional[str],
    checkpoint: bool,
    tab_dao: TabDAO,
    tile_dao: TileDAO,
    for_update: bool = False,
) -> Tuple[object, object]:
    """Helper function to retrieve a tile by ID or by tab_id and name."""
    tile = None
    tab = None

    # Get by ID if provided
    if tile_id:
        tile = tile_dao.get(tile_id, is_checkpoint=checkpoint)
        if not tile:
            raise HTTPException(
                status_code=404,
                detail=f"Tile with ID {tile_id} not found.",
            )
        # Get tab to verify access
        tab = tab_dao.get(tile.tab_id)
        if not tab:
            raise HTTPException(
                status_code=404,
                detail=f"Tab with ID {tile.tab_id} not found.",
            )
    # Get by tab_id and name
    elif tab_id and name:
        # Get tab
        tab = tab_dao.get(tab_id)
        if not tab:
            raise HTTPException(
                status_code=404,
                detail=f"Tab with ID {tab_id} not found.",
            )

        # For specific operations like deletion, we need to get the active tile
        is_checkpoint = checkpoint
        if for_update and (checkpoint_operations := ["delete", "checkpoint"]):
            is_checkpoint = False

        # Get tile by tab_id and name
        tile = tile_dao.get_by_tab_and_name(
            tab_id=tab_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if not tile:
            raise HTTPException(
                status_code=404,
                detail=f"Tile {name} not found in tab {tab_id}.",
            )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either tile_id or both tab_id and name must be provided.",
        )

    return tile, tab


@router.post(
    "/",
    response_model=TileSchema,
    status_code=201,
    responses={
        201: {
            "description": "Tile created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "tab_id": "tab_456",
                        "name": "Data Table",
                        "type": "Table",
                        "position": {"x": 0, "y": 0, "width": 6, "height": 4},
                        "min_width": 2,
                        "min_height": 2,
                        "visible": True,
                        "locked": False,
                        "moved": False,
                        "static": False,
                        "context": None,
                        "table": "main_data",
                        "auto_update": True,
                        "freeze": False,
                        "filters": None,
                        "common_filter": None,
                        "metric": None,
                        "column_context": None,
                        "grouping": None,
                        "is_checkpoint": False,
                        "table_tile": {
                            "id": "table_123",
                            "tile_id": "123",
                            "table_type": "Data Table",
                            "page_number": 1,
                            "column_order": ["id", "name", "value"],
                            "hidden_columns": [],
                            "sorting": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:00:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Tab not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Tab not found. Please provide valid tab_id or project_id+interface_name+tab_name.",
                    },
                },
            },
        },
        409: {
            "description": "Tile with this name already exists for this tab",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Tile with name Data Table already exists in this tab.",
                    },
                },
            },
        },
    },
)
def create_tile(
    request_fastapi: Request,
    request: CreateTileRequest,
    checkpoint: bool = Query(
        False,
        description="Whether to create a checkpoint tile (manual save)",
    ),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Create a new tile in a tab."""
    # Get the tab, either directly by ID if provided in the request
    tab = None
    if hasattr(request, "tab_id") and request.tab_id:
        tab = tab_dao.get(request.tab_id)
    # Or by project_id + interface_name + tab_name if provided
    elif (
        hasattr(request, "project_id")
        and request.project_id
        and hasattr(request, "interface_name")
        and request.interface_name
        and hasattr(request, "tab_name")
        and request.tab_name
    ):
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
            is_checkpoint=checkpoint,
        )
        if not interface:
            raise HTTPException(
                status_code=404,
                detail=f"Interface {request.interface_name} not found in project {request.project_id}.",
            )

        # Then get the tab
        tab = tab_dao.get_by_interface_and_name(
            interface_id=interface.id,
            name=request.tab_name,
            is_checkpoint=checkpoint,
        )

    if not tab:
        raise HTTPException(
            status_code=404,
            detail=f"Tab not found. Please provide valid tab_id or project_id+interface_name+tab_name.",
        )

    try:
        # Create tile with position from the request
        position_data = {
            "x_position": request.position.x,
            "y_position": request.position.y,
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
            column_context=request.column_context,
            grouping=request.grouping,
            is_checkpoint=checkpoint,
            **position_data,
        )

        print(tile.__dict__)

        # Handle specialized tile data based on type
        if request.type == "Table" and request.table_tile:
            tile_dao.create_table_tile(
                tab_id=tab.id,
                name=request.name,
                tile_id=tile.id,
                table_type=request.table_tile.table_type
                if hasattr(request.table_tile, "table_type")
                else None,
                page_number=request.table_tile.page_number
                if hasattr(request.table_tile, "page_number")
                else None,
                column_order=request.table_tile.column_order
                if hasattr(request.table_tile, "column_order")
                else None,
                hidden_columns=request.table_tile.hidden_columns
                if hasattr(request.table_tile, "hidden_columns")
                else None,
                sorting=request.table_tile.sorting
                if hasattr(request.table_tile, "sorting")
                else None,
                group_sorting=request.table_tile.group_sorting
                if hasattr(request.table_tile, "group_sorting")
                else None,
                columns_pin_left=request.table_tile.columns_pin_left
                if hasattr(request.table_tile, "columns_pin_left")
                else None,
                columns_pin_right=request.table_tile.columns_pin_right
                if hasattr(request.table_tile, "columns_pin_right")
                else None,
                selected=request.table_tile.selected
                if hasattr(request.table_tile, "selected")
                else None,
                is_checkpoint=checkpoint,
                **position_data,
            )
        elif request.type == "Plot" and request.plot_tile:
            tile_dao.create_plot_tile(
                tab_id=tab.id,
                name=request.name,
                tile_id=tile.id,
                plot_type=request.plot_tile.plot_type
                if hasattr(request.plot_tile, "plot_type")
                else None,
                plot_scale_x=request.plot_tile.plot_scale_x
                if hasattr(request.plot_tile, "plot_scale_x")
                else None,
                plot_scale_y=request.plot_tile.plot_scale_y
                if hasattr(request.plot_tile, "plot_scale_y")
                else None,
                plot_aggregate=request.plot_tile.plot_aggregate
                if hasattr(request.plot_tile, "plot_aggregate")
                else None,
                x_axis=request.plot_tile.x_axis
                if hasattr(request.plot_tile, "x_axis")
                else None,
                y_axis=request.plot_tile.y_axis
                if hasattr(request.plot_tile, "y_axis")
                else None,
                plot_group_by=request.plot_tile.plot_group_by
                if hasattr(request.plot_tile, "plot_group_by")
                else None,
                plot_group_by_colors=request.plot_tile.plot_group_by_colors
                if hasattr(request.plot_tile, "plot_group_by_colors")
                else None,
                bin_count=request.plot_tile.bin_count
                if hasattr(request.plot_tile, "bin_count")
                else None,
                regression_line=request.plot_tile.regression_line
                if hasattr(request.plot_tile, "regression_line")
                else None,
                is_checkpoint=checkpoint,
                **position_data,
            )
        elif request.type == "View" and request.view_tile:
            tile_dao.create_view_tile(
                tab_id=tab.id,
                name=request.name,
                tile_id=tile.id,
                base_index=request.view_tile.base_index
                if hasattr(request.view_tile, "base_index")
                else None,
                is_checkpoint=checkpoint,
                **position_data,
            )
        elif request.type == "Editor" and request.editor_tile:
            tile_dao.create_editor_tile(
                tab_id=tab.id,
                name=request.name,
                tile_id=tile.id,
                content=request.editor_tile.content
                if hasattr(request.editor_tile, "content")
                else "",
                file_path=request.editor_tile.file_path
                if hasattr(request.editor_tile, "file_path")
                else None,
                file_type=request.editor_tile.file_type
                if hasattr(request.editor_tile, "file_type")
                else None,
                is_checkpoint=checkpoint,
                **position_data,
            )

        # Get the full tile with all associated data
        created_tile = tile_dao.get_by_tab_and_name(
            tab_id=tab.id,
            name=request.name,
            is_checkpoint=checkpoint,
        )
        return _create_tile_response(created_tile)

    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create tile: {str(e)}")


@router.get(
    "/",
    response_model=TileSchema,
    responses={
        200: {
            "description": "Tile details retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "tab_id": "tab_456",
                        "name": "Data Table",
                        "type": "Table",
                        "position": {"x": 0, "y": 0, "width": 6, "height": 4},
                        "min_width": 2,
                        "min_height": 2,
                        "visible": True,
                        "locked": False,
                        "moved": False,
                        "static": False,
                        "context": None,
                        "table": "main_data",
                        "auto_update": True,
                        "freeze": False,
                        "filters": None,
                        "common_filter": None,
                        "metric": None,
                        "column_context": None,
                        "grouping": None,
                        "is_checkpoint": False,
                        "table_tile": {
                            "id": "table_123",
                            "tile_id": "123",
                            "table_type": "Data Table",
                            "page_number": 1,
                            "column_order": ["id", "name", "value"],
                            "hidden_columns": [],
                            "sorting": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:00:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Tile not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Tile with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tile_id or both tab_id and name must be provided.",
                    },
                },
            },
        },
    },
)
def get_tile(
    tile_id: Optional[str] = Query(None, description="The ID of the tile to retrieve"),
    tab_id: Optional[str] = Query(None, description="The tab ID the tile belongs to"),
    name: Optional[str] = Query(None, description="The name of the tile to retrieve"),
    checkpoint: bool = Query(
        False,
        description="Whether to get a checkpoint tile (manual save)",
    ),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Get a specific tile by ID or by tab_id and name."""
    # Use helper function to get tile
    tile, _ = _get_tile(
        tile_id=tile_id,
        tab_id=tab_id,
        name=name,
        checkpoint=checkpoint,
        tab_dao=tab_dao,
        tile_dao=tile_dao,
    )

    return _create_tile_response(tile)


@router.get(
    "/list",
    response_model=List[TileSchema],
    responses={
        200: {
            "description": "Tiles list retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": "123",
                            "tab_id": "tab_456",
                            "name": "Data Table",
                            "type": "Table",
                            "position": {"x": 0, "y": 0, "width": 6, "height": 4},
                            "min_width": 2,
                            "min_height": 2,
                            "visible": True,
                            "locked": False,
                            "moved": False,
                            "static": False,
                            "context": None,
                            "table": "main_data",
                            "auto_update": True,
                            "freeze": False,
                            "filters": None,
                            "common_filter": None,
                            "metric": None,
                            "column_context": None,
                            "grouping": None,
                            "is_checkpoint": False,
                            "table_tile": {
                                "id": "table_123",
                                "tile_id": "123",
                                "table_type": "Data Table",
                                "page_number": 1,
                                "column_order": ["id", "name", "value"],
                                "hidden_columns": [],
                                "sorting": None,
                                "group_sorting": None,
                                "columns_pin_left": [],
                                "columns_pin_right": [],
                                "selected": None,
                            },
                            "plot_tile": None,
                            "view_tile": None,
                            "editor_tile": None,
                            "created_at": "2024-01-01T12:00:00Z",
                            "updated_at": "2024-01-01T12:00:00Z",
                        },
                        {
                            "id": "124",
                            "tab_id": "tab_456",
                            "name": "Chart",
                            "type": "Plot",
                            "position": {"x": 6, "y": 0, "width": 6, "height": 4},
                            "min_width": 2,
                            "min_height": 2,
                            "visible": True,
                            "locked": False,
                            "moved": False,
                            "static": False,
                            "context": None,
                            "table": "main_data",
                            "auto_update": True,
                            "freeze": False,
                            "filters": None,
                            "common_filter": None,
                            "metric": None,
                            "column_context": None,
                            "grouping": None,
                            "is_checkpoint": False,
                            "table_tile": None,
                            "plot_tile": {
                                "id": "plot_124",
                                "tile_id": "124",
                                "plot_type": "scatter",
                                "plot_scale_x": "linear",
                                "plot_scale_y": "linear",
                                "plot_aggregate": None,
                                "x_axis": "x",
                                "y_axis": "y",
                                "plot_group_by": None,
                                "plot_group_by_colors": None,
                                "bin_count": 10,
                                "regression_line": False,
                            },
                            "view_tile": None,
                            "editor_tile": None,
                            "created_at": "2024-01-01T12:00:00Z",
                            "updated_at": "2024-01-01T12:00:00Z",
                        },
                    ],
                },
            },
        },
        404: {
            "description": "Tab not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Tab with ID tab_456 not found."},
                },
            },
        },
    },
)
def list_tiles(
    tab_id: str = Query(..., description="The tab ID to list tiles for"),
    name: Optional[str] = Query(None, description="Filter tiles by name"),
    type: Optional[str] = Query(None, description="Filter tiles by type"),
    checkpoint: bool = Query(
        False,
        description="Whether to list checkpoint tiles (manual save)",
    ),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """List all tiles for a tab."""
    # Get tab
    tab = tab_dao.get(tab_id)
    if not tab:
        raise HTTPException(
            status_code=404,
            detail=f"Tab with ID {tab_id} not found.",
        )

    # Get tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(
        tab_id=tab_id,
        name=name,
        type=type,
        is_checkpoint=checkpoint,
    )

    return [_create_tile_response(tile) for tile in tiles]


@router.put(
    "/",
    response_model=TileSchema,
    responses={
        200: {
            "description": "Tile updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "tab_id": "tab_456",
                        "name": "Updated Data Table",
                        "type": "Table",
                        "position": {"x": 1, "y": 1, "width": 8, "height": 5},
                        "min_width": 2,
                        "min_height": 2,
                        "visible": True,
                        "locked": False,
                        "moved": True,
                        "static": False,
                        "context": None,
                        "table": "main_data",
                        "auto_update": True,
                        "freeze": False,
                        "filters": None,
                        "common_filter": None,
                        "metric": None,
                        "column_context": None,
                        "grouping": None,
                        "is_checkpoint": False,
                        "table_tile": {
                            "id": "table_123",
                            "tile_id": "123",
                            "table_type": "Data Table",
                            "page_number": 1,
                            "column_order": ["id", "name", "value"],
                            "hidden_columns": [],
                            "sorting": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:30:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Tile not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Tile with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tile_id or both tab_id and name must be provided.",
                    },
                },
            },
        },
    },
)
def update_tile(
    request: UpdateTileRequest,
    tile_id: Optional[str] = Query(None, description="The ID of the tile to update"),
    tab_id: Optional[str] = Query(None, description="The tab ID the tile belongs to"),
    name: Optional[str] = Query(None, description="The name of the tile to update"),
    checkpoint: bool = Query(
        False,
        description="Whether this is a checkpoint update (manual save)",
    ),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Update a tile by ID or by tab_id and name."""
    # Use helper function to get tile

    # Convert Pydantic model to dict, excluding unset fields
    update_dict = request.model_dump(exclude_unset=True)

    # Update the tile
    if tile_id:
        updated = tile_dao.update_tile(
            id=tile_id,
            is_checkpoint=checkpoint,
            **update_dict,
        )
    else:
        tile, _ = _get_tile(
            tile_id=tile_id,
            tab_id=tab_id,
            name=name,
            checkpoint=checkpoint,
            tab_dao=tab_dao,
            tile_dao=tile_dao,
        )
        updated = tile_dao.update_tile(
            id=tile.id,  # We already have the tile, so use its ID
            is_checkpoint=checkpoint,
            **update_dict,
        )

    return _create_tile_response(updated)


@router.post(
    "/checkpoint",
    response_model=TileSchema,
    responses={
        200: {
            "description": "Tile checkpoint created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "checkpoint_123",
                        "tab_id": "checkpoint_tab_456",
                        "name": "Data Table",
                        "type": "Table",
                        "position": {"x": 0, "y": 0, "width": 6, "height": 4},
                        "min_width": 2,
                        "min_height": 2,
                        "visible": True,
                        "locked": False,
                        "moved": False,
                        "static": False,
                        "context": None,
                        "table": "main_data",
                        "auto_update": True,
                        "freeze": False,
                        "filters": None,
                        "common_filter": None,
                        "metric": None,
                        "column_context": None,
                        "grouping": None,
                        "is_checkpoint": True,
                        "table_tile": {
                            "id": "checkpoint_table_123",
                            "tile_id": "checkpoint_123",
                            "table_type": "Data Table",
                            "column_context": None,
                            "page_number": 1,
                            "column_order": ["id", "name", "value"],
                            "hidden_columns": [],
                            "sorting": None,
                            "grouping": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:30:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Tile not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Tile with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tile_id or both tab_id and name must be provided.",
                    },
                },
            },
        },
        500: {
            "description": "Failed to create checkpoint",
            "content": {
                "application/json": {
                    "example": {"detail": "Failed to create tile checkpoint."},
                },
            },
        },
    },
)
def create_tile_checkpoint(
    tile_id: Optional[str] = Query(
        None,
        description="The ID of the tile to checkpoint",
    ),
    tab_id: Optional[str] = Query(None, description="The tab ID the tile belongs to"),
    name: Optional[str] = Query(None, description="The name of the tile to checkpoint"),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Create a manual checkpoint (save) of a tile."""
    # Use helper function to get tile with for_update=True to ensure we're operating on the active tile
    tile, tab = _get_tile(
        tile_id=tile_id,
        tab_id=tab_id,
        name=name,
        checkpoint=False,
        tab_dao=tab_dao,
        tile_dao=tile_dao,
        for_update=True,
    )

    # First, get the interface that owns this tab
    interface = interface_dao.get(tab.interface_id)
    if not interface:
        raise HTTPException(
            status_code=404,
            detail=f"Interface with ID {tab.interface_id} not found.",
        )

    # Ensure the parent interface has a checkpoint
    checkpoint_interface = interface_dao.get_checkpoint(id=interface.id)
    if not checkpoint_interface:
        # If no checkpoint exists for the interface, create one
        checkpoint_interface = interface_dao.checkpoint_interface(
            interface_id=interface.id,
        )

        if not checkpoint_interface:
            raise HTTPException(
                status_code=500,
                detail="Failed to create checkpoint for parent interface.",
            )

    # Ensure the parent tab has a checkpoint
    checkpoint_tab = tab_dao.get_checkpoint(id=tab.id)
    if not checkpoint_tab:
        # If no checkpoint exists for the tab, create one
        checkpoint_tab = tab_dao.checkpoint_tab(
            tab_id=tab.id,
            target_interface_id=checkpoint_interface.id,
        )

        if not checkpoint_tab:
            raise HTTPException(
                status_code=500,
                detail="Failed to create checkpoint for parent tab.",
            )

    # Use the TileDAO checkpoint_tile method to handle the checkpoint creation
    checkpoint_tile = tile_dao.checkpoint_tile(
        tile_id=tile.id,
        target_tab_id=checkpoint_tab.id,
    )

    if not checkpoint_tile:
        raise HTTPException(status_code=500, detail="Failed to create tile checkpoint.")

    return _create_tile_response(checkpoint_tile)


@router.get(
    "/checkpoint",
    response_model=TileSchema,
    responses={
        200: {
            "description": "Tile checkpoint retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "checkpoint_123",
                        "tab_id": "checkpoint_tab_456",
                        "name": "Data Table",
                        "type": "Table",
                        "position": {"x": 0, "y": 0, "width": 6, "height": 4},
                        "min_width": 2,
                        "min_height": 2,
                        "visible": True,
                        "locked": False,
                        "moved": False,
                        "static": False,
                        "context": None,
                        "table": "main_data",
                        "auto_update": True,
                        "freeze": False,
                        "filters": None,
                        "common_filter": None,
                        "metric": None,
                        "is_checkpoint": True,
                        "table_tile": {
                            "id": "checkpoint_table_123",
                            "tile_id": "checkpoint_123",
                            "table_type": "Data Table",
                            "page_number": 1,
                            "column_order": ["id", "name", "value"],
                            "hidden_columns": [],
                            "sorting": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:30:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Tile or checkpoint not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "No checkpoint found for the specified tile.",
                    },
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tile_id or both tab_id and name must be provided.",
                    },
                },
            },
        },
    },
)
def get_tile_checkpoint(
    tile_id: Optional[str] = Query(
        None,
        description="The ID of the tile to get checkpoint for",
    ),
    tab_id: Optional[str] = Query(None, description="The tab ID the tile belongs to"),
    name: Optional[str] = Query(
        None,
        description="The name of the tile to get checkpoint for",
    ),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Get the checkpoint (manual save) for a tile by ID or by tab_id and name."""
    # First get the active tile to use as a reference
    tile = None
    if tile_id:
        tile = tile_dao.get(id=tile_id)
        if not tile:
            raise HTTPException(
                status_code=404,
                detail=f"Tile with ID {tile_id} not found.",
            )
    elif tab_id and name:
        tile = tile_dao.get_by_tab_and_name(
            tab_id=tab_id,
            name=name,
            is_checkpoint=False,
        )
        if not tile:
            raise HTTPException(
                status_code=404,
                detail=f"Tile with name {name} not found in tab {tab_id}.",
            )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either tile_id or both tab_id and name must be provided.",
        )

    # Get the checkpoint version of this tile
    checkpoint_tile = tile_dao.get_checkpoint(id=tile.id)

    if not checkpoint_tile:
        raise HTTPException(
            status_code=404,
            detail="No checkpoint found for the specified tile.",
        )

    return _create_tile_response(checkpoint_tile)


@router.delete(
    "/",
    status_code=204,
    responses={
        204: {
            "description": "Tile deleted successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Tile deleted successfully"},
                },
            },
        },
        404: {
            "description": "Tile not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Tile with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tile_id or both tab_id and name must be provided.",
                    },
                },
            },
        },
        500: {
            "description": "Failed to delete tile",
            "content": {
                "application/json": {
                    "example": {"detail": "Failed to delete tile."},
                },
            },
        },
    },
)
def delete_tile(
    tile_id: Optional[str] = Query(None, description="The ID of the tile to delete"),
    tab_id: Optional[str] = Query(None, description="The tab ID the tile belongs to"),
    name: Optional[str] = Query(None, description="The name of the tile to delete"),
    tile_dao: TileDAO = Depends(),
):
    """Delete a tile by ID or by tab_id and name."""
    # Use helper function to get tile with for_update=True to ensure we're deleting the active tile

    # Delete the tile
    if tile_id:
        success = tile_dao.delete_tile(id=tile_id)
    else:
        success = tile_dao.delete_tile(tab_id=tab_id, name=name)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete tile.")


@router.patch(
    "/",
    response_model=TileSchema,
    responses={
        200: {
            "description": "Tile patched successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "tab_id": "tab_456",
                        "name": "Data Table",
                        "type": "Table",
                        "position": {"x": 2, "y": 2, "width": 6, "height": 4},
                        "min_width": 2,
                        "min_height": 2,
                        "visible": True,
                        "locked": False,
                        "moved": True,
                        "static": False,
                        "context": None,
                        "table": "main_data",
                        "auto_update": True,
                        "freeze": False,
                        "filters": None,
                        "common_filter": None,
                        "metric": None,
                        "column_context": None,
                        "grouping": None,
                        "is_checkpoint": False,
                        "table_tile": {
                            "id": "table_123",
                            "tile_id": "123",
                            "table_type": "Data Table",
                            "page_number": 1,
                            "column_order": ["id", "name", "value"],
                            "hidden_columns": [],
                            "sorting": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:45:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Tile not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Tile with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tile_id or both tab_id and name must be provided.",
                    },
                },
            },
        },
        500: {
            "description": "Failed to patch tile",
            "content": {
                "application/json": {
                    "example": {"detail": "Failed to patch tile."},
                },
            },
        },
    },
)
def patch_tile(
    update_data: Dict[str, Any],
    tile_id: Optional[str] = Query(None, description="The ID of the tile to patch"),
    tab_id: Optional[str] = Query(None, description="The tab ID the tile belongs to"),
    name: Optional[str] = Query(None, description="The name of the tile to patch"),
    checkpoint: bool = Query(
        False,
        description="Whether this is a checkpoint update (manual save)",
    ),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """
    Partially update a tile by ID or by tab_id and name.

    Only the fields included in the request body will be updated.
    """
    # Use helper function to get tile
    tile, _ = _get_tile(
        tile_id=tile_id,
        tab_id=tab_id,
        name=name,
        checkpoint=checkpoint,
        tab_dao=tab_dao,
        tile_dao=tile_dao,
    )

    # Apply the patch
    updated = tile_dao.patch_tile(
        update_data=update_data,
        id=tile.id,
        tab_id=tile.tab_id,
        name=tile.name,
        is_checkpoint=checkpoint,
    )

    if not updated:
        raise HTTPException(status_code=500, detail="Failed to patch tile.")

    return _create_tile_response(updated)


@router.patch(
    "/specialized",
    response_model=TileSchema,
    responses={
        200: {
            "description": "Specialized tile patched successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "tab_id": "tab_456",
                        "name": "Data Table",
                        "type": "Table",
                        "position": {"x": 0, "y": 0, "width": 6, "height": 4},
                        "min_width": 2,
                        "min_height": 2,
                        "visible": True,
                        "locked": False,
                        "moved": False,
                        "static": False,
                        "context": None,
                        "table": "main_data",
                        "auto_update": True,
                        "freeze": False,
                        "filters": None,
                        "common_filter": None,
                        "metric": None,
                        "column_context": None,
                        "grouping": None,
                        "is_checkpoint": False,
                        "table_tile": {
                            "id": "table_123",
                            "tile_id": "123",
                            "table_type": "Data Table",
                            "page_number": 2,
                            "column_order": ["id", "name", "value", "new_column"],
                            "hidden_columns": ["id"],
                            "sorting": {"column": "name", "direction": "asc"},
                            "group_sorting": None,
                            "columns_pin_left": ["name"],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T13:00:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Tile not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Table tile not found"},
                },
            },
        },
        400: {
            "description": "Invalid tile type or missing parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Invalid tile_type. Must be one of Table, Plot, View, Editor",
                    },
                },
            },
        },
    },
)
async def patch_specialized_tile(
    tile_type: str = Query(
        ...,
        description="Type of tile to patch (Table, Plot, View, Editor)",
    ),
    tile_id: Optional[str] = None,
    tab_id: Optional[str] = None,
    name: Optional[str] = None,
    update_data: Dict[str, Any] = Body(...),
    tile_dao: TileDAO = Depends(),
):
    """
    Generic endpoint to patch any specialized tile type.

    The tile_type parameter determines which specialized tile type to update.
    Valid values are: Table, Plot, View, Editor

    For Table tiles, valid specialized fields include: table_type, column_context, page_number,
    column_order, hidden_columns, sorting, grouping, group_sorting, columns_pin_left,
    columns_pin_right, selected

    For Plot tiles, valid specialized fields include: plot_type, plot_scale_x, plot_scale_y,
    plot_aggregate, x_axis, y_axis, plot_group_by, plot_group_by_colors, bin_count, regression_line

    For View tiles, valid specialized fields include: base_index

    For Editor tiles, valid specialized fields include: file_path, file_type, content
    """
    # Validate the tile type
    valid_types = ["Table", "Plot", "View", "Editor"]
    if tile_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tile_type. Must be one of {', '.join(valid_types)}",
        )

    # Validate we have either tile_id or (tab_id and name)
    if not tile_id and (not tab_id or not name):
        raise HTTPException(
            status_code=400,
            detail="Must provide either tile_id or both tab_id and name",
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
        tile_type=tile_type,
    )

    if not result:
        raise HTTPException(status_code=404, detail=f"{tile_type} tile not found")

    return _create_tile_response(result)
