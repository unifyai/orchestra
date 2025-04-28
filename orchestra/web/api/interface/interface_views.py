from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.tab_dao import TabDAO
from orchestra.web.api.interface.schema import (
    InterfaceSchema,
    CreateInterfaceRequest,
    UpdateInterfaceRequest,
)

router = APIRouter(prefix="/interfaces", tags=["interfaces"])


def _create_interface_response(interface, tabs=None) -> InterfaceSchema:
    """Helper function to convert an interface entity to an InterfaceSchema with optional tabs."""
    
    tab_list = []
    if tabs:
        # Format tabs into TabSchema objects
        for tab in tabs:
            # This would call the equivalent function in tab_views.py
            from orchestra.web.api.interface.tab_views import _create_tab_response
            tab_list.append(_create_tab_response(tab))
    
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
) -> Tuple[object, object]:
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
        interface = interface_dao.get(interface_id)
        if not interface:
            raise HTTPException(
                status_code=404, 
                detail=f"Interface with ID {interface_id} not found."
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
        
        # For specific operations like deletion, we need to get the active interface
        is_checkpoint = checkpoint
        if for_update and (checkpoint_operations := ["delete", "checkpoint"]):
            is_checkpoint = False
            
        # Get interface by project and name
        interface = interface_dao.get_by_project_and_name(
            project_id=project_obj.id,
            name=name,
            is_checkpoint=is_checkpoint
        )
        
        if not interface:
            raise HTTPException(
                status_code=404, 
                detail=f"Interface {name} not found in project {project}."
            )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either interface_id or both project and name must be provided.",
        )
    
    return interface, project_obj


@router.post(
    "/",
    response_model=InterfaceSchema,
    status_code=201,
    responses={
        201: {"description": "Interface created successfully"},
        404: {"description": "Project not found"},
        409: {"description": "Interface with this name already exists"},
    },
)
def create_interface(
    request_fastapi: Request,
    request: CreateInterfaceRequest,
    checkpoint: bool = Query(False, description="Whether to create a checkpoint interface (manual save)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
):
    """Create a new interface in a project."""
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
        is_checkpoint=checkpoint
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
        is_checkpoint=checkpoint
    )
    
    return _create_interface_response(interface)


