from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.db.dao.tile_dao import TileDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Interface, Project, Tab
from orchestra.web.api.interface.schema import (
    CreateInterfaceRequest,
    ExportInterfaceTemplateRequest,
    ImportInterfaceTemplateRequest,
    InterfaceSchema,
    InterfaceTemplateSchema,
    SanitizeInterfaceTemplateRequest,
    TemplateExportResponse,
    TemplateImportResponse,
    UpdateInterfaceRequest,
    ValidateInterfaceTemplateRequest,
    ValidationResultSchema,
)
from orchestra.web.api.interface.template_utils import (
    TemplateConverter,
    TemplateSanitizer,
    TemplateValidator,
)

router = APIRouter(prefix="/interfaces", tags=["interfaces"])


def _create_interface_response(
    interface: Interface,
    tabs: Optional[List[Tab]] = None,
    session: Optional[Session] = None,
) -> InterfaceSchema:
    """Helper function to convert an interface entity to an InterfaceSchema with optional tabs."""

    tab_list = []
    if tabs:
        # Initialize TileDAO if session is provided and tiles need to be loaded
        tile_dao = None
        if session:
            tile_dao = TileDAO(session)

        # Format tabs into TabSchema objects
        for tab in tabs:
            # Load tiles for this tab if session is provided
            tiles = None
            if tile_dao:
                tiles = tile_dao.list_tiles_by_tab(
                    tab_id=str(tab.id),
                    is_checkpoint=tab.is_checkpoint,
                )
            # If tiles are already loaded on the tab object, use those
            elif hasattr(tab, "tiles") and tab.tiles:
                tiles = tab.tiles

            # This would call the equivalent function in tab_views.py
            from orchestra.web.api.interface.tab_views import _create_tab_response

            tab_list.append(_create_tab_response(tab, tiles))

    return InterfaceSchema(
        id=str(interface.id),
        name=interface.name,
        project_id=interface.project_id,
        tabs=tab_list,
        active_tab_id=str(interface.active_tab_id) if interface.active_tab_id else None,
        color=interface.color,
        is_checkpoint=interface.is_checkpoint,
        created_at=interface.created_at.isoformat() if interface.created_at else None,
        updated_at=interface.updated_at.isoformat() if interface.updated_at else None,
    )


def _get_interface(
    request_fastapi: Request,
    interface_id: Optional[str],
    project: Optional[str],
    name: Optional[str],
    checkpoint: bool,
    project_dao: ProjectDAO,
    interface_dao: InterfaceDAO,
    for_update: bool = False,
    only_interface: bool = False,
) -> Tuple[Interface, Project]:
    """Helper function to retrieve an interface by ID or by project and name.

    Args:
        request_fastapi: The FastAPI request object.
        interface_id: Optional ID of the interface to retrieve.
        project: Optional project name the interface belongs to.
        name: Optional name of the interface to retrieve.
        checkpoint: Whether to get a checkpoint version.
        project_dao: Project DAO dependency.
        interface_dao: Interface DAO dependency.
        for_update: Whether this is for an update/delete operation (affects checkpoint flag).

    Returns:
        Tuple of (interface, project_obj)

    Raises:
        HTTPException: If interface not found or parameters are invalid.
    """
    interface = None
    project_obj = None

    # Get by ID if provided
    if interface_id:
        interface = interface_dao.get(interface_id, is_checkpoint=checkpoint)
        if not interface:
            raise HTTPException(
                status_code=404,
                detail=f"Interface with ID {interface_id} not found.",
            )
        if not only_interface:
            # Get project to verify access
            project_obj = project_dao.get(interface.project_id)
            if not project_obj:
                raise HTTPException(
                    status_code=404,
                    detail=f"Project with ID {interface.project_id} not found.",
                )
    # Get by project and name
    elif project and name:
        # Verify project exists and user has access
        project_obj = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=project,
        )
        if not project_obj:
            raise HTTPException(
                status_code=404,
                detail=f"Project {project} not found or you don't have access.",
            )

        # For specific operations like deletion, we need to get the active interface
        is_checkpoint = checkpoint
        if for_update and (checkpoint_operations := ["delete", "checkpoint"]):
            is_checkpoint = False

        # Get interface by project and name
        interface = interface_dao.get_by_project_and_name(
            project_id=project_obj.id,
            name=name,
            is_checkpoint=is_checkpoint,
        )

        if not interface:
            raise HTTPException(
                status_code=404,
                detail=f"Interface {name} not found in project {project}.",
            )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either interface_id or both project and name must be provided.",
        )

    if only_interface:
        return interface, None
    else:
        return interface, project_obj


