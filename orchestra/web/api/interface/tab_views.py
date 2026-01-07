from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Interface, Tab, Tile
from orchestra.web.api.interface.schema import (
    CreateTabRequest,
    ExportTabTemplateRequest,
    ImportTabTemplateRequest,
    TabSchema,
    TemplateExportResponse,
    TemplateImportResponse,
    UpdateTabRequest,
)
from orchestra.web.api.interface.template_utils import (
    TemplateConverter,
    TemplateSanitizer,
    TemplateValidator,
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
        context=tab.context,
        color=tab.color,
        icon=tab.icon,
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
    only_tab: bool = False,
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
        if not only_tab:
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

    if only_tab:
        return tab, None
    else:
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
                        "context": {},
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
    session: Session = Depends(get_db_session),
):
    """Create a new tab."""
    tab_dao = TabDAO(session)
    interface_dao = InterfaceDAO(session)
    tile_dao = TileDAO(session)

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

    # Validate context if provided (non-empty string)
    if request.context and request.context.strip():
        organization_member_dao = OrganizationMemberDAO(session)
        context_dao = ContextDAO(session)
        project_dao = ProjectDAO(session, organization_member_dao, context_dao)

        # Get the project ID from the interface
        project_obj = project_dao.get(interface.project_id)
        if project_obj:
            existing_contexts = context_dao.filter(
                project_id=project_obj.id,
                name=request.context,
            )
            if not existing_contexts:
                raise HTTPException(
                    status_code=400,
                    detail=f"Context '{request.context}' not found in project.",
                )

    # Create the tab
    tab = tab_dao.create_tab(
        tab_id=getattr(request, "tab_id", None),
        interface_id=interface.id,
        name=request.name,
        visible=request.visible,
        active=request.active,
        order=request.order,
        context=request.context,
        color=request.color,
        is_checkpoint=checkpoint,
        icon=request.icon,
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
                        "context": {},
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
    session: Session = Depends(get_db_session),
):
    """Get a specific tab by ID or by interface_id and name."""
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

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
                            "context": {},
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
                            "context": {},
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
    session: Session = Depends(get_db_session),
):
    """List all tabs for an interface."""
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

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
                        "context": {},
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
    session: Session = Depends(get_db_session),
):
    """Update a tab by ID or by interface_id and name."""
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # Use helper function to get tab

    # Convert Pydantic model to dict, excluding unset fields
    update_dict = request.model_dump(exclude_unset=True)

    # Validate context if provided (non-empty string)
    if update_dict.get("context") and update_dict["context"].strip():
        organization_member_dao = OrganizationMemberDAO(session)
        context_dao = ContextDAO(session)
        project_dao = ProjectDAO(session, organization_member_dao, context_dao)

        # Get the tab first to determine the project
        if not tab_id:
            tab, _ = _get_tab(
                tab_id=tab_id,
                interface_id=interface_id,
                name=name,
                checkpoint=checkpoint,
                interface_dao=interface_dao,
                tab_dao=tab_dao,
            )
            tab_interface = interface_dao.get(tab.interface_id)
        else:
            tab = tab_dao.get(tab_id, is_checkpoint=checkpoint)
            if not tab:
                raise HTTPException(
                    status_code=404,
                    detail=f"Tab with ID {tab_id} not found.",
                )
            tab_interface = interface_dao.get(tab.interface_id)

        if tab_interface:
            project_obj = project_dao.get(tab_interface.project_id)
            if project_obj:
                existing_contexts = context_dao.filter(
                    project_id=project_obj.id,
                    name=update_dict["context"],
                )
                if not existing_contexts:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Context '{update_dict['context']}' not found in project.",
                    )

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
                        "context": {},
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
    session: Session = Depends(get_db_session),
):
    """Create a manual checkpoint (save) of a tab and all its tiles."""
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

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
    responses={
        200: {
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
    session: Session = Depends(get_db_session),
):
    """Delete a tab by ID or by interface_id and name."""
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

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
        tile_dao.delete_tile(tab_id=tab.id, name=tile.name)

    # Delete the tab
    if tab_id:
        success = tab_dao.delete_tab(id=tab_id)
    else:
        success = tab_dao.delete_tab(interface_id=interface.id, name=name)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete tab.")

    return {"info": "Tab deleted successfully!"}


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
                        "context": {},
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
    session: Session = Depends(get_db_session),
):
    """Get the checkpoint (manual save) for a tab by ID or by interface_id and name."""
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

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