@router.get(
    "/",
    response_model=InterfaceSchema,
    responses={
        200: {"description": "Interface details retrieved successfully"},
        404: {"description": "Interface not found"},
        400: {"description": "Missing required parameters"},
    },
)
def get_interface(
    request_fastapi: Request,
    interface_id: Optional[str] = Query(None, description="The ID of the interface to retrieve"),
    project: Optional[str] = Query(None, description="The project ID the interface belongs to"),
    name: Optional[str] = Query(None, description="The name of the interface to retrieve"),
    checkpoint: bool = Query(False, description="Whether to get a checkpoint version (manually saved)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Get a specific interface by ID or by project ID and name."""
    # Use helper function to get interface
    interface, _ = _get_interface(
        request_fastapi=request_fastapi,
        interface_id=interface_id,
        project=project,
        name=name,
        checkpoint=checkpoint,
        project_dao=project_dao,
        interface_dao=interface_dao
    )
    
    # Get tabs for this interface
    tabs = tab_dao.list_tabs(
        interface_id=interface.id,
        is_checkpoint=interface.is_checkpoint
    )
    
    return _create_interface_response(interface, tabs)


@router.get(
    "/list",
    response_model=List[InterfaceSchema],
    responses={
        200: {"description": "Interfaces list retrieved successfully"},
        404: {"description": "Project not found"},
    },
)
def list_interfaces(
    request_fastapi: Request,
    project: str = Query(..., description="The project ID to list interfaces for"),
    checkpoint: bool = Query(False, description="Whether to list checkpoint versions (manually saved)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """List all interfaces for a project."""
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
    interfaces = interface_dao.get_interfaces(project_id=project.id, is_checkpoint=checkpoint)
    
    result = []
    for interface in interfaces:
        # Get tabs for this interface
        tabs = tab_dao.list_tabs(
            interface_id=interface.id,
            is_checkpoint=interface.is_checkpoint
        )
        
        result.append(_create_interface_response(interface, tabs))
    
    return result


@router.put(
    "/",
    response_model=InterfaceSchema,
    responses={
        200: {"description": "Interface updated successfully"},
        404: {"description": "Interface not found"},
        400: {"description": "Missing required parameters"},
    },
)
def update_interface(
    request_fastapi: Request,
    request: UpdateInterfaceRequest,
    interface_id: Optional[str] = Query(None, description="The ID of the interface to update"),
    project: Optional[str] = Query(None, description="The project ID the interface belongs to"),
    name: Optional[str] = Query(None, description="The name of the interface to update"),
    checkpoint: bool = Query(False, description="Whether this is a checkpoint update (manual save)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Update an interface by ID or by project ID and name."""
    project_obj = None
    interface = None
    
    # Get by ID if provided - ID takes precedence over project+name
    if interface_id:
        interface = interface_dao.get(interface_id)
        if not interface:
            raise HTTPException(
                status_code=404, 
                detail=f"Interface with ID {interface_id} not found."
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
            is_checkpoint=checkpoint
        )
        
        # For updates, we need to handle the case where the interface with the given
        # checkpoint status might not exist yet (we'll create it in that case)
        if not interface and not checkpoint:
            # If non-checkpoint interface doesn't exist, that's an error
            raise HTTPException(
                status_code=404, 
                detail=f"Interface {name} not found in project {project}."
            )
        elif not interface and checkpoint:
            # If checkpoint version doesn't exist but regular version does,
            # get the regular version to create a checkpoint from it
            regular_interface = interface_dao.get_by_project_and_name(
                project_id=project_obj.id,
                name=name,
                is_checkpoint=False
            )
            
            if not regular_interface:
                raise HTTPException(
                    status_code=404, 
                    detail=f"Interface {name} not found in project {project}."
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
                is_checkpoint=True
            )
    else:
        raise HTTPException(
            status_code=400,
            detail="Either interface_id or both project and name must be provided.",
        )
    
    # Convert Pydantic model to dict, excluding unset fields
    update_dict = request.model_dump()

    # Verify that the tab exists and belongs to this interface if active_tab_id is being updated
    if update_dict.get("active_tab_id"):
        tab = tab_dao.get(update_dict["active_tab_id"])
        if not tab or tab.interface_id != interface.id:
            raise HTTPException(
                status_code=404, 
                detail=f"Tab {update_dict['active_tab_id']} not found or doesn't belong to this interface."
            )
    
    # Update the interface
    if interface_id:
        updated = interface_dao.update_interface(
            id=interface_id,
            **update_dict
        )
    else:
        updated = interface_dao.update_interface(
            id=interface.id,  # We already have the interface, so use its ID
            **update_dict
        )

    # Get tabs for this interface
    tabs = tab_dao.list_tabs(
        interface_id=updated.id,
        is_checkpoint=updated.is_checkpoint
    )
    
    return _create_interface_response(updated, tabs)


@router.post(
    "/checkpoint",
    response_model=InterfaceSchema,
    responses={
        200: {"description": "Interface checkpoint created successfully"},
        404: {"description": "Interface not found"},
        400: {"description": "Missing required parameters"},
    },
)
def create_interface_checkpoint(
    request_fastapi: Request,
    interface_id: Optional[str] = Query(None, description="The ID of the interface to checkpoint"),
    project: Optional[str] = Query(None, description="The project ID the interface belongs to"),
    name: Optional[str] = Query(None, description="The name of the interface to checkpoint"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Create a manual checkpoint (save) of an interface by ID or by project ID and name."""
    # Get the active interface first
    interface, _ = _get_interface(
        request_fastapi=request_fastapi,
        interface_id=interface_id,
        project=project,
        name=name,
        checkpoint=False,  # Always get the active interface
        project_dao=project_dao,
        interface_dao=interface_dao,
        for_update=True
    )
    
    # Check if a checkpoint already exists
    existing_checkpoint = interface_dao.get_by_project_and_name(
        project_id=interface.project_id,
        name=interface.name,
        is_checkpoint=True
    )
    
    # If checkpoint exists, update it with the current interface values
    if existing_checkpoint:
        updated = interface_dao.update_interface(
            id=str(existing_checkpoint.id),
            color=interface.color,
            active_tab_id=interface.active_tab_id,
            is_checkpoint=True,
        )
    # Otherwise, create a new checkpoint
    else:
        updated = interface_dao.create_interface(
            name=interface.name,
            project_id=interface.project_id,
            color=interface.color,
            active_tab_id=interface.active_tab_id,
            is_checkpoint=True
        )
    
    # Verify the updated interface exists
    if not updated:
        raise HTTPException(
            status_code=500,
            detail="Failed to create or update checkpoint interface."
        )
    
    # Get tabs for the active interface
    active_tabs = tab_dao.list_tabs(
        interface_id=str(interface.id),  # Ensure ID is string
        is_checkpoint=False
    )
    
    # Create or update checkpoint tabs
    for tab in active_tabs:
        # Check if a checkpoint tab already exists
        existing_checkpoint_tab = tab_dao.get_by_interface_and_name(
            interface_id=str(updated.id),  # Ensure ID is string
            name=tab.name,
            is_checkpoint=True
        )
        
        if existing_checkpoint_tab:
            # Update existing checkpoint tab
            tab_dao.update_tab(
                id=str(existing_checkpoint_tab.id),  # Ensure ID is string
                visible=tab.visible,
                active=tab.active,
                order=tab.order,
                global_context=tab.global_context,
                color=tab.color
            )
        else:
            # Create new checkpoint tab
            tab_dao.create_tab(
                interface_id=str(updated.id),  # Ensure ID is string
                name=tab.name,
                visible=tab.visible,
                active=tab.active,
                order=tab.order,
                global_context=tab.global_context,
                color=tab.color,
                is_checkpoint=True
            )
    
    # Get all tabs for the checkpoint interface to return
    checkpoint_tabs = tab_dao.list_tabs(
        interface_id=str(updated.id),  # Ensure ID is string
        is_checkpoint=True
    )
    
    return _create_interface_response(updated, checkpoint_tabs)


@router.get(
    "/checkpoint",
    response_model=InterfaceSchema,
    responses={
        200: {"description": "Interface checkpoint retrieved successfully"},
        404: {"description": "Interface or checkpoint not found"},
        400: {"description": "Missing required parameters"},
    },
)
def get_interface_checkpoint(
    request_fastapi: Request,
    interface_id: Optional[str] = Query(None, description="The ID of the interface to get checkpoint for"),
    project: Optional[str] = Query(None, description="The project ID the interface belongs to"),
    name: Optional[str] = Query(None, description="The name of the interface to get checkpoint for"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Get the latest checkpoint (manual save) for an interface by ID or by project ID and name."""
    # Use helper function to get interface with for_update=True to ensure we're looking at the active interface
    
    # Find the latest checkpoint
    if interface_id:
        checkpoint = interface_dao.get_latest_checkpoint(id=interface_id)
    else:
        _, project_obj = _get_interface(
            request_fastapi=request_fastapi,
            interface_id=interface_id,
            project=project,
            name=name,
            checkpoint=False,
            project_dao=project_dao,
            interface_dao=interface_dao,
            for_update=True
        )
        checkpoint = interface_dao.get_latest_checkpoint(project_id=project_obj.id, name=name)
    
    if not checkpoint:
        raise HTTPException(
            status_code=404, 
            detail=f"No checkpoint found for the specified interface."
        )
    
    # Get tabs for this checkpoint interface
    tabs = tab_dao.list_tabs(
        interface_id=checkpoint.id,
        is_checkpoint=True
    )
    
    return _create_interface_response(checkpoint, tabs)


@router.delete(
    "/",
    status_code=204,
    responses={
        204: {"description": "Interface deleted successfully"},
        404: {"description": "Interface not found"},
        400: {"description": "Missing required parameters"},
    },
)
def delete_interface(
    request_fastapi: Request,
    interface_id: Optional[str] = Query(None, description="The ID of the interface to delete"),
    project: Optional[str] = Query(None, description="The project ID the interface belongs to"),
    name: Optional[str] = Query(None, description="The name of the interface to delete"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Delete an interface by ID or by project ID and name."""
    # Use helper function to get interface with for_update=True to ensure we're deleting the active interface
    interface, project_obj = _get_interface(
        request_fastapi=request_fastapi,
        interface_id=interface_id,
        project=project,
        name=name,
        checkpoint=False,
        project_dao=project_dao,
        interface_dao=interface_dao,
        for_update=True
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