@router.post(
    "/",
    response_model=InterfaceSchema,
    status_code=201,
    responses={
        201: {
            "description": "Interface created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "name": "my_interface",
                        "project_id": "proj_abc",
                        "tabs": [],
                        "active_tab_id": None,
                        "color": "blue",
                        "is_checkpoint": False,
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:00:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Project not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project my_project not found or you don't have access.",
                    },
                },
            },
        },
        409: {
            "description": "Interface with this name already exists",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Interface with name my_interface already exists in this project.",
                    },
                },
            },
        },
    },
)
def create_interface(
    request_fastapi: Request,
    request: CreateInterfaceRequest,
    checkpoint: bool = Query(
        False,
        description="Whether to create a checkpoint interface (manual save)",
    ),
    session: Session = Depends(get_db_session),
):
    """Create a new interface in a project."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)

    # Verify project exists and user has access
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=request.project,  # Assuming project is the name for now
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project} not found or you don't have access.",
        )

    # Check if interface already exists
    existing = interface_dao.get_by_project_and_name(
        project.id,
        request.name,
        is_checkpoint=checkpoint,
    )

    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Interface with name {request.name} already exists in this project.",
        )

    # Create the interface
    interface = interface_dao.create_interface(
        name=request.name,
        project_id=project.id,
        color=request.color,
        is_checkpoint=checkpoint,
    )

    # Get tabs for this interface
    tabs = tab_dao.list_tabs(
        interface_id=str(interface.id),
        is_checkpoint=interface.is_checkpoint,
    )

    return _create_interface_response(interface, tabs, session)


@router.get(
    "/",
    response_model=InterfaceSchema,
    responses={
        200: {
            "description": "Interface details retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "name": "my_interface",
                        "project_id": "proj_abc",
                        "tabs": [],
                        "active_tab_id": None,
                        "color": "blue",
                        "is_checkpoint": False,
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
                    "example": {"detail": "Interface with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either interface_id or both project and name must be provided.",
                    },
                },
            },
        },
    },
)
def get_interface(
    request_fastapi: Request,
    interface_id: Optional[str] = Query(
        None,
        description="The ID of the interface to retrieve",
    ),
    project: Optional[str] = Query(
        None,
        description="The project ID the interface belongs to",
    ),
    name: Optional[str] = Query(
        None,
        description="The name of the interface to retrieve",
    ),
    checkpoint: bool = Query(
        False,
        description="Whether to get a checkpoint version (manually saved)",
    ),
    session: Session = Depends(get_db_session),
):
    """Get a specific interface by ID or by project ID and name."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)

    # Use helper function to get interface
    interface, _ = _get_interface(
        request_fastapi=request_fastapi,
        interface_id=interface_id,
        project=project,
        name=name,
        checkpoint=checkpoint,
        project_dao=project_dao,
        interface_dao=interface_dao,
    )

    # Get tabs for this interface
    tabs = tab_dao.list_tabs(
        interface_id=interface.id,
        is_checkpoint=interface.is_checkpoint,
    )

    return _create_interface_response(interface, tabs, session)


