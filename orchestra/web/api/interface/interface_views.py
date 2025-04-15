from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

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
        id=interface.id,
        name=interface.name,
        project_id=str(interface.project_id),
        tabs=tab_list,
        active_tab_id=interface.active_tab_id,
        color=interface.color,
        is_checkpoint=interface.is_checkpoint,
        created_at=interface.created_at.isoformat() if interface.created_at else None,
        updated_at=interface.updated_at.isoformat() if interface.updated_at else None,
    )


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
        name=request.project_id,  # Assuming project_id is the name for now
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project_id} not found or you don't have access.",
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
    "/{interface_id}",
    response_model=InterfaceSchema,
    responses={
        200: {"description": "Interface details retrieved successfully"},
        404: {"description": "Interface not found"},
    },
)
def get_interface(
    request_fastapi: Request,
    interface_id: str = Path(..., description="The ID of the interface to retrieve"),
    checkpoint: bool = Query(False, description="Whether to get a checkpoint version (manually saved)"),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Get a specific interface by ID."""
    interface = interface_dao.get(interface_id)
        
    # If checkpoint is requested but interface is not a checkpoint, try to find the latest checkpoint
    if checkpoint and interface and not interface.is_checkpoint:
        checkpoint_interface = interface_dao.get_latest_checkpoint(interface.project_id, interface.name)
        if checkpoint_interface:
            interface = checkpoint_interface
    
    if not interface:
        raise HTTPException(status_code=404, detail=f"Interface {interface_id} not found.")
    
    # Get tabs for this interface
    tabs = tab_dao.list_tabs_by_interface(
        interface_id=interface.id,
        is_checkpoint=interface.is_checkpoint
    )
    
    return _create_interface_response(interface, tabs)


@router.get(
    "/",
    response_model=List[InterfaceSchema],
    responses={
        200: {"description": "Interfaces list retrieved successfully"},
        404: {"description": "Project not found"},
    },
)
def list_interfaces(
    request_fastapi: Request,
    project_id: str = Query(..., description="The project ID to list interfaces for"),
    checkpoint: bool = Query(False, description="Whether to list checkpoint versions (manually saved)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """List all interfaces for a project."""
    # Verify project exists and user has access
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project_id,  # Assuming project_id is the name for now
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project_id} not found or you don't have access.",
        )
    
    # Get interfaces
    interfaces = interface_dao.get_interfaces(project_id=project.id, is_checkpoint=checkpoint)
    
    result = []
    for interface in interfaces:
        # Get tabs for this interface
        tabs = tab_dao.list_tabs_by_interface(
            interface_id=interface.id,
            is_checkpoint=interface.is_checkpoint
        )
        
        result.append(_create_interface_response(interface, tabs))
    
    return result


@router.put(
    "/{interface_id}",
    response_model=InterfaceSchema,
    responses={
        200: {"description": "Interface updated successfully"},
        404: {"description": "Interface not found"},
    },
)
def update_interface(
    request_fastapi: Request,
    request: UpdateInterfaceRequest,
    interface_id: str = Path(..., description="The ID of the interface to update"),
    checkpoint: bool = Query(False, description="Whether this is a checkpoint update (manual save)"),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Update an interface by ID."""
    interface = interface_dao.get(interface_id)
    
    if not interface:
        raise HTTPException(status_code=404, detail=f"Interface {interface_id} not found.")
    
    # Update parameters
    update_params = {"id": interface_id, "is_checkpoint": checkpoint}
    
    if request.name is not None:
        update_params["name"] = request.name
    
    if request.color is not None:
        update_params["color"] = request.color
        
    if request.active_tab_id is not None:
        # Verify that the tab exists and belongs to this interface
        tab = tab_dao.get_tab(request.active_tab_id)
        if not tab or tab.interface_id != interface_id:
            raise HTTPException(
                status_code=404, 
                detail=f"Tab {request.active_tab_id} not found or doesn't belong to this interface."
            )
        update_params["active_tab_id"] = request.active_tab_id
    
    # Update the interface
    updated = interface_dao.update_interface(**update_params)
    
    # Get tabs for this interface
    tabs = tab_dao.list_tabs_by_interface(
        interface_id=updated.id,
        is_checkpoint=updated.is_checkpoint
    )
    
    return _create_interface_response(updated, tabs)


@router.post(
    "/{interface_id}/checkpoint",
    response_model=InterfaceSchema,
    responses={
        200: {"description": "Interface checkpoint created successfully"},
        404: {"description": "Interface not found"},
    },
)
def create_interface_checkpoint(
    request_fastapi: Request,
    interface_id: str = Path(..., description="The ID of the interface to checkpoint"),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Create a manual checkpoint (save) of an interface."""
    # Get the current interface
    interface = interface_dao.get(interface_id)
    if not interface:
        raise HTTPException(status_code=404, detail=f"Interface {interface_id} not found.")
    
    # Create a checkpoint by setting the is_checkpoint flag
    updated = interface_dao.make_checkpoint(interface_id)
    
    # Get tabs for this interface
    tabs = tab_dao.list_tabs_by_interface(
        interface_id=updated.id,
        is_checkpoint=updated.is_checkpoint
    )
    
    return _create_interface_response(updated, tabs)


@router.get(
    "/{interface_id}/checkpoint",
    response_model=InterfaceSchema,
    responses={
        200: {"description": "Interface checkpoint retrieved successfully"},
        404: {"description": "Interface or checkpoint not found"},
    },
)
def get_interface_checkpoint(
    request_fastapi: Request,
    interface_id: str = Path(..., description="The ID of the interface to get checkpoint for"),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Get the latest checkpoint (manual save) for an interface."""
    # Get the current interface
    interface = interface_dao.get(interface_id)
    if not interface:
        raise HTTPException(status_code=404, detail=f"Interface {interface_id} not found.")
    
    # Find the latest checkpoint
    checkpoint = interface_dao.get_latest_checkpoint(interface.project_id, interface.name)
    if not checkpoint:
        raise HTTPException(status_code=404, detail=f"No checkpoint found for interface {interface_id}.")
    
    # Get tabs for this checkpoint interface
    tabs = tab_dao.list_tabs_by_interface(
        interface_id=checkpoint.id,
        is_checkpoint=True
    )
    
    return _create_interface_response(checkpoint, tabs)


@router.delete(
    "/{interface_id}",
    status_code=204,
    responses={
        204: {"description": "Interface deleted successfully"},
        404: {"description": "Interface not found"},
    },
)
def delete_interface(
    request_fastapi: Request,
    interface_id: str = Path(..., description="The ID of the interface to delete"),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Delete an interface by ID."""
    interface = interface_dao.get(interface_id)
    
    if not interface:
        raise HTTPException(status_code=404, detail=f"Interface {interface_id} not found.")
    
    # First delete all tabs associated with this interface
    tabs = tab_dao.list_tabs_by_interface(interface_id=interface_id)
    for tab in tabs:
        tab_dao.delete_tab(tab.id)
    
    # Delete the interface
    success = interface_dao.delete_interface(interface_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete interface.") 