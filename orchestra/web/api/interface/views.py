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
            "description": "Context Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Context <context> not found.",
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
        project_id=projects[0][0].id,
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
        project_id=projects[0][0].id,
        context=request.context,
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
            "description": "Context Not Found",
            "content": {
                "application/json": {
                    "example": {
                        "detail": "Context <context> not found.",
                    },
                },
            },
        },
        404_3: {
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
        project_id=projects[0][0].id,
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
        project_id=projects[0][0].id,
        items=json.dumps([item.model_dump() for item in request.items]),
        new_counter=request.new_counter,
        context=request.context,
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
                            "name": "tab1",
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
    all_interfaces = dao.get_interfaces(
        request_fastapi.state.user_id,
        project_id=projects[0][0].id,
    )
    if len(all_interfaces) == 0:
        name = name if name is not None else "tab1"
    interfaces = dao.get_interfaces(
        request_fastapi.state.user_id,
        project_id=projects[0][0].id,
        name=name,
    )
    if len(interfaces) == 0 and len(all_interfaces) == 0:
        items = [
            {
                "i": "Table",
                "x": 0.0,
                "y": 0.0,
                "w": 7.0,
                "h": 8.0,
                "tab": "Table",
                "table_type": "Data Table",
            },
            {
                "i": "View",
                "x": 7.0,
                "y": 0.0,
                "w": 5.0,
                "h": 8.0,
                "tab": "View",
                "table": "Table",
            },
        ]
        new_counter = len(items)
        dao.create_interface(
            user_id=request_fastapi.state.user_id,
            name=name,
            items=json.dumps(items),
            new_counter=new_counter,
            project_id=projects[0][0].id,
            context=None,
        )
        return [
            {
                "name": name,
                "project": project,
                "items": items,
                "new_counter": new_counter,
                "context": None,
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
            "context": interface.context,
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
    project_dao: ProjectDAO = Depends(),
):
    projects = project_dao.filter(user_id=request_fastapi.state.user_id, name=project)
    if len(projects) == 0:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )
    dao = temp_interface_dao if temporary else interface_dao
    interfaces = dao.get_interfaces(
        request_fastapi.state.user_id,
        project_id=projects[0][0].id,
        name=name,
    )
    if len(interfaces) == 0:
        raise HTTPException(
            status_code=404,
            detail="Interface not added yet. Create it first.",
        )
    dao.delete_interface(
        user_id=request_fastapi.state.user_id,
        project_id=projects[0][0].id,
        name=name,
    )
    return {"info": "Interface deleted successfully!"}
