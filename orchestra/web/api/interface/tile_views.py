from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.db.dao.tile_dao import TileDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Tab, Tile
from orchestra.web.api.interface.schema import (
    CreateTileRequest,
    EditorTileSchema,
    ExportTileTemplateRequest,
    ImportTileTemplateRequest,
    PlotTileSchema,
    TableTileSchema,
    TemplateExportResponse,
    TemplateImportResponse,
    TerminalTileSchema,
    TileSchema,
    UpdateTileRequest,
    ViewTileSchema,
)
from orchestra.web.api.interface.template_utils import TemplateConverter

router = APIRouter(prefix="/tile", tags=["tile"])


def _create_tile_response(tile: Tile) -> TileSchema:
    """Helper function to convert a tile entity to a TileSchema with specialized tile data."""

    if tile is None:
        return

    # Create specialized tile data schemas if they exist
    table_tile_data = None
    plot_tile_data = None
    view_tile_data = None
    editor_tile_data = None
    terminal_tile_data = None

    if hasattr(tile, "table_tile") and tile.table_tile:
        table_tile_data = TableTileSchema(
            id=str(tile.table_tile.id),
            tile_id=str(tile.table_tile.tile_id),
            table_type=tile.table_tile.table_type,
            page_number=tile.table_tile.page_number,
            column_order=tile.table_tile.column_order,
            hidden_columns=tile.table_tile.hidden_columns,
            default_hidden_columns=tile.table_tile.default_hidden_columns,
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
            file_name=tile.editor_tile.file_name,
            file_type=tile.editor_tile.file_type,
            content=tile.editor_tile.content,
        )

    if hasattr(tile, "terminal_tile") and tile.terminal_tile:
        terminal_tile_data = TerminalTileSchema(
            id=str(tile.terminal_tile.id),
            tile_id=str(tile.terminal_tile.tile_id),
            shell_type=tile.terminal_tile.shell_type,
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
        minW=tile.minW,
        minH=tile.minH,
        visible=tile.visible,
        locked=tile.locked,
        moved=tile.moved,
        static=tile.static,
        color=tile.color,
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
        terminal_tile=terminal_tile_data,
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
    only_tile: bool = False,
) -> Tuple[Tile, Tab]:
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
        if not only_tile:
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

    if only_tile:
        return tile, None
    else:
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
                        "minW": 2,
                        "minH": 2,
                        "visible": True,
                        "locked": False,
                        "moved": False,
                        "static": False,
                        "color": None,
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
                            "default_hidden_columns": True,
                            "sorting": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "terminal_tile": None,
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
    request: CreateTileRequest,
    checkpoint: bool = Query(
        False,
        description="Whether to create a checkpoint tile (manual save)",
    ),
    session: Session = Depends(get_db_session),
):
    """Create a new tile in a tab."""
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # Get the tab, either directly by ID if provided in the request
    tab = None
    if hasattr(request, "tab_id") and request.tab_id:
        tab = tab_dao.get(request.tab_id)

    if not tab:
        raise HTTPException(
            status_code=404,
            detail=f"Tab not found. Please provide valid tab_id.",
        )

    # Validate context fields if provided (non-empty strings)
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)

    # Get the project ID from the tab's interface
    interface = interface_dao.get(tab.interface_id)
    if interface:
        project_obj = project_dao.get(interface.project_id)
        if project_obj:
            # Validate context field
            if request.context and request.context.strip():
                existing_contexts = context_dao.filter(
                    project_id=project_obj.id,
                    name=request.context,
                )
                if not existing_contexts:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Context '{request.context}' not found in project.",
                    )

    try:
        # Create tile with position from the request
        position_data = {
            "x_position": request.position.x,
            "y_position": request.position.y,
            "width": request.position.width,
            "height": request.position.height,
        }

        # Extract specialized tile data if available
        specialized_data = {}
        if request.type == "Table" and request.table_tile:
            specialized_data["table_tile"] = request.table_tile.model_dump(
                exclude_unset=True,
            )
        elif request.type == "Plot" and request.plot_tile:
            specialized_data["plot_tile"] = request.plot_tile.model_dump(
                exclude_unset=True,
            )
        elif request.type == "View" and request.view_tile:
            specialized_data["view_tile"] = request.view_tile.model_dump(
                exclude_unset=True,
            )
        elif request.type == "Editor" and request.editor_tile:
            specialized_data["editor_tile"] = request.editor_tile.model_dump(
                exclude_unset=True,
            )
        elif request.type == "Terminal" and request.terminal_tile:
            specialized_data["terminal_tile"] = request.terminal_tile.model_dump(
                exclude_unset=True,
            )

        # Create the tile with all data including specialized tile data
        tile_dao.create_tile(
            tile_id=getattr(request, "tile_id", None),
            tab_id=tab.id,
            name=request.name,
            type=request.type,
            minW=request.minW,
            minH=request.minH,
            visible=request.visible,
            locked=request.locked,
            moved=request.moved,
            static=request.static,
            color=request.color,
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
            **specialized_data,
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
                        "minW": 2,
                        "minH": 2,
                        "visible": True,
                        "locked": False,
                        "moved": False,
                        "static": False,
                        "color": None,
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
                            "default_hidden_columns": True,
                            "sorting": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "terminal_tile": None,
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
    session: Session = Depends(get_db_session),
):
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

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
                            "minW": 2,
                            "minH": 2,
                            "visible": True,
                            "locked": False,
                            "moved": False,
                            "static": False,
                            "color": None,
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
                                "default_hidden_columns": True,
                                "sorting": None,
                                "group_sorting": None,
                                "columns_pin_left": [],
                                "columns_pin_right": [],
                                "selected": None,
                            },
                            "plot_tile": None,
                            "view_tile": None,
                            "editor_tile": None,
                            "terminal_tile": None,
                            "created_at": "2024-01-01T12:00:00Z",
                            "updated_at": "2024-01-01T12:00:00Z",
                        },
                        {
                            "id": "124",
                            "tab_id": "tab_456",
                            "name": "Chart",
                            "type": "Plot",
                            "position": {"x": 6, "y": 0, "width": 6, "height": 4},
                            "minW": 2,
                            "minH": 2,
                            "visible": True,
                            "locked": False,
                            "moved": False,
                            "static": False,
                            "color": None,
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
                            "terminal_tile": None,
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
    session: Session = Depends(get_db_session),
):
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

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
                        "minW": 2,
                        "minH": 2,
                        "visible": True,
                        "locked": False,
                        "moved": True,
                        "static": False,
                        "color": None,
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
                            "default_hidden_columns": True,
                            "sorting": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "terminal_tile": None,
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
    session: Session = Depends(get_db_session),
):
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    """Update a tile by ID or by tab_id and name."""
    # Use helper function to get tile

    # Convert Pydantic model to dict, excluding unset fields
    update_dict = request.model_dump(exclude_unset=True)

    # Validate context fields if they're being updated
    if "context" in update_dict:
        from orchestra.db.dao.context_dao import ContextDAO
        from orchestra.db.dao.interface_dao import InterfaceDAO
        from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
        from orchestra.db.dao.project_dao import ProjectDAO

        organization_member_dao = OrganizationMemberDAO(session)
        context_dao = ContextDAO(session)
        project_dao = ProjectDAO(session, organization_member_dao, context_dao)
        interface_dao = InterfaceDAO(session)

        # Get the tile first to determine the project
        if tile_id:
            tile = tile_dao.get(tile_id, is_checkpoint=checkpoint)
            if not tile:
                raise HTTPException(
                    status_code=404,
                    detail=f"Tile with ID {tile_id} not found.",
                )
            tile_tab = tab_dao.get(tile.tab_id)
        else:
            tile, tile_tab = _get_tile(
                tile_id=tile_id,
                tab_id=tab_id,
                name=name,
                checkpoint=checkpoint,
                tab_dao=tab_dao,
                tile_dao=tile_dao,
            )

        if tile_tab:
            interface = interface_dao.get(tile_tab.interface_id)
            if interface:
                project_obj = project_dao.get(interface.project_id)
                if project_obj:
                    # Validate context field
                    if (
                        "context" in update_dict
                        and update_dict["context"]
                        and str(update_dict["context"]).strip()
                    ):
                        existing_contexts = context_dao.filter(
                            project_id=project_obj.id,
                            name=update_dict["context"],
                        )
                        if not existing_contexts:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Context '{update_dict['context']}' not found in project.",
                            )

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
                        "minW": 2,
                        "minH": 2,
                        "visible": True,
                        "locked": False,
                        "moved": False,
                        "static": False,
                        "color": None,
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
                            "default_hidden_columns": True,
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
                        "terminal_tile": None,
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
    session: Session = Depends(get_db_session),
):
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

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
                        "minW": 2,
                        "minH": 2,
                        "visible": True,
                        "locked": False,
                        "moved": False,
                        "static": False,
                        "color": None,
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
                            "default_hidden_columns": True,
                            "sorting": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "terminal_tile": None,
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
    session: Session = Depends(get_db_session),
):

    tile_dao = TileDAO(session)
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
    responses={
        200: {
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
    session: Session = Depends(get_db_session),
):
    tile_dao = TileDAO(session)
    """Delete a tile by ID or by tab_id and name."""
    # Use helper function to get tile with for_update=True to ensure we're deleting the active tile

    # Delete the tile
    if tile_id:
        success = tile_dao.delete_tile(id=tile_id)
    else:
        success = tile_dao.delete_tile(tab_id=tab_id, name=name)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete tile.")

    return {"info": "Tile deleted successfully!"}


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
                        "minW": 2,
                        "minH": 2,
                        "visible": True,
                        "locked": False,
                        "moved": True,
                        "static": False,
                        "color": None,
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
                            "default_hidden_columns": True,
                            "sorting": None,
                            "group_sorting": None,
                            "columns_pin_left": [],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "terminal_tile": None,
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
    session: Session = Depends(get_db_session),
):
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    """
    Partially update a tile by ID or by tab_id and name.

    Only the fields included in the request body will be updated.
    """
    # Use helper function to get tile
    tile, tab = _get_tile(
        tile_id=tile_id,
        tab_id=tab_id,
        name=name,
        checkpoint=checkpoint,
        tab_dao=tab_dao,
        tile_dao=tile_dao,
    )

    # Validate context fields if they're being updated
    if "context" in update_data:
        from orchestra.db.dao.context_dao import ContextDAO
        from orchestra.db.dao.interface_dao import InterfaceDAO
        from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
        from orchestra.db.dao.project_dao import ProjectDAO

        organization_member_dao = OrganizationMemberDAO(session)
        context_dao = ContextDAO(session)
        project_dao = ProjectDAO(session, organization_member_dao, context_dao)
        interface_dao = InterfaceDAO(session)

        interface = interface_dao.get(tab.interface_id)
        if interface:
            project_obj = project_dao.get(interface.project_id)
            if project_obj:
                # Validate context field
                if (
                    "context" in update_data
                    and update_data["context"]
                    and str(update_data["context"]).strip()
                ):
                    existing_contexts = context_dao.filter(
                        project_id=project_obj.id,
                        name=update_data["context"],
                    )
                    if not existing_contexts:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Context '{update_data['context']}' not found in project.",
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
                        "minW": 2,
                        "minH": 2,
                        "visible": True,
                        "locked": False,
                        "moved": False,
                        "static": False,
                        "color": None,
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
                            "default_hidden_columns": True,
                            "sorting": {"column": "name", "direction": "asc"},
                            "group_sorting": None,
                            "columns_pin_left": ["name"],
                            "columns_pin_right": [],
                            "selected": None,
                        },
                        "plot_tile": None,
                        "view_tile": None,
                        "editor_tile": None,
                        "terminal_tile": None,
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
                        "detail": "Invalid tile_type. Must be one of Table, Plot, View, Editor, Terminal",
                    },
                },
            },
        },
    },
)
async def patch_specialized_tile(
    tile_type: str = Query(
        ...,
        description="Type of tile to patch (Table, Plot, View, Editor, Terminal)",
    ),
    tile_id: Optional[str] = None,
    tab_id: Optional[str] = None,
    name: Optional[str] = None,
    update_data: Dict[str, Any] = Body(...),
    session: Session = Depends(get_db_session),
):
    """Update the specialized data for a specific tile type."""
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # Validate tile type
    valid_tile_types = ["Table", "Plot", "View", "Editor", "Terminal"]
    if tile_type not in valid_tile_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid tile_type. Must be one of {', '.join(valid_tile_types)}",
        )

    # Get the tile
    tile, _ = _get_tile(
        tile_id=tile_id,
        tab_id=tab_id,
        name=name,
        checkpoint=False,
        tab_dao=tab_dao,
        tile_dao=tile_dao,
    )

    # Update the specialized tile data
    try:
        updated_tile = tile_dao.patch_specialized_tile(
            id=str(tile.id),
            tile_type=tile_type,
            update_data=update_data,
        )

        if not updated_tile:
            raise HTTPException(
                status_code=404,
                detail=f"{tile_type} tile not found",
            )

        return _create_tile_response(updated_tile)

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update {tile_type.lower()} tile: {str(e)}",
        )


