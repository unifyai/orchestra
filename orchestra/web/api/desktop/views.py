from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.desktop_dao import DesktopDAO
from orchestra.web.api.assistant.schema import InfoResponse
from orchestra.web.api.dependencies import get_db_session
from orchestra.web.api.desktop.schema import DesktopCreate, DesktopRead, DesktopUpdate

router = APIRouter(tags=["Desktops"])


@router.post(
    "/desktop",
    response_model=InfoResponse[DesktopRead],
    status_code=status.HTTP_200_OK,
    summary="Register a desktop",
    description="Register a user desktop after the desktop app obtains a public hostname from the tunnel service.",
)
def register_desktop(
    desktop_in: DesktopCreate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[DesktopRead]:
    user_id = request.state.user_id
    dao = DesktopDAO(session)

    desktop = dao.create(
        user_id=user_id,
        name=desktop_in.name,
        url=desktop_in.url,
        os=desktop_in.os,
    )
    session.commit()

    return InfoResponse(
        info=DesktopRead(
            id=desktop.id,
            user_id=desktop.user_id,
            name=desktop.name,
            url=desktop.url,
            os=desktop.os,
            assigned_to_assistant_id=None,
            created_at=desktop.created_at,
            updated_at=desktop.updated_at,
        ),
    )


@router.get(
    "/desktop",
    response_model=InfoResponse[List[DesktopRead]],
    status_code=status.HTTP_200_OK,
    summary="List desktops",
    description="List all registered desktops for the authenticated user, with assignment info.",
)
def list_desktops(
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[List[DesktopRead]]:
    user_id = request.state.user_id
    dao = DesktopDAO(session)

    desktops = dao.list_for_user(user_id)
    return InfoResponse(
        info=[
            DesktopRead(
                id=d.id,
                user_id=d.user_id,
                name=d.name,
                url=d.url,
                os=d.os,
                assigned_to_assistant_id=dao.get_assigned_assistant_id(d.id),
                created_at=d.created_at,
                updated_at=d.updated_at,
            )
            for d in desktops
        ],
    )


@router.patch(
    "/desktop/{desktop_id}",
    response_model=InfoResponse[DesktopRead],
    status_code=status.HTTP_200_OK,
    summary="Update a desktop",
    description="Update a registered desktop's URL, name, or OS.",
)
def update_desktop(
    desktop_id: int,
    desktop_update: DesktopUpdate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[DesktopRead]:
    user_id = request.state.user_id
    dao = DesktopDAO(session)

    update_data = desktop_update.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update.",
        )

    updated = dao.update(desktop_id, user_id, update_data)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Desktop not found.",
        )
    session.commit()

    return InfoResponse(
        info=DesktopRead(
            id=updated.id,
            user_id=updated.user_id,
            name=updated.name,
            url=updated.url,
            os=updated.os,
            assigned_to_assistant_id=dao.get_assigned_assistant_id(updated.id),
            created_at=updated.created_at,
            updated_at=updated.updated_at,
        ),
    )


@router.delete(
    "/desktop/{desktop_id}",
    response_model=InfoResponse[str],
    status_code=status.HTTP_200_OK,
    summary="Unregister a desktop",
    description="Unregister a desktop. Fails if the desktop is currently assigned to an assistant.",
)
def delete_desktop(
    desktop_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[str]:
    user_id = request.state.user_id
    dao = DesktopDAO(session)

    assigned = dao.get_assigned_assistant_id(desktop_id)
    if assigned is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Desktop is assigned to assistant {assigned}. Unassign it first.",
        )

    deleted = dao.delete(desktop_id, user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Desktop not found.",
        )
    session.commit()

    return InfoResponse(info="Desktop deleted successfully.")