@router.get(
    "/list",
    response_model=List[InterfaceSchema],
    responses={
        200: {
            "description": "Interfaces list retrieved successfully",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "id": "123",
                            "name": "my_interface",
                            "project_id": "proj_abc",
                            "tabs": [],
                            "active_tab_id": None,
                            "color": "blue",
                            "is_checkpoint": False,
                            "created_at": "2024-01-01T12:00:00Z",
                            "updated_at": "2024-01-01T12:00:00Z",
                        },
                    ],
                },
            },
        },
        404: {
            "description": "Project not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project my_project not found or you don't have access.",
                    },
                },
            },
        },
    },
)
def list_interfaces(
    request_fastapi: Request,
    project: str = Query(..., description="The project ID to list interfaces for"),
    checkpoint: bool = Query(
        False,
        description="Whether to list checkpoint versions (manually saved)",
    ),
    session: Session = Depends(get_db_session),
):
    """List all interfaces for a project."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)

    # Verify project exists and user has access
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found or you don't have access.",
        )

    # Get interfaces
    interfaces = interface_dao.get_interfaces(
        project_id=project.id,
        is_checkpoint=checkpoint,
    )

    result = []
    for interface in interfaces:
        # Get tabs for this interface
        tabs = tab_dao.list_tabs(
            interface_id=interface.id,
            is_checkpoint=interface.is_checkpoint,
        )

        result.append(_create_interface_response(interface, tabs, session))

    return result


@router.put(
    "/",
    response_model=InterfaceSchema,
    responses={
        200: {
            "description": "Interface updated successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "name": "my_interface",
                        "project_id": "proj_abc",
                        "tabs": [],
                        "active_tab_id": None,
                        "color": "blue",
                        "is_checkpoint": False,
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
                    "example": {"detail": "Interface with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either interface_id or both project and name must be provided.",
                    },
                },
            },
        },
    },
)
def update_interface(
    request_fastapi: Request,
    request: UpdateInterfaceRequest,
    interface_id: Optional[str] = Query(
        None,
        description="The ID of the interface to update",
    ),
    project: Optional[str] = Query(
        None,
        description="The project ID the interface belongs to",
    ),
    name: Optional[str] = Query(
        None,
        description="The name of the interface to update",
    ),
    checkpoint: bool = Query(
        False,
        description="Whether this is a checkpoint update (manual save)",
    ),
    session: Session = Depends(get_db_session),
):
    """Update an interface by ID or by project ID and name."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)

    project_obj = None
    interface = None

    # Get by ID if provided - ID takes precedence over project+name
    if interface_id:
        interface = interface_dao.get(interface_id)
        if not interface:
            raise HTTPException(
                status_code=404,
                detail=f"Interface with ID {interface_id} not found.",
            )
        # Get project to verify access
        project_obj = project_dao.get(interface.project_id)
        if not project_obj:
            raise HTTPException(
                status_code=404,
                detail=f"Project with ID {interface.project_id} not found.",
            )
    # Get by project and name
    elif project and name:
        # Verify project exists and user has access
        project_obj = project_dao.get_by_user_and_name(
            user_id=request_fastapi.state.user_id,
            name=project,
        )
        if not project_obj:
            raise HTTPException(
                status_code=404,
                detail=f"Project {project} not found or you don't have access.",
            )

        # Check if interface with the specified checkpoint status exists
        interface = interface_dao.get_by_project_and_name(
            project_id=project_obj.id,
            name=name,
            is_checkpoint=checkpoint,
        )

        # For updates, we need to handle the case where the interface with the given
        # checkpoint status might not exist yet (we'll create it in that case)
        if not interface and not checkpoint:
            # If non-checkpoint interface doesn't exist, that's an error
            raise HTTPException(
                status_code=404,
                detail=f"Interface {name} not found in project {project}.",
            )
        elif not interface and checkpoint:
            # If checkpoint version doesn't exist but regular version does,
            # get the regular version to create a checkpoint from it
            regular_interface = interface_dao.get_by_project_and_name(
                project_id=project_obj.id,
                name=name,
                is_checkpoint=False,
            )

            if not regular_interface:
                raise HTTPException(
                    status_code=404,
                    detail=f"Interface {name} not found in project {project}.",
                )

            # Create a new checkpoint version based on the regular interface
            interface = interface_dao.create_interface(
                name=regular_interface.name,
                project_id=regular_interface.project_id,
                items=regular_interface.items,
                new_counter=regular_interface.new_counter,
                context=regular_interface.context,
                color=regular_interface.color,
                active_tab_id=regular_interface.active_tab_id,
                is_checkpoint=True,
            )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either interface_id or both project and name must be provided.",
        )

    # Convert Pydantic model to dict
    update_dict = request.model_dump()

    # Verify that the tab exists and belongs to this interface if active_tab_id is being updated
    if update_dict.get("active_tab_id"):
        tab = tab_dao.get(update_dict["active_tab_id"])
        if not tab or tab.interface_id != interface.id:
            raise HTTPException(
                status_code=404,
                detail=f"Tab {update_dict['active_tab_id']} not found or doesn't belong to this interface.",
            )

    # Update the interface
    if interface_id:
        updated = interface_dao.update_interface(id=interface_id, **update_dict)
    else:
        updated = interface_dao.update_interface(
            id=interface.id,  # We already have the interface, so use its ID
            **update_dict,
        )

    # Get tabs for this interface
    tabs = tab_dao.list_tabs(
        interface_id=updated.id,
        is_checkpoint=updated.is_checkpoint,
    )

    return _create_interface_response(updated, tabs, session)