# Template Endpoints for Tiles
@router.post(
    "/export_template",
    response_model=TemplateExportResponse,
    responses={
        200: {
            "description": "Tile template exported successfully",
            "content": {
                "application/json": {
                    "example": {
                        "template": {
                            "name": "Data Table",
                            "type": "Table",
                            "position": {"x": 0, "y": 0, "width": 6, "height": 4},
                            "table_tile": {
                                "table_type": "Data Table",
                                "column_order": ["id", "name", "value"],
                            },
                        },
                        "metadata": {"exported_at": "2024-01-01T12:00:00Z"},
                        "export_stats": {"tiles": 1},
                    },
                },
            },
        },
    },
)
def export_tile_template(
    request_fastapi: Request,
    request: ExportTileTemplateRequest,
    session: Session = Depends(get_db_session),
):
    """Export a tile as a reusable template."""
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # Get the tile to export
    tile, _ = _get_tile(
        tile_id=request.tile_id,
        tab_id=request.tab_id,
        name=request.tile_name,
        checkpoint=request.checkpoint,
        tab_dao=tab_dao,
        tile_dao=tile_dao,
        only_tile=True,
    )

    # Convert to template
    template = TemplateConverter.tile_to_template(
        tile,
        description=request.description,
        created_by=request_fastapi.state.user_id,
        tags=request.tags,
    )

    # Create metadata
    from datetime import datetime, timezone

    metadata = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by": request_fastapi.state.user_id,
        "source_tab": request.tab_id,
        "template_name": request.template_name or tile.name,
    }

    # Calculate export stats
    export_stats = {
        "tiles": 1,
    }

    return TemplateExportResponse(
        template=template.model_dump(),
        metadata=metadata,
        export_stats=export_stats,
    )


