from typing import List, Optional

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
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    name: str = Query(..., description="The name of the interface to retrieve"),
    checkpoint: bool = Query(False, description="Whether to get a checkpoint version (manually saved)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Get a specific interface by project ID and name."""
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
    
    # Get interface by project and name
    interface = interface_dao.get_by_project_and_name(
        project_id=project.id,
        name=name,
        is_checkpoint=checkpoint
    )
    
    if not interface:
        raise HTTPException(
            status_code=404, 
            detail=f"Interface {name} not found in project {project_id}."
        )
    
    # Get tabs for this interface
    tabs = tab_dao.list_tabs_by_interface(
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
        name=project_id,
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
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    name: str = Query(..., description="The name of the interface to update"),
    checkpoint: bool = Query(False, description="Whether this is a checkpoint update (manual save)"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Update an interface by project ID and name."""
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
    
    # Get interface by project and name
    interface = interface_dao.get_by_project_and_name(
        project_id=project.id,
        name=name,
        is_checkpoint=checkpoint
    )
    
    if not interface:
        raise HTTPException(
            status_code=404, 
            detail=f"Interface {name} not found in project {project_id}."
        )
    
    # Prepare update parameters
    update_params = {
        "project_id": project.id,
        "name": name,
        "is_checkpoint": checkpoint
    }
    
    # Add new name if provided
    if request.name is not None:
        update_params["new_name"] = request.name
    
    if request.color is not None:
        update_params["color"] = request.color
        
    if request.active_tab_id is not None:
        # Verify that the tab exists and belongs to this interface
        tab = tab_dao.get_tab(request.active_tab_id)
        if not tab or tab.interface_id != interface.id:
            raise HTTPException(
                status_code=404, 
                detail=f"Tab {request.active_tab_id} not found or doesn't belong to this interface."
            )
        update_params["active_tab_id"] = request.active_tab_id
    
    # Update the interface
    updated = interface_dao.update_interface_by_name(**update_params)
    
    # Get tabs for this interface
    tabs = tab_dao.list_tabs_by_interface(
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
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    name: str = Query(..., description="The name of the interface to checkpoint"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Create a manual checkpoint (save) of an interface by project ID and name."""
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
    
    # Get interface by project and name
    interface = interface_dao.get_by_project_and_name(
        project_id=project.id,
        name=name,
        is_checkpoint=False  # We're looking for the active interface, not a checkpoint
    )
    
    if not interface:
        raise HTTPException(
            status_code=404, 
            detail=f"Interface {name} not found in project {project_id}."
        )
    
    # Create a checkpoint
    updated = interface_dao.make_checkpoint_by_name(
        project_id=project.id,
        name=name
    )
    
    # Get tabs for this interface
    tabs = tab_dao.list_tabs_by_interface(
        interface_id=updated.id,
        is_checkpoint=updated.is_checkpoint
    )
    
    return _create_interface_response(updated, tabs)


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
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    name: str = Query(..., description="The name of the interface to get checkpoint for"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Get the latest checkpoint (manual save) for an interface by project ID and name."""
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
    
    # Get interface by project and name
    interface = interface_dao.get_by_project_and_name(
        project_id=project.id,
        name=name,
        is_checkpoint=False  # We're looking for the active interface, not a checkpoint
    )
    
    if not interface:
        raise HTTPException(
            status_code=404, 
            detail=f"Interface {name} not found in project {project_id}."
        )
    
    # Find the latest checkpoint
    checkpoint = interface_dao.get_latest_checkpoint(project.id, name)
    if not checkpoint:
        raise HTTPException(
            status_code=404, 
            detail=f"No checkpoint found for interface {name} in project {project_id}."
        )
    
    # Get tabs for this checkpoint interface
    tabs = tab_dao.list_tabs_by_interface(
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
    project_id: str = Query(..., description="The project ID the interface belongs to"),
    name: str = Query(..., description="The name of the interface to delete"),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    tab_dao: TabDAO = Depends(),
):
    """Delete an interface by project ID and name."""
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
    
    # Get interface by project and name
    interface = interface_dao.get_by_project_and_name(
        project_id=project.id,
        name=name,
        is_checkpoint=False  # We're looking for the active interface, not a checkpoint
    )
    
    if not interface:
        raise HTTPException(
            status_code=404, 
            detail=f"Interface {name} not found in project {project_id}."
        )
    
    # First delete all tabs associated with this interface
    tabs = tab_dao.list_tabs_by_interface(interface_id=interface.id)
    for tab in tabs:
        tab_dao.delete_tab_by_name(interface_id=interface.id, name=tab.name)
    
    # Delete the interface
    success = interface_dao.delete_interface_by_name(project_id=project.id, name=name)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete interface.") 