@router.post(
    "/checkpoint",
    response_model=InterfaceSchema,
    responses={
        200: {
            "description": "Interface checkpoint created successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "name": "my_interface",
                        "project_id": "proj_abc",
                        "tabs": [],
                        "active_tab_id": None,
                        "color": "blue",
                        "is_checkpoint": True,
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
                    "example": {"detail": "Interface with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either interface_id or both project and name must be provided.",
                    },
                },
            },
        },
        500: {
            "description": "Failed to create checkpoint",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Failed to create or update checkpoint interface.",
                    },
                },
            },
        },
    },
)
def create_interface_checkpoint(
    request_fastapi: Request,
    interface_id: Optional[str] = Query(
        None,
        description="The ID of the interface to checkpoint",
    ),
    project: Optional[str] = Query(
        None,
        description="The project ID the interface belongs to",
    ),
    name: Optional[str] = Query(
        None,
        description="The name of the interface to checkpoint",
    ),
    session: Session = Depends(get_db_session),
):
    """Create a manual checkpoint (save) of an interface by ID or by project ID and name."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)

    # Get the active interface first
    interface, project_obj = _get_interface(
        request_fastapi=request_fastapi,
        interface_id=interface_id,
        project=project,
        name=name,
        checkpoint=False,  # Always get the active interface
        project_dao=project_dao,
        interface_dao=interface_dao,
        for_update=True,
    )

    # Use the InterfaceDAO checkpoint_interface method to handle the checkpointing
    if interface_id:
        checkpoint_interface = interface_dao.checkpoint_interface(
            interface_id=interface_id,
        )
    else:
        checkpoint_interface = interface_dao.checkpoint_interface(
            project_id=project_obj.id,
            name=name,
        )

    # Verify the checkpoint interface exists
    if not checkpoint_interface:
        raise HTTPException(
            status_code=500,
            detail="Failed to create or update checkpoint interface.",
        )

    # Get tabs for the active interface
    tabs = tab_dao.list_tabs(
        interface_id=str(interface.id),
        is_checkpoint=False,  # Ensure ID is string
    )

    # Create or update checkpoint tabs using the TabDAO checkpoint_tab method
    for tab in tabs:
        # Use the TabDAO checkpoint_tab method to handle the tab checkpointing
        tab_dao.checkpoint_tab(
            tab_id=str(tab.id),
            target_interface_id=str(checkpoint_interface.id),
        )

    # Get all tabs for the checkpoint interface to return
    checkpoint_tabs = tab_dao.list_tabs(
        interface_id=str(checkpoint_interface.id),  # Ensure ID is string
        is_checkpoint=True,
    )

    # If we have a different number of tabs in the current tab compared to the
    # checkpoint tab, we need to delete the extra tabs from the checkpoint tab
    if len(tabs) < len(checkpoint_tabs):
        # Delete any tabs in the checkpoint tab that are not in the current tab
        for checkpoint_tab in checkpoint_tabs:
            if not any(
                checkpoint_tab.id == tab.checkpoint_or_active_id for tab in tabs
            ):
                tab_dao.delete_tab(id=str(checkpoint_tab.id), is_checkpoint=True)

        # Get tabs for the checkpoint tab to return
        checkpoint_tabs = tab_dao.list_tabs(
            interface_id=str(checkpoint_interface.id),
            is_checkpoint=True,
        )

    return _create_interface_response(checkpoint_interface, checkpoint_tabs, session)


@router.get(
    "/checkpoint",
    response_model=InterfaceSchema,
    responses={
        200: {
            "description": "Interface checkpoint retrieved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "id": "123",
                        "name": "my_interface",
                        "project_id": "proj_abc",
                        "tabs": [],
                        "active_tab_id": None,
                        "color": "blue",
                        "is_checkpoint": True,
                        "created_at": "2024-01-01T12:00:00Z",
                        "updated_at": "2024-01-01T12:00:00Z",
                    },
                },
            },
        },
        404: {
            "description": "Interface or checkpoint not found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "No checkpoint found for the specified interface.",
                    },
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either interface_id or both project and name must be provided.",
                    },
                },
            },
        },
    },
)
def get_interface_checkpoint(
    request_fastapi: Request,
    interface_id: Optional[str] = Query(
        None,
        description="The ID of the interface to get checkpoint for",
    ),
    project: Optional[str] = Query(
        None,
        description="The project ID the interface belongs to",
    ),
    name: Optional[str] = Query(
        None,
        description="The name of the interface to get checkpoint for",
    ),
    session: Session = Depends(get_db_session),
):
    """Get the latest checkpoint (manual save) for an interface by ID or by project ID and name."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)

    # Use helper function to get interface with for_update=True to ensure we're looking at the active interface

    # Find the latest checkpoint
    if interface_id:
        checkpoint = interface_dao.get_checkpoint(id=interface_id)
    else:
        _, project_obj = _get_interface(
            request_fastapi=request_fastapi,
            interface_id=interface_id,
            project=project,
            name=name,
            checkpoint=False,
            project_dao=project_dao,
            interface_dao=interface_dao,
            for_update=True,
        )
        checkpoint = interface_dao.get_checkpoint(
            project_id=project_obj.id,
            name=name,
        )

    if not checkpoint:
        raise HTTPException(
            status_code=404,
            detail=f"No checkpoint found for the specified interface.",
        )

    # Get tabs for this checkpoint interface
    tabs = tab_dao.list_tabs(interface_id=checkpoint.id, is_checkpoint=True)

    return _create_interface_response(checkpoint, tabs, session)