@router.post(
    "/import_template",
    response_model=TemplateImportResponse,
    responses={
        200: {
            "description": "Tile template imported successfully",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "import_stats": {"tiles": 1},
                        "created_ids": {"tile_id": "ghi789"},
                        "warnings": [],
                    },
                },
            },
        },
    },
)
def import_tile_template(
    request_fastapi: Request,
    request: ImportTileTemplateRequest,
    session: Session = Depends(get_db_session),
):
    """Import a tile template into a tab."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # Get target tab
    tab = None
    if request.tab_id:
        tab = tab_dao.get(request.tab_id)
    elif request.interface_id and request.tab_name:
        tab = tab_dao.get_by_interface_and_name(
            interface_id=request.interface_id,
            name=request.tab_name,
            is_checkpoint=False,
        )
    elif request.interface_name and request.tab_name:
        # Get project first
        organization_id = getattr(request_fastapi.state, "organization_id", None)
        project = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=request.project_name,
            organization_id=organization_id,
        )
        if not project:
            raise HTTPException(
                status_code=404,
                detail=f"Project {request.project_name} not found or you don't have access.",
            )

        interface = interface_dao.get_by_project_and_name(
            project_id=project.id,
            name=request.interface_name,
            is_checkpoint=False,
        )
        if interface:
            tab = tab_dao.get_by_interface_and_name(
                interface_id=str(interface.id),
                name=request.tab_name,
                is_checkpoint=False,
            )

    if not tab:
        raise HTTPException(
            status_code=404,
            detail="Target tab not found.",
        )

    warnings = []

    # Determine tile name
    tile_name = request.new_tile_name or request.template.name or "Imported Tile"

    # Check for name conflicts
    existing_tile = tile_dao.get_by_tab_and_name(
        tab_id=str(tab.id),
        name=tile_name,
        is_checkpoint=False,
    )

    if existing_tile and not request.overwrite_existing:
        raise HTTPException(
            status_code=409,
            detail=f"Tile with name {tile_name} already exists. Use overwrite_existing=true to replace it.",
        )

    # If overwriting and tile exists, delete it first
    if existing_tile and request.overwrite_existing:
        tile_dao.delete_tile(id=str(existing_tile.id))
        warnings.append(f"Replaced existing tile '{tile_name}'")

    # Handle position
    position = request.template.position or {"x": 0, "y": 0, "width": 4, "height": 4}

    # Create the tile
    tile = tile_dao.create_tile(
        tab_id=str(tab.id),
        name=tile_name,
        type=request.template.type,
        x_position=(
            position.get("x", 0)
            if isinstance(position, dict)
            else getattr(position, "x", 0)
        ),
        y_position=(
            position.get("y", 0)
            if isinstance(position, dict)
            else getattr(position, "y", 0)
        ),
        width=(
            position.get("width", 4)
            if isinstance(position, dict)
            else getattr(position, "width", 4)
        ),
        height=(
            position.get("height", 4)
            if isinstance(position, dict)
            else getattr(position, "height", 4)
        ),
        minW=request.template.minW,
        minH=request.template.minH,
        visible=(
            request.template.visible if request.template.visible is not None else True
        ),
        locked=(
            request.template.locked if request.template.locked is not None else False
        ),
        moved=request.template.moved if request.template.moved is not None else False,
        static=(
            request.template.static if request.template.static is not None else False
        ),
        color=request.template.color,
        context=request.template.context,
        table=request.template.table,
        auto_update=request.template.auto_update,
        freeze=request.template.freeze,
        filters=request.template.filters,
        common_filter=request.template.common_filter,
        metric=request.template.metric,
        column_context=request.template.column_context,
        grouping=request.template.grouping,
        is_checkpoint=False,
        # Pass specialized tile data
        table_tile=(
            request.template.table_tile.model_dump()
            if request.template.table_tile
            else None
        ),
        plot_tile=(
            request.template.plot_tile.model_dump()
            if request.template.plot_tile
            else None
        ),
        view_tile=(
            request.template.view_tile.model_dump()
            if request.template.view_tile
            else None
        ),
        editor_tile=(
            request.template.editor_tile.model_dump()
            if request.template.editor_tile
            else None
        ),
        terminal_tile=(
            request.template.terminal_tile.model_dump()
            if request.template.terminal_tile
            else None
        ),
    )

    created_ids = {"tile_id": str(tile.id)}
    import_stats = {"tiles": 1}

    return TemplateImportResponse(
        success=True,
        validation_result=None,
        import_stats=import_stats,
        created_ids=created_ids,
        warnings=warnings,
    )
