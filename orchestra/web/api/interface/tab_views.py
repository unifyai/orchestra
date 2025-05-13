from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.db.dao.tile_dao import TileDAO
from orchestra.db.models.orchestra_models import Interface, Tab, Tile
from orchestra.web.api.interface.schema import (
    CreateTabRequest,
    TabSchema,
    UpdateTabRequest,
)

router = APIRouter(prefix="/tab", tags=["tab"])


def _create_tab_response(tab: Tab, tiles: Optional[List[Tile]] = None) -> TabSchema:
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


def _get_tab(
    tab_id: Optional[str],
    interface_id: Optional[str],
    name: Optional[str],
    checkpoint: bool,
    interface_dao: InterfaceDAO,
    tab_dao: TabDAO,
    for_update: bool = False,
) -> Tuple[Tab, Interface]:
    """Helper function to retrieve a tab by ID or by interface_id and name."""
    tab = None
    interface = None

    # Get by ID if provided
    if tab_id:
        tab = tab_dao.get(tab_id, is_checkpoint=checkpoint)
        if not tab:
            raise HTTPException(
                status_code=404,
                detail=f"Tab with ID {tab_id} not found.",
            )
        # Get interface to verify access
        interface = interface_dao.get(tab.interface_id)
        if not interface:
            raise HTTPException(
                status_code=404,
                detail=f"Interface with ID {tab.interface_id} not found.",
            )
    # Get by interface_id and name
    elif interface_id and name:
        # Get interface
        interface = interface_dao.get(interface_id)
        if not interface:
            raise HTTPException(
                status_code=404,
                detail=f"Interface with ID {interface_id} not found.",
            )

        # For specific operations like deletion, we need to get the active tab
        is_checkpoint = checkpoint
        if for_update and (checkpoint_operations := ["delete", "checkpoint"]):
            is_checkpoint = False

        # Get tab by interface_id and name
        tab = tab_dao.get_by_interface_and_name(
            interface_id=interface_id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if not tab:
            raise HTTPException(
                status_code=404,
                detail=f"Tab {name} not found in interface {interface_id}.",
            )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either tab_id or both interface_id and name must be provided.",
        )

    return tab, interface