@router.delete(
    "/",
    responses={
        200: {
            "description": "Interface deleted successfully",
            "content": {
                "application/json": {
                    "example": {"info": "Interface deleted successfully"},
                },
            },
        },
        404: {
            "description": "Interface not found",
            "content": {
                "application/json": {
                    "example": {"detail": "Interface with ID 123 not found."},
                },
            },
        },
        400: {
            "description": "Missing required parameters",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Either interface_id or both project and name must be provided.",
                    },
                },
            },
        },
        500: {
            "description": "Failed to delete interface",
            "content": {
                "application/json": {
                    "example": {"detail": "Failed to delete interface."},
                },
            },
        },
    },
)
def delete_interface(
    request_fastapi: Request,
    interface_id: Optional[str] = Query(
        None,
        description="The ID of the interface to delete",
    ),
    project: Optional[str] = Query(
        None,
        description="The project ID the interface belongs to",
    ),
    name: Optional[str] = Query(
        None,
        description="The name of the interface to delete",
    ),
    session: Session = Depends(get_db_session),
):
    """Delete an interface by ID or by project ID and name."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)

    # Use helper function to get interface with for_update=True to ensure we're deleting the active interface
    interface, project_obj = _get_interface(
        request_fastapi=request_fastapi,
        interface_id=interface_id,
        project=project,
        name=name,
        checkpoint=False,
        project_dao=project_dao,
        interface_dao=interface_dao,
        for_update=True,
    )

    # First delete all tabs associated with this interface
    tabs = tab_dao.list_tabs(interface_id=interface.id)
    for tab in tabs:
        tab_dao.delete_tab(interface_id=interface.id, name=tab.name)

    # Delete the interface
    if interface_id:
        success = interface_dao.delete_interface(id=interface_id)
    else:
        success = interface_dao.delete_interface(project_id=project_obj.id, name=name)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete interface.")

    return {"info": "Interface deleted successfully!"}


# Template Endpoints
@router.post(
    "/export_template",
    response_model=TemplateExportResponse,
    responses={
        200: {
            "description": "Interface template exported successfully",
            "content": {
                "application/json": {
                    "example": {
                        "template": {
                            "name": "Analytics Dashboard",
                            "tabs": [
                                {
                                    "name": "Overview",
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
                            ],
                            "template_version": "1.0",
                        },
                        "metadata": {"exported_at": "2024-01-01T12:00:00Z"},
                        "export_stats": {"tabs": 1, "tiles": 1},
                    },
                },
            },
        },
    },
)
def export_interface_template(
    request_fastapi: Request,
    request: ExportInterfaceTemplateRequest,
    session: Session = Depends(get_db_session),
):
    """Export an interface as a reusable template."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # Get the interface to export
    interface, _ = _get_interface(
        request_fastapi=request_fastapi,
        interface_id=request.interface_id,
        project=request.project,
        name=request.interface_name,
        checkpoint=request.checkpoint,
        project_dao=project_dao,
        interface_dao=interface_dao,
        only_interface=True,
    )

    # Get tabs with tiles for this interface
    tabs = tab_dao.list_tabs(
        interface_id=interface.id,
        is_checkpoint=interface.is_checkpoint,
    )

    for tab in tabs:
        tab.tiles = tile_dao.list_tiles_by_tab(
            tab_id=str(tab.id),
            is_checkpoint=tab.is_checkpoint,
        )

    # Ensure tabs have their tiles loaded
    interface.tabs = tabs

    # Convert to template
    template = TemplateConverter.interface_to_template(
        interface=interface,
        description=request.description,
        created_by=request_fastapi.state.user_id,
        tags=request.tags,
    )

    # Create metadata
    from datetime import datetime, timezone

    metadata = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by": request_fastapi.state.user_id,
        "source_project": request.project,
        "template_name": request.template_name or interface.name,
    }

    # Calculate export stats
    export_stats = {
        "interfaces": 1,
        "tabs": len(template.tabs),
        "tiles": sum(len(tab.tiles) for tab in template.tabs),
    }

    return TemplateExportResponse(
        template=template,
        metadata=metadata,
        export_stats=export_stats,
    )


