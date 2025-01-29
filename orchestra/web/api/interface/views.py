import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from orchestra.db.dao.interface_dao import InterfaceDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.dao.temp_interface_dao import TempInterfaceDAO
from orchestra.web.api.interface.schema import InterfaceConfig

router = APIRouter()


@router.post(
    "/interface",
    responses={
        200: {
            "description": "Interface created.",
            "content": {
                "application/json": {
                    "example": {"info": "Interface created successfully!"},
                },
            },
        },
        400: {
            "description": "Interface already exists.",
            "content": {
                "application/json": {
                    "example": {
                        "info": "Interface already exists, update the interface instead.",
                    },
                },
            },
        },
        404: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
    },
)
def create_interface(
    request_fastapi: Request,
    request: InterfaceConfig,
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    temp_interface_dao: TempInterfaceDAO = Depends(),
):
    projects = project_dao.filter(
        user_id=request_fastapi.state.user_id,
        name=request.project,
    )
    if len(projects) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project} not found.",
        )
    dao = temp_interface_dao if request.temporary else interface_dao
    interfaces = dao.get_interfaces(
        request_fastapi.state.user_id,
        project=request.project,
        name=request.name,
    )
    if len(interfaces) > 0:
        raise HTTPException(
            status_code=400,
            detail="Interface already exists, update the interface instead.",
        )
    dao.create_interface(
        user_id=request_fastapi.state.user_id,
        name=request.name,
        items=json.dumps([item.model_dump() for item in request.items]),
        new_counter=request.new_counter,
        project=request.project,
    )
    return {"info": "Interface created successfully!"}


@router.put(
    "/interface",
    responses={
        200: {
            "description": "Interface updated.",
            "content": {
                "application/json": {"info": "Interface updated successfully!"},
            },
        },
        404_1: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
        404_2: {
            "description": "Interface Not Found",
            "content": {
                "application/json": {
                    "example": "Interface not added yet. Create it first.",
                },
            },
        },
    },
)
def update_interface(
    request_fastapi: Request,
    request: InterfaceConfig,
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    temp_interface_dao: TempInterfaceDAO = Depends(),
):
    projects = project_dao.filter(
        user_id=request_fastapi.state.user_id,
        name=request.project,
    )
    if len(projects) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project} not found.",
        )
    dao = temp_interface_dao if request.temporary else interface_dao
    interfaces = dao.get_interfaces(
        request_fastapi.state.user_id,
        project=request.project,
        name=request.name,
    )
    if len(interfaces) == 0:
        raise HTTPException(
            status_code=404,
            detail="Interface not added yet. Create it first.",
        )
    dao.update_interface(
        user_id=request_fastapi.state.user_id,
        name=request.name,
        project=request.project,
        items=json.dumps([item.model_dump() for item in request.items]),
        new_counter=request.new_counter,
        new_name=request.new_name,
    )
    return {"info": "Interface updated successfully!"}


@router.get(
    "/interface",
    responses={
        200: {
            "description": "Interface retrieved.",
            "content": {
                "application/json": {
                    "example": [
                        {
                            "name": "interface_1",
                            "project": "my_project",
                            "items": [
                                {
                                    "i": "n0",
                                    "x": 0,
                                    "y": 0,
                                    "w": 3,
                                    "h": 3,
                                    "tab": None,
                                },
                                {
                                    "i": "n1",
                                    "x": 0,
                                    "y": 3,
                                    "w": 2,
                                    "h": 3,
                                    "tab": "Plot_1",
                                },
                            ],
                            "new_counter": 2,
                        },
                    ],
                },
            },
        },
        404_1: {
            "description": "Project Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Project <project> not found.",
                    },
                },
            },
        },
        404_2: {
            "description": "Interface Not Found",
            "content": {
                "application/json": {
                    "example": "Interface not added yet. Create it first.",
                },
            },
        },
    },
)
def get_interfaces(
    request_fastapi: Request,
    name: str = Query(None),
    project: str = Query(...),
    temporary: bool = Query(False),
    project_dao: ProjectDAO = Depends(),
    interface_dao: InterfaceDAO = Depends(),
    temp_interface_dao: TempInterfaceDAO = Depends(),
):
    projects = project_dao.filter(user_id=request_fastapi.state.user_id, name=project)
    if len(projects) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )
    dao = temp_interface_dao if temporary else interface_dao
    all_interfaces = dao.get_interfaces(request_fastapi.state.user_id, project=project)
    if len(all_interfaces) == 0:
        name = name if name is not None else "interface_1"
    interfaces = dao.get_interfaces(
        request_fastapi.state.user_id,
        project=project,
        name=name,
    )
    if len(interfaces) == 0 and len(all_interfaces) == 0:
        items = []
        new_counter = len(items)
        dao.create_interface(
            user_id=request_fastapi.state.user_id,
            name=name,
            items=json.dumps(items),
            new_counter=new_counter,
            project=project,
        )
        return [
            {
                "name": name,
                "project": project,
                "items": items,
                "new_counter": new_counter,
            },
        ]
    elif len(interfaces) == 0:
        raise HTTPException(
            status_code=404,
            detail="Interface not added yet. Create it first.",
        )
    return [
        {
            "name": interface.name,
            "project": interface.project,
            "items": json.loads(interface.items),
            "new_counter": interface.new_counter,
        }
        for interface in interfaces
    ]


@router.delete(
    "/interface",
    responses={
        200: {
            "description": "Interface deleted.",
            "content": {
                "application/json": {
                    "example": {"info": "Interface deleted successfully!"},
                },
            },
        },
        404: {
            "description": "Interface Not Found",
            "content": {
                "application/json": {
                    "example": "Interface not added yet. Create it first.",
                },
            },
        },
    },
)
def delete_interface(
    request_fastapi: Request,
    name: str = Query(...),
    project: str = Query(...),
    temporary: bool = Query(False),
    interface_dao: InterfaceDAO = Depends(),
    temp_interface_dao: TempInterfaceDAO = Depends(),
):
    dao = temp_interface_dao if temporary else interface_dao
    interfaces = dao.get_interfaces(
        request_fastapi.state.user_id,
        project=project,
        name=name,
    )
    if len(interfaces) == 0:
        raise HTTPException(
            status_code=404,
            detail="Interface not added yet. Create it first.",
        )
    dao.delete_interface(request_fastapi.state.user_id, name=name)
    return {"info": "Interface deleted successfully!"}
