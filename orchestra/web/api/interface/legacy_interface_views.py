import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestra.db.dao.async_context_dao import AsyncContextDAO
from orchestra.db.dao.async_legacy_interface_dao import AsyncLegacyInterfaceDAO
from orchestra.db.dao.async_organization_member_dao import AsyncOrganizationMemberDAO
from orchestra.db.dao.async_project_dao import AsyncProjectDAO
from orchestra.db.dao.async_temp_interface_dao import AsyncTempInterfaceDAO
from orchestra.db.dependencies import get_async_db_session
from orchestra.db.models.orchestra_models import Interface
from orchestra.web.api.interface.schema import LegacyInterfaceConfig

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
async def create_interface(
    request_fastapi: Request,
    request: LegacyInterfaceConfig,
    session: AsyncSession = Depends(get_async_db_session),
):
    organization_member_dao = AsyncOrganizationMemberDAO(session)
    context_dao = AsyncContextDAO(session)
    project_dao = AsyncProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = AsyncLegacyInterfaceDAO(session)
    temp_interface_dao = AsyncTempInterfaceDAO(session)

    organization_id = getattr(request_fastapi.state, "organization_id", None)
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=request.project,
        organization_id=organization_id,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project} not found.",
        )
    dao = temp_interface_dao if request.temporary else interface_dao
    interfaces = dao.get_interfaces(
        project_id=project.id,
        name=request.name,
    )
    if len(interfaces) > 0:
        raise HTTPException(
            status_code=400,
            detail="Interface already exists, update the interface instead.",
        )

    # Validate context if provided (non-empty string)
    if request.context and request.context.strip():
        existing_contexts = await context_dao.filter(
            project_id=project.id,
            name=request.context,
        )
        if not existing_contexts:
            raise HTTPException(
                status_code=400,
                detail=f"Context '{request.context}' not found in project.",
            )

    # icon and order are accepted by both AsyncLegacyInterfaceDAO and AsyncTempInterfaceDAO implementations
    dao.create_interface(  # type: ignore[arg-type]
        name=request.name,
        items=json.dumps([item.model_dump() for item in request.items]),
        new_counter=request.new_counter,
        project_id=project.id,
        context=request.context,
        color=request.color,
        icon=request.icon or "folder",
        order=request.order,
    )

    # Retrieve the newly created interface to return its ID
    result = await session.execute(
        select(Interface)
        .where(Interface.project_id == project.id, Interface.name == request.name)
        .order_by(Interface.created_at.desc()),
    )
    created_ifc = result.scalars().first()

    return {"id": str(created_ifc.id) if created_ifc else None}


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
async def update_interface(
    request_fastapi: Request,
    request: LegacyInterfaceConfig,
    session: AsyncSession = Depends(get_async_db_session),
):
    organization_member_dao = AsyncOrganizationMemberDAO(session)
    context_dao = AsyncContextDAO(session)
    project_dao = AsyncProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = AsyncLegacyInterfaceDAO(session)
    temp_interface_dao = AsyncTempInterfaceDAO(session)

    organization_id = getattr(request_fastapi.state, "organization_id", None)
    project = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=request.project,
        organization_id=organization_id,
    )
    if not project:
        raise HTTPException(
            status_code=404,
            detail=f"Project {request.project} not found.",
        )
    dao = temp_interface_dao if request.temporary else interface_dao
    interfaces = dao.get_interfaces(
        project_id=project.id,
        name=request.name,
    )
    if len(interfaces) == 0:
        raise HTTPException(
            status_code=404,
            detail="Interface not added yet. Create it first.",
        )

    # Validate context if provided (non-empty string)
    if request.context and request.context.strip():
        existing_contexts = await context_dao.filter(
            project_id=project.id,
            name=request.context,
        )
        if not existing_contexts:
            raise HTTPException(
                status_code=400,
                detail=f"Context '{request.context}' not found in project.",
            )

    dao.update_interface(
        name=request.name,
        project_id=project.id,
        items=json.dumps([item.model_dump() for item in request.items]),
        new_counter=request.new_counter,
        context=request.context,
        color=request.color,
        icon=request.icon,
        order=request.order,
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
async def get_interfaces(
    request_fastapi: Request,
    name: str = Query(None),
    project: str = Query(...),
    temporary: bool = Query(False),
    session: AsyncSession = Depends(get_async_db_session),
):
    organization_member_dao = AsyncOrganizationMemberDAO(session)
    context_dao = AsyncContextDAO(session)
    project_dao = AsyncProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = AsyncLegacyInterfaceDAO(session)
    temp_interface_dao = AsyncTempInterfaceDAO(session)

    organization_id = getattr(request_fastapi.state, "organization_id", None)
    project_obj = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project,
        organization_id=organization_id,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )
    dao = temp_interface_dao if temporary else interface_dao
    all_interfaces = dao.get_interfaces(
        project_id=project_obj.id,
    )
    if len(all_interfaces) == 0:
        name = name if name is not None else "tab1"
    interfaces = dao.get_interfaces(
        project_id=project_obj.id,
        name=name,
    )
    if len(interfaces) == 0 and len(all_interfaces) == 0:
        # items = [
        #     {
        #         "i": "Table",
        #         "x": 0.0,
        #         "y": 0.0,
        #         "w": 7.0,
        #         "h": 8.0,
        #         "tab": "Table",
        #         "table_type": "Data Table",
        #     },
        #     {
        #         "i": "View",
        #         "x": 7.0,
        #         "y": 0.0,
        #         "w": 5.0,
        #         "h": 8.0,
        #         "tab": "View",
        #         "table": "Table",
        #     },
        # ]
        items = []
        new_counter = len(items)
        dao.create_interface(
            name=name,
            items=json.dumps(items),
            new_counter=new_counter,
            project_id=project_obj.id,
            context=None,
            color=None,
        )
        return [
            {
                "name": name,
                "project": project,
                "items": items,
                "new_counter": new_counter,
                "context": None,
                "color": None,
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
            "color": interface.color,
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
async def delete_interface(
    request_fastapi: Request,
    name: str = Query(...),
    project: str = Query(...),
    temporary: bool = Query(False),
    session: AsyncSession = Depends(get_async_db_session),
):
    organization_member_dao = AsyncOrganizationMemberDAO(session)
    context_dao = AsyncContextDAO(session)
    project_dao = AsyncProjectDAO(session, organization_member_dao, context_dao)
    interface_dao = AsyncLegacyInterfaceDAO(session)
    temp_interface_dao = AsyncTempInterfaceDAO(session)

    organization_id = getattr(request_fastapi.state, "organization_id", None)
    project_obj = project_dao.get_by_user_and_name(
        user_id=request_fastapi.state.user_id,
        name=project,
        organization_id=organization_id,
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project {project} not found.",
        )
    dao = temp_interface_dao if temporary else interface_dao
    interfaces = dao.get_interfaces(
        project_id=project_obj.id,
        name=name,
    )
    if len(interfaces) == 0:
        raise HTTPException(
            status_code=404,
            detail="Interface not added yet. Create it first.",
        )
    dao.delete_interface(
        project_id=project_obj.id,
        name=name,
    )
    return {"info": "Interface deleted successfully!"}