@router.post(
    "/validate_template",
    response_model=ValidationResultSchema,
    responses={
        200: {
            "description": "Interface template validation completed",
            "content": {
                "application/json": {
                    "example": {
                        "is_valid": True,
                        "issues": [],
                        "can_sanitize": True,
                    },
                },
            },
        },
    },
)
def validate_interface_template(
    request_fastapi: Request,
    request: ValidateInterfaceTemplateRequest,
    session: Session = Depends(get_db_session),
):
    """Validate an interface template against a target project."""
    validator = TemplateValidator(session)

    # Get project validation schema
    validation_schema = validator.get_project_validation_schema(
        user_id=request_fastapi.state.user_id,
        project_name=request.project,
    )

    # Validate the interface template
    return validator.validate_interface_template(
        interface_template=request.template,
        validation_schema=validation_schema,
    )


@router.post(
    "/sanitize_template",
    response_model=dict,
    responses={
        200: {
            "description": "Interface template sanitized successfully",
            "content": {
                "application/json": {
                    "example": {
                        "sanitized_template": {
                            "name": "Analytics Dashboard",
                            "tabs": [{"name": "Overview", "tiles": []}],
                        },
                        "changes_made": ["Removed invalid context reference"],
                    },
                },
            },
        },
    },
)
def sanitize_interface_template(
    request_fastapi: Request,
    request: SanitizeInterfaceTemplateRequest,
    session: Session = Depends(get_db_session),
):
    """Sanitize an interface template for a target project."""
    validator = TemplateValidator(session)

    # Get project validation schema
    validation_schema = validator.get_project_validation_schema(
        user_id=request_fastapi.state.user_id,
        project_name=request.project,
    )

    # Sanitize the template
    sanitizer = TemplateSanitizer(validation_schema)
    sanitized_template = sanitizer.sanitize_interface_template(
        interface_template=request.template,
        remove_invalid=request.remove_invalid_references,
        preserve_structure=request.preserve_structure,
    )

    return {
        "sanitized_template": sanitized_template,
        "changes_made": ["Template sanitized for target project"],
    }


