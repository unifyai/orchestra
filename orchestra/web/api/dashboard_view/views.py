"""
Endpoints related to dashboard_view management and operations.
"""

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from starlette import status

from orchestra.db.dao.dashboard_view_dao import DashboardViewDAO

# Async DAOs
from orchestra.db.dao.async_dashboard_view_dao import AsyncDashboardViewDAO
from sqlalchemy.ext.asyncio import AsyncSession
from orchestra.db.dependencies import get_async_db_session, get_db_session
from orchestra.web.api.dashboard_view.schema import (
    DashboardViewDelete,
    DashboardViewInfo,
    DashboardViewNewName,
)
from orchestra.web.api.utils.http_responses import not_found

router = APIRouter()


@router.get(
    "/dashboard_views/{project_id}",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {
                    "example": [
                        ("name1", "dashboard_view_1"),
                        ("name2", "dashboard_view_2"),
                    ],
                },
            },
        },
    },
)
async def list_dashboard_views(
    project_id: int = Path(),
    session: AsyncSession = Depends(get_async_db_session),
):
    """
    Retrieve a list of all dashboard_views.
    """
    dashboard_view_dao = AsyncDashboardViewDAO(session)
    dashboard_views = dashboard_view_dao.list_dashboard_views(project_id=project_id)
    return [[d.name, d.view] for d in dashboard_views]


@router.post(
    "/dashboard_view",
    responses={
        201: {
            "description": "DashboardView Created",
            "content": {
                "application/json": {
                    "example": {"info": "DashboardView created successfully!"},
                },
            },
        },
        400: {
            "description": "DashboardView already exists",
            "content": {
                "application/json": {"example": "DashboardView already exists."},
            },
        },
    },
)
async def create_dashboard_view(
    dashboard_view: DashboardViewInfo,
    session: AsyncSession = Depends(get_async_db_session),
):
    """
    Create a new dashboard_view.
    """
    dashboard_view_dao = AsyncDashboardViewDAO(session)
    existing_dashboard_view = await dashboard_view_dao.filter(
        project_id=dashboard_view.project_id,
        name=dashboard_view.name,
    )
    if existing_dashboard_view:
        raise HTTPException(status_code=400, detail="DashboardView already exists")
    await dashboard_view_dao.create(
        project_id=dashboard_view.project_id,
        name=dashboard_view.name,
        view=dashboard_view.view,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"info": "DashboardView created successfully!"},
    )


@router.patch(
    "/dashboard_view",
    responses={
        200: {
            "description": "DashboardView Renamed",
            "content": {
                "application/json": {"info": "DashboardView renamed successfully!"},
            },
        },
        404: {
            "description": "DashboardView Not Found",
            "content": {"application/json": {"example": "DashboardView not found."}},
        },
    },
)
async def rename_dashboard_view(
    new_name: DashboardViewNewName,
    session: AsyncSession = Depends(get_async_db_session),
):
    """
    Rename an existing dashboard_view.
    """
    dashboard_view_dao = AsyncDashboardViewDAO(session)
    dashboard_view = await dashboard_view_dao.filter(
        project_id=new_name.project_id,
        name=new_name.name,
    )
    if not dashboard_view:
        raise not_found("DashboardView")
    await dashboard_view_dao.update(id=dashboard_view[0].id, name=new_name.new_name)
    return {"info": "DashboardView renamed successfully!"}


@router.delete(
    "/dashboard_view",
    responses={
        200: {
            "description": "Successful Response",
            "content": {
                "application/json": {"info": "DashboardView deleted successfully!"},
            },
        },
        404: {
            "description": "DashboardView Not Found",
            "content": {"application/json": {"example": "DashboardView not found"}},
        },
    },
)
async def delete_dashboard_view(
    view_to_delete: DashboardViewDelete,
    session: AsyncSession = Depends(get_async_db_session),
):
    """
    Delete a dashboard_view and all its corresponding entries.
    """
    dashboard_view_dao = AsyncDashboardViewDAO(session)
    dashboard_view = await dashboard_view_dao.filter(
        project_id=view_to_delete.project_id,
        name=view_to_delete.name,
    )
    if not dashboard_view:
        raise not_found("DashboardView")
    await dashboard_view_dao.delete(id=dashboard_view[0].id)
    return {"info": "DashboardView deleted successfully!"}
