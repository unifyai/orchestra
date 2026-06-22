from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.desktop_dao import DesktopDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.assistant.schema import InfoResponse
from orchestra.web.api.desktop.schema import (
    DesktopCreate,
    DesktopLinkCreate,
    DesktopLinkRead,
    DesktopPubkeyRead,
    DesktopRead,
    DesktopUpdate,
    SftpTunnelUpdate,
)

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
            assigned_to_assistant_ids=[],
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
                assigned_to_assistant_ids=dao.list_assigned_assistant_ids(d.id),
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
            assigned_to_assistant_ids=dao.list_assigned_assistant_ids(updated.id),
            created_at=updated.created_at,
            updated_at=updated.updated_at,
        ),
    )


@router.delete(
    "/desktop/{desktop_id}",
    response_model=InfoResponse[str],
    status_code=status.HTTP_200_OK,
    summary="Unregister a desktop",
    description="Unregister a desktop. If assigned to an assistant, the assignment is cleared automatically.",
)
def delete_desktop(
    desktop_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[str]:
    user_id = request.state.user_id
    dao = DesktopDAO(session)

    # Assistant links cascade via the FK ON DELETE CASCADE.
    deleted = dao.delete(desktop_id, user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Desktop not found.",
        )
    session.commit()

    return InfoResponse(info="Desktop deleted successfully.")


@router.post(
    "/desktop/link",
    response_model=InfoResponse[DesktopLinkRead],
    status_code=status.HTTP_200_OK,
    summary="Link a desktop to an assistant",
    description=(
        "Link the caller's own registered desktop to an assistant. Replaces any "
        "desktop the caller has already linked to that assistant."
    ),
)
def link_desktop(
    link_in: DesktopLinkCreate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[DesktopLinkRead]:
    user_id = request.state.user_id
    dao = DesktopDAO(session)

    link = dao.link(
        assistant_id=link_in.assistant_id,
        desktop_id=link_in.desktop_id,
        requesting_user_id=user_id,
        filesys_sync=link_in.filesys_sync,
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Desktop not found.",
        )
    session.commit()

    return InfoResponse(
        info=DesktopLinkRead(
            assistant_id=link.assistant_id,
            desktop_id=link.user_desktop_id,
            owner_user_id=link.owner_user_id,
            filesys_sync=link.filesys_sync,
        ),
    )


@router.delete(
    "/desktop/link/{assistant_id}",
    response_model=InfoResponse[str],
    status_code=status.HTTP_200_OK,
    summary="Unlink the caller's desktop from an assistant",
    description="Remove the caller's own desktop link from an assistant.",
)
def unlink_desktop(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[str]:
    user_id = request.state.user_id
    dao = DesktopDAO(session)

    unlinked = dao.unlink(assistant_id, user_id)
    if not unlinked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No linked desktop found for this assistant.",
        )
    session.commit()

    return InfoResponse(info="Desktop unlinked successfully.")


@router.get(
    "/desktop/link/{assistant_id}/pubkey",
    response_model=InfoResponse[DesktopPubkeyRead],
    status_code=status.HTTP_200_OK,
    summary="Get the public key for on-demand filesystem access",
    description=(
        "Return the OpenSSH public key the desktop app must install in its "
        "app-owned authorized_keys so this assistant can reach the user's home "
        "over SFTP. Available only when the link has filesystem sync enabled."
    ),
)
def get_link_pubkey(
    assistant_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[DesktopPubkeyRead]:
    user_id = request.state.user_id
    dao = DesktopDAO(session)

    public_key = dao.get_link_pubkey(assistant_id, user_id)
    if public_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No filesystem-sync link found for this assistant.",
        )
    session.commit()

    return InfoResponse(info=DesktopPubkeyRead(public_key=public_key))


@router.post(
    "/desktop/link/{assistant_id}/sftp-tunnel",
    response_model=InfoResponse[str],
    status_code=status.HTTP_200_OK,
    summary="Report the SFTP tunnel coordinates for the caller's device",
    description=(
        "The desktop app reports the public host/port of the raw-TCP tunnel "
        "fronting its local SFTP server, so the assistant can dial it on demand."
    ),
)
def set_link_sftp_tunnel(
    assistant_id: int,
    tunnel_in: SftpTunnelUpdate,
    request: Request,
    session: Session = Depends(get_db_session),
) -> InfoResponse[str]:
    user_id = request.state.user_id
    dao = DesktopDAO(session)

    link = dao.set_sftp_tunnel(
        assistant_id,
        user_id,
        host=tunnel_in.host,
        port=tunnel_in.port,
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No linked desktop found for this assistant.",
        )
    session.commit()

    return InfoResponse(info="SFTP tunnel coordinates updated.")