# Template Endpoints for Tabs
@router.post(
    "/export_template",
    response_model=TemplateExportResponse,
    responses={
        200: {
            "description": "Tab template exported successfully",
            "content": {
                "application/json": {
                    "example": {
                        "template": {
                            "name": "Overview Tab",
                            "tiles": [
                                {
                                    "name": "Data Table",
                                    "type": "Table",
                                    "position": {
                                        "x": 0,
                                        "y": 0,
                                        "width": 6,
                                        "height": 4,
                                    },
                                },
                            ],
                        },
                        "metadata": {"exported_at": "2024-01-01T12:00:00Z"},
                        "export_stats": {"tabs": 1, "tiles": 1},
                    },
                },
            },
        },
    },
)
def export_tab_template(
    request_fastapi: Request,
    request: ExportTabTemplateRequest,
    session: Session = Depends(get_db_session),
):
    """Export a tab as a reusable template."""
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # Get the tab to export
    tab, _ = _get_tab(
        tab_id=request.tab_id,
        interface_id=request.interface_id,
        name=request.tab_name,
        checkpoint=request.checkpoint,
        interface_dao=interface_dao,
        tab_dao=tab_dao,
        only_tab=True,
    )

    # Get tiles for this tab
    tiles = tile_dao.list_tiles_by_tab(
        tab_id=tab.id,
        is_checkpoint=tab.is_checkpoint,
    )

    # Ensure tab has its tiles loaded
    tab.tiles = tiles

    # Convert to template
    template = TemplateConverter.tab_to_template(
        tab,
        description=request.description,
        created_by=request_fastapi.state.user_id,
        tags=request.tags,
    )

    # Create metadata
    from datetime import datetime, timezone

    metadata = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by": request_fastapi.state.user_id,
        "source_interface": request.interface_id,
        "template_name": request.template_name or tab.name,
    }

    # Calculate export stats
    export_stats = {
        "tabs": 1,
        "tiles": len(template.tiles),
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
            "description": "Tab template imported successfully",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "import_stats": {"tabs": 1, "tiles": 3},
                        "created_ids": {"tab_id": "def456"},
                        "warnings": [],
                    },
                },
            },
        },
    },
)
def import_tab_template(
    request_fastapi: Request,
    request: ImportTabTemplateRequest,
    session: Session = Depends(get_db_session),
):
    """Import a tab template into an interface."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # Get target interface
    interface = None
    if request.interface_id:
        interface = interface_dao.get(request.interface_id)
    elif request.interface_name:
        # Get project first
        organization_id = getattr(request_fastapi.state, "organization_id", None)
        project = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=request.project,
            organization_id=organization_id,
        )
        if not project:
            raise HTTPException(
                status_code=404,
                detail=f"Project {request.project} not found or you don't have access.",
            )

        interface = interface_dao.get_by_project_and_name(
            project_id=project.id,
            name=request.interface_name,
            is_checkpoint=False,
        )

    if not interface:
        raise HTTPException(
            status_code=404,
            detail="Target interface not found.",
        )

    validation_result = None
    warnings = []

    # Validate template if requested
    if request.validate_first:
        # Get project for validation
        project = project_dao.get(interface.project_id)
        organization_id = getattr(request_fastapi.state, "organization_id", None)
        validator = TemplateValidator(session)
        validation_schema = validator.get_project_validation_schema(
            user_id=request_fastapi.state.user_id,
            project_name=project.name,
            organization_id=organization_id,
        )

        # Create a minimal interface template for validation
        interface_template = {
            "name": "temp",
            "tabs": [request.template],
        }

        validation_result = validator.validate_interface_template(
            interface_template=interface_template,
            validation_schema=validation_schema,
        )

        # Auto-sanitize if requested and there are issues
        if request.auto_sanitize and not validation_result.is_valid:
            sanitizer = TemplateSanitizer(validation_schema)
            sanitized_interface = sanitizer.sanitize_interface_template(
                interface_template=interface_template,
                remove_invalid=True,
                preserve_structure=True,
            )
            if sanitized_interface.get("tabs"):
                request.template = sanitized_interface["tabs"][0]
            warnings.append("Template was automatically sanitized")

    # Determine tab name
    tab_name = request.new_tab_name or request.template.name or "Imported Tab"

    # Check for name conflicts
    existing_tab = tab_dao.get_by_interface_and_name(
        interface_id=str(interface.id),
        name=tab_name,
        is_checkpoint=False,
    )

    if existing_tab and not request.overwrite_existing:
        raise HTTPException(
            status_code=409,
            detail=f"Tab with name {tab_name} already exists. Use overwrite_existing=true to replace it.",
        )

    # If overwriting and tab exists, delete it first
    if existing_tab and request.overwrite_existing:
        tab_dao.delete_tab(id=str(existing_tab.id))
        warnings.append(f"Replaced existing tab '{tab_name}'")

    # Create the tab
    tab = tab_dao.create_tab(
        interface_id=str(interface.id),
        name=tab_name,
        visible=(
            request.template.visible if request.template.visible is not None else True
        ),
        active=(
            request.template.active if request.template.active is not None else False
        ),
        order=request.template.order if request.template.order is not None else 0,
        context=request.template.context,
        color=request.template.color,
        is_checkpoint=False,
    )

    created_ids = {"tab_id": str(tab.id)}
    import_stats = {"tabs": 1, "tiles": 0}

    # Create tiles for this tab
    for tile_data in request.template.tiles:
        position = tile_data.position or {"x": 0, "y": 0, "width": 4, "height": 4}

        tile = tile_dao.create_tile(
            tab_id=str(tab.id),
            name=tile_data.name or "Imported Tile",
            type=tile_data.type,
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
            minW=tile_data.minW,
            minH=tile_data.minH,
            visible=tile_data.visible if tile_data.visible is not None else True,
            locked=tile_data.locked if tile_data.locked is not None else False,
            moved=tile_data.moved if tile_data.moved is not None else False,
            static=tile_data.static if tile_data.static is not None else False,
            color=tile_data.color,
            context=tile_data.context,
            table=tile_data.table,
            auto_update=tile_data.auto_update,
            freeze=tile_data.freeze,
            filters=tile_data.filters,
            common_filter=tile_data.common_filter,
            metric=tile_data.metric,
            column_context=tile_data.column_context,
            grouping=tile_data.grouping,
            is_checkpoint=False,
            # Pass specialized tile data
            table_tile=(
                tile_data.table_tile.model_dump() if tile_data.table_tile else None
            ),
            plot_tile=tile_data.plot_tile.model_dump() if tile_data.plot_tile else None,
            view_tile=tile_data.view_tile.model_dump() if tile_data.view_tile else None,
            editor_tile=(
                tile_data.editor_tile.model_dump() if tile_data.editor_tile else None
            ),
            terminal_tile=(
                tile_data.terminal_tile.model_dump()
                if tile_data.terminal_tile
                else None
            ),
        )
        import_stats["tiles"] += 1

    return TemplateImportResponse(
        success=True,
        validation_result=validation_result,
        import_stats=import_stats,
        created_ids=created_ids,
        warnings=warnings,
    )