@router.post(
    "/import_template",
    response_model=TemplateImportResponse,
    responses={
        200: {
            "description": "Interface template imported successfully",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "import_stats": {"interfaces": 1, "tabs": 2, "tiles": 5},
                        "created_ids": {"interface_id": "abc123"},
                        "warnings": [],
                    },
                },
            },
        },
        400: {
            "description": "Template validation failed",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Template validation failed: Context 'invalid_context' not found",
                    },
                },
            },
        },
    },
)
def import_interface_template(
    request_fastapi: Request,
    request: ImportInterfaceTemplateRequest,
    session: Session = Depends(get_db_session),
):
    """Import an interface template into a project."""
    organization_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = InterfaceDAO(session)
    tab_dao = TabDAO(session)
    tile_dao = TileDAO(session)

    # Get target project
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=request.project,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project} not found or you don't have access.",
        )

    validation_result = None
    warnings = []

    # Apply validation and sanitization
    template = request.template

    validator = TemplateValidator(session)
    validation_schema = validator.get_project_validation_schema(
        user_id=request_fastapi.state.user_id,
        project_name=project.name,
    )

    # Apply basic sanitization always (for security/consistency reasons)
    sanitizer = TemplateSanitizer(validation_schema)
    template_dict = sanitizer.sanitize_interface_template(
        interface_template=template,
        remove_invalid=False,  # Only remove basic issues like 'selected' field
        preserve_structure=True,
    )
    template = InterfaceTemplateSchema(**template_dict)

    if request.validate_first:
        validation_schema = validator.get_project_validation_schema(
            user_id=request_fastapi.state.user_id,
            project_name=request.project,
        )

        validation_result = validator.validate_interface_template(
            interface_template=template,
            validation_schema=validation_schema,
        )

        # Check for blocking errors
        errors = [issue for issue in validation_result.issues if issue.level == "error"]
        if errors and not request.auto_sanitize:
            error_messages = [
                f"{issue.component_name}: {issue.message}" for issue in errors
            ]
            raise HTTPException(
                status_code=400,
                detail=f"Template validation failed: {'; '.join(error_messages)}",
            )

        # Auto-sanitize if requested and there are issues
        if request.auto_sanitize and not validation_result.is_valid:
            sanitized_dict = sanitizer.sanitize_interface_template(
                interface_template=template,
                remove_invalid=True,
                preserve_structure=True,
            )
            # Convert back to schema object
            template = InterfaceTemplateSchema(**sanitized_dict)
            warnings.append("Template was automatically sanitized")

    # Use TemplateConverter to create interface from template
    try:
        interface, converter_warnings = TemplateConverter.template_to_interface(
            interface_template=template,
            project_id=project.id,
            interface_dao=interface_dao,
            tab_dao=tab_dao,
            tile_dao=tile_dao,
            new_interface_name=request.new_interface_name,
            overwrite_existing=request.overwrite_existing,
        )
        warnings.extend(converter_warnings)
    except ValueError as e:
        if "already exists" in str(e):
            if request.overwrite_existing:
                # This shouldn't happen as overwrite should handle it
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to overwrite interface: {str(e)}",
                )
            else:
                raise HTTPException(
                    status_code=409,
                    detail=f"Interface with name {request.new_interface_name or template.name} already exists. Use overwrite_existing=true to replace it.",
                )
        else:
            raise HTTPException(status_code=400, detail=str(e))

    # Calculate import stats
    tabs = tab_dao.list_tabs(interface_id=str(interface.id), is_checkpoint=False)
    total_tiles = 0
    for tab in tabs:
        tiles = tile_dao.list_tiles_by_tab(tab_id=str(tab.id), is_checkpoint=False)
        total_tiles += len(tiles)

    created_ids = {"interface_id": str(interface.id)}
    import_stats = {"interfaces": 1, "tabs": len(tabs), "tiles": total_tiles}

    if validation_result:
        validation_result.sanitized_template = template

    return TemplateImportResponse(
        success=True,
        validation_result=validation_result,
        import_stats=import_stats,
        created_ids=created_ids,
        warnings=warnings,
    )