@router.post(
    "/",
    response_model=TabSchema,
    status_code=201,
    responses={
        201: {
            "description": "Tab created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "interface_id": "456",
                        "name": "my_tab",
                        "visible": True,
                        "active": True,
                        "order": 1,
                        "global_context": {},
                        "color": "blue",
                        "is_checkpoint": False,
                        "tiles": [],
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:00:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Interface not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Interface not found. Please provide valid interface_id or project_id+interface_name.",
                    },
                },
            },
        },
        409: {
            "description": "Tab with this name already exists for this interface",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Tab with name my_tab already exists for this interface.",
                    },
                },
            },
        },
    },
)
def create_tab(
    request: CreateTabRequest,
    checkpoint: bool = Query(
        False,
        description="Whether to create a checkpoint tab (manual save)",
    ),
    tab_dao: TabDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Create a new tab."""
    # Get the interface, first by ID if provided in the request
    if not getattr(request, "interface_id", None):
        raise HTTPException(
            status_code=400,
            detail="Interface ID is required.",
        )

    interface = interface_dao.get(getattr(request, "interface_id"))

    if not interface:
        raise HTTPException(
            status_code=404,
            detail=f"Interface with ID {getattr(request, 'interface_id')} not found.",
        )

    # Check if tab already exists with the same name in this interface
    existing = tab_dao.get_by_interface_and_name(
        interface_id=interface.id,
        name=request.name,
        is_checkpoint=checkpoint,
    )

    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Tab with name {request.name} already exists for this interface.",
        )

    # Create the tab
    tab = tab_dao.create_tab(
        tab_id=getattr(request, "tab_id", None),
        interface_id=interface.id,
        name=request.name,
        visible=request.visible,
        active=request.active,
        order=request.order,
        global_context=request.global_context,
        color=request.color,
        is_checkpoint=checkpoint,
    )

    # Get all tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=tab.id, is_checkpoint=tab.is_checkpoint)

    return _create_tab_response(tab, tiles)


@router.get(
    "/",
    response_model=TabSchema,
    responses={
        200: {
            "description": "Tab details retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "interface_id": "456",
                        "name": "my_tab",
                        "visible": True,
                        "active": True,
                        "order": 1,
                        "global_context": {},
                        "color": "blue",
                        "is_checkpoint": False,
                        "tiles": [
                            {
                                "id": "789",
                                "tab_id": "123",
                                "name": "my_tile",
                                "type": "chart",
                                "config": {},
                                "is_checkpoint": False,
                                "created_at": "2024-01-01T12:00:00Z",
                                "updated_at": "2024-01-01T12:00:00Z",
                            },
                        ],
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
                    "example": {"detail": "Tab with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tab_id or both interface_id and name must be provided.",
                    },
                },
            },
        },
    },
)
def get_tab(
    tab_id: Optional[str] = Query(None, description="The ID of the tab to retrieve"),
    interface_id: Optional[str] = Query(
        None,
        description="The interface ID the tab belongs to",
    ),
    name: Optional[str] = Query(None, description="The name of the tab to retrieve"),
    checkpoint: bool = Query(
        False,
        description="Whether to get a checkpoint tab (manual save)",
    ),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Get a specific tab by ID or by interface_id and name."""
    # Use helper function to get tab
    tab, _ = _get_tab(
        tab_id=tab_id,
        interface_id=interface_id,
        name=name,
        checkpoint=checkpoint,
        interface_dao=interface_dao,
        tab_dao=tab_dao,
    )

    # Get all tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=tab.id, is_checkpoint=tab.is_checkpoint)

    return _create_tab_response(tab, tiles)


@router.get(
    "/list",
    response_model=List[TabSchema],
    responses={
        200: {
            "description": "Tabs list retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": "123",
                            "interface_id": "456",
                            "name": "my_tab_1",
                            "visible": True,
                            "active": True,
                            "order": 1,
                            "global_context": {},
                            "color": "blue",
                            "is_checkpoint": False,
                            "tiles": [],
                            "created_at": "2024-01-01T12:00:00Z",
                            "updated_at": "2024-01-01T12:00:00Z",
                        },
                        {
                            "id": "124",
                            "interface_id": "456",
                            "name": "my_tab_2",
                            "visible": True,
                            "active": False,
                            "order": 2,
                            "global_context": {},
                            "color": "green",
                            "is_checkpoint": False,
                            "tiles": [],
                            "created_at": "2024-01-01T12:00:00Z",
                            "updated_at": "2024-01-01T12:00:00Z",
                        },
                    ],
                },
            },
        },
        404: {
            "description": "Interface not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Interface with ID 456 not found."},
                },
            },
        },
    },
)
def list_tabs(
    interface_id: str = Query(..., description="The interface ID to list tabs for"),
    name: Optional[str] = Query(None, description="Filter tabs by name"),
    checkpoint: bool = Query(
        False,
        description="Whether to list checkpoint tabs (manual save)",
    ),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """List all tabs for an interface."""
    # Get interface
    interface = interface_dao.get(interface_id)
    if not interface:
        raise HTTPException(
            status_code=404,
            detail=f"Interface with ID {interface_id} not found.",
        )

    # Get tabs for this interface
    tabs = tab_dao.list_tabs(
        interface_id=interface_id,
        name=name,
        is_checkpoint=checkpoint,
    )

    result = []
    for tab in tabs:
        # Get tiles for each tab
        tiles = tile_dao.list_tiles_by_tab(
            tab_id=tab.id,
            is_checkpoint=tab.is_checkpoint,
        )
        result.append(_create_tab_response(tab, tiles))

    return result


@router.put(
    "/",
    response_model=TabSchema,
    responses={
        200: {
            "description": "Tab updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "interface_id": "456",
                        "name": "updated_tab_name",
                        "visible": True,
                        "active": True,
                        "order": 1,
                        "global_context": {},
                        "color": "red",
                        "is_checkpoint": False,
                        "tiles": [],
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:30:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Tab not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Tab with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tab_id or both interface_id and name must be provided.",
                    },
                },
            },
        },
    },
)
def update_tab(
    request: UpdateTabRequest,
    tab_id: Optional[str] = Query(None, description="The ID of the tab to update"),
    interface_id: Optional[str] = Query(
        None,
        description="The interface ID the tab belongs to",
    ),
    name: Optional[str] = Query(None, description="The name of the tab to update"),
    checkpoint: bool = Query(
        False,
        description="Whether this is a checkpoint update (manual save)",
    ),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Update a tab by ID or by interface_id and name."""
    # Use helper function to get tab

    # Convert Pydantic model to dict, excluding unset fields
    update_dict = request.model_dump(exclude_unset=True)

    # Update the tab
    if tab_id:
        updated = tab_dao.update_tab(id=tab_id, is_checkpoint=checkpoint, **update_dict)
    else:
        tab, _ = _get_tab(
            tab_id=tab_id,
            interface_id=interface_id,
            name=name,
            checkpoint=checkpoint,
            interface_dao=interface_dao,
            tab_dao=tab_dao,
        )
        updated = tab_dao.update_tab(
            id=tab.id,  # We already have the tab, so use its ID
            is_checkpoint=checkpoint,
            **update_dict,
        )

    # Get all tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(
        tab_id=updated.id,
        is_checkpoint=updated.is_checkpoint,
    )

    return _create_tab_response(updated, tiles)


@router.post(
    "/checkpoint",
    response_model=TabSchema,
    responses={
        200: {
            "description": "Tab checkpoint created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "789",
                        "interface_id": "456",
                        "name": "my_tab",
                        "visible": True,
                        "active": True,
                        "order": 1,
                        "global_context": {},
                        "color": "blue",
                        "is_checkpoint": True,
                        "tiles": [],
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:30:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Tab not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Tab with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tab_id or both interface_id and name must be provided.",
                    },
                },
            },
        },
        500: {
            "description": "Failed to create checkpoint",
            "content": {
                "application/json": {
                    "example": {"detail": "Failed to create tab checkpoint."},
                },
            },
        },
    },
)
def create_tab_checkpoint(
    tab_id: Optional[str] = Query(None, description="The ID of the tab to checkpoint"),
    interface_id: Optional[str] = Query(
        None,
        description="The interface ID the tab belongs to",
    ),
    name: Optional[str] = Query(None, description="The name of the tab to checkpoint"),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Create a manual checkpoint (save) of a tab and all its tiles."""
    # Use helper function to get tab with for_update=True to ensure we're operating on the active tab
    tab, interface = _get_tab(
        tab_id=tab_id,
        interface_id=interface_id,
        name=name,
        checkpoint=False,
        interface_dao=interface_dao,
        tab_dao=tab_dao,
        for_update=True,
    )

    # First ensure that the parent interface has a checkpoint
    checkpoint_interface = interface_dao.get_checkpoint(id=str(interface.id))

    if not checkpoint_interface:
        # If no checkpoint exists for the interface, create one
        checkpoint_interface = interface_dao.checkpoint_interface(
            interface_id=str(interface.id),
        )

        if not checkpoint_interface:
            raise HTTPException(
                status_code=500,
                detail="Failed to create checkpoint for parent interface.",
            )

    # Use the TabDAO checkpoint_tab method to handle the tab checkpointing
    checkpoint_tab = tab_dao.checkpoint_tab(
        tab_id=tab.id,
        target_interface_id=checkpoint_interface.id,
    )

    if not checkpoint_tab:
        raise HTTPException(status_code=500, detail="Failed to create tab checkpoint.")

    # Get tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=str(tab.id), is_checkpoint=False)

    # Create checkpoint tiles for each tile in the tab
    for tile in tiles:
        # Use the TileDAO checkpoint_tile method to handle tile checkpointing
        tile_dao.checkpoint_tile(
            tile_id=str(tile.id),
            target_tab_id=str(checkpoint_tab.id),
        )

    # Get tiles for the checkpoint tab to return
    checkpoint_tiles = tile_dao.list_tiles_by_tab(
        tab_id=str(checkpoint_tab.id),
        is_checkpoint=True,
    )

    # If we have a different number of tiles in the current tab comapred to the
    # checkpoint tab, we need to delete the extra tiles from the checkpoint tab
    if len(tiles) < len(checkpoint_tiles):
        # Delete any tiles in the checkpoint tab that are not in the current tab
        for checkpoint_tile in checkpoint_tiles:
            if not any(
                checkpoint_tile.id == tile.checkpoint_or_active_id for tile in tiles
            ):
                tile_dao.delete_tile(id=str(checkpoint_tile.id), is_checkpoint=True)

        # Get tiles for the checkpoint tab to return
        checkpoint_tiles = tile_dao.list_tiles_by_tab(
            tab_id=str(checkpoint_tab.id),
            is_checkpoint=True,
        )

    return _create_tab_response(checkpoint_tab, checkpoint_tiles)


@router.delete(
    "/",
    status_code=204,
    responses={
        204: {
            "description": "Tab deleted successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Tab deleted successfully"},
                },
            },
        },
        404: {
            "description": "Tab not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Tab with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tab_id or both interface_id and name must be provided.",
                    },
                },
            },
        },
        500: {
            "description": "Failed to delete tab",
            "content": {
                "application/json": {
                    "example": {"detail": "Failed to delete tab."},
                },
            },
        },
    },
)
def delete_tab(
    tab_id: Optional[str] = Query(None, description="The ID of the tab to delete"),
    interface_id: Optional[str] = Query(
        None,
        description="The interface ID the tab belongs to",
    ),
    name: Optional[str] = Query(None, description="The name of the tab to delete"),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Delete a tab by ID or by interface_id and name."""
    # Use helper function to get tab with for_update=True to ensure we're deleting the active tab
    tab, interface = _get_tab(
        tab_id=tab_id,
        interface_id=interface_id,
        name=name,
        checkpoint=False,
        interface_dao=interface_dao,
        tab_dao=tab_dao,
        for_update=True,
    )

    # First delete all tiles associated with this tab
    tiles = tile_dao.list_tiles_by_tab(tab_id=tab.id)
    for tile in tiles:
        tile_dao.delete_tile_by_name(tab_id=tab.id, name=tile.name)

    # Delete the tab
    if tab_id:
        success = tab_dao.delete_tab(id=tab_id)
    else:
        success = tab_dao.delete_tab(interface_id=interface.id, name=name)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete tab.")


@router.get(
    "/checkpoint",
    response_model=TabSchema,
    responses={
        200: {
            "description": "Tab checkpoint retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "789",
                        "interface_id": "456",
                        "name": "my_tab",
                        "visible": True,
                        "active": True,
                        "order": 1,
                        "global_context": {},
                        "color": "blue",
                        "is_checkpoint": True,
                        "tiles": [],
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:30:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Tab or checkpoint not found",
            "content": {
                "application/json": {
                    "example": {"detail": "No checkpoint found for the specified tab."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either tab_id or both interface_id and name must be provided.",
                    },
                },
            },
        },
    },
)
def get_tab_checkpoint(
    tab_id: Optional[str] = Query(
        None,
        description="The ID of the tab to get checkpoint for",
    ),
    interface_id: Optional[str] = Query(
        None,
        description="The interface ID the tab belongs to",
    ),
    name: Optional[str] = Query(
        None,
        description="The name of the tab to get checkpoint for",
    ),
    tab_dao: TabDAO = Depends(),
    tile_dao: TileDAO = Depends(),
):
    """Get the checkpoint (manual save) for a tab by ID or by interface_id and name."""
    # First get the active tab to use as a reference
    tab = None
    if tab_id:
        tab = tab_dao.get(id=tab_id)
        if not tab:
            raise HTTPException(
                status_code=404,
                detail=f"Tab with ID {tab_id} not found.",
            )
    elif interface_id and name:
        tab = tab_dao.get_by_interface_and_name(
            interface_id=interface_id,
            name=name,
            is_checkpoint=False,
        )
        if not tab:
            raise HTTPException(
                status_code=404,
                detail=f"Tab with name {name} not found in interface {interface_id}.",
            )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either tab_id or both interface_id and name must be provided.",
        )

    # Get the checkpoint version of this tab
    checkpoint_tab = tab_dao.get_checkpoint(id=tab.id)

    if not checkpoint_tab:
        raise HTTPException(
            status_code=404,
            detail="No checkpoint found for the specified tab.",
        )

    # Get tiles for this checkpoint tab
    checkpoint_tiles = tile_dao.list_tiles_by_tab(
        tab_id=checkpoint_tab.id,
        is_checkpoint=True,
    )

    return _create_tab_response(checkpoint_tab, checkpoint_tiles)
