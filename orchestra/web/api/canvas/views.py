"""Canvas token registration and resolution endpoints.

A canvas lives in Unify contexts under ``Canvas/*``: the authored source, the
compiled bundle, the resolved bindings and the declared actions are all rows
there. What is relational is only what console needs before it can read any of
that — which context, whose identity, and whether this canvas may be served at
all.

Two roles, deliberately split:

* **Unify** (``API_KEY_AUTH``) registers a token when it publishes a canvas,
  moves it between draft, published and quarantined, and revokes it on delete.
* **Console** (``ADMIN_AUTH``) resolves a token to the owner's identity so it can
  read the canvas row as that owner, and gets ``visibility`` and ``status`` back
  on the same call because it has to check both before using the admin key.

There is no bundle endpoint here. The compiled module is stored on the canvas row
itself with its sha256, so console reads it through the ordinary logs API as the
owner and verifies the hash before handing the bytes to the frame. An endpoint
serving bundle bytes would be a second, weaker path to the same data.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestra.db.dao.canvas_token_dao import CanvasTokenDAO
from orchestra.db.dependencies import get_db_session
from orchestra.web.api.canvas.schema import (
    CanvasTokenResolutionResponse,
    CanvasTokenResponse,
    RegisterCanvasTokenRequest,
    UpdateCanvasTokenRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()
admin_router = APIRouter()


def _resolve_project_id(
    session: Session,
    *,
    user_id: str,
    organization_id,
    project_name: str,
) -> int:
    """Resolve a project the caller can actually reach.

    Going through ``filter_by_user_access`` rather than a name lookup is what
    stops a caller registering a token that points at somebody else's project.
    """
    from orchestra.db.dao.context_dao import ContextDAO
    from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
    from orchestra.db.dao.project_dao import ProjectDAO

    org_member_dao = OrganizationMemberDAO(session)
    context_dao = ContextDAO(session)
    project_dao = ProjectDAO(session, org_member_dao, context_dao)

    rows = project_dao.filter_by_user_access(
        user_id=user_id,
        organization_id=organization_id,
        name=project_name,
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_name}' not found or not accessible",
        )
    return rows[0][0].id


def _owned_entry(session: Session, token: str, user_id: str):
    """Fetch a token the caller owns, or raise.

    404 for missing and 403 for someone else's, rather than 404 for both: the
    token is already known to whoever holds the URL, so collapsing the two hides
    nothing and makes a real permission problem look like a typo.
    """
    dao = CanvasTokenDAO(session)
    entry = dao.get_by_token(token)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found",
        )
    if entry.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify your own canvases",
        )
    return dao, entry


@router.post(
    "/canvas/tokens",
    response_model=CanvasTokenResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Token registered successfully"},
        404: {"description": "Project not found or not accessible"},
        409: {"description": "Token already exists"},
    },
)
def register_canvas_token(
    request_fastapi: Request,
    body: RegisterCanvasTokenRequest,
    session: Session = Depends(get_db_session),
) -> CanvasTokenResponse:
    """Register a token-to-context mapping for a canvas.

    Called by Unify once the canvas row is written. The token is generated there,
    so a collision is a genuine conflict rather than something to retry silently.
    """
    user_id = request_fastapi.state.user_id
    organization_id = request_fastapi.state.organization_id

    dao = CanvasTokenDAO(session)
    if dao.get_by_token(body.token):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Token '{body.token}' already exists",
        )

    project_id = _resolve_project_id(
        session,
        user_id=user_id,
        organization_id=organization_id,
        project_name=body.project_name,
    )

    entry = dao.register(
        token=body.token,
        context_name=body.context_name,
        project_id=project_id,
        user_id=user_id,
        organization_id=organization_id,
        visibility=body.visibility,
        status=body.status,
    )
    session.commit()

    return CanvasTokenResponse(
        token=entry.token,
        context_name=entry.context_name,
        visibility=entry.visibility,
        status=entry.status,
    )


@router.patch(
    "/canvas/tokens/{token}",
    response_model=CanvasTokenResponse,
    responses={
        200: {"description": "Token updated"},
        403: {"description": "Not the owner"},
        404: {"description": "Token not found"},
    },
)
def update_canvas_token(
    request_fastapi: Request,
    token: str,
    body: UpdateCanvasTokenRequest,
    session: Session = Depends(get_db_session),
) -> CanvasTokenResponse:
    """Change a canvas's visibility or lifecycle status.

    Quarantining is the kill switch: it stops the bundle, the data reads and the
    actions in one place, which is why it is a status on the routing row rather
    than a flag each read path checks for itself.
    """
    dao, _ = _owned_entry(session, token, request_fastapi.state.user_id)

    entry = dao.set_state(token, visibility=body.visibility, status=body.status)
    session.commit()

    return CanvasTokenResponse(
        token=entry.token,
        context_name=entry.context_name,
        visibility=entry.visibility,
        status=entry.status,
    )


@router.delete(
    "/canvas/tokens/{token}",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Token deleted"},
        403: {"description": "Not the owner"},
        404: {"description": "Token not found"},
    },
)
def delete_canvas_token(
    request_fastapi: Request,
    token: str,
    session: Session = Depends(get_db_session),
) -> dict:
    """Revoke a canvas token so its URL stops resolving."""
    dao, _ = _owned_entry(session, token, request_fastapi.state.user_id)
    dao.delete_by_token(token)
    session.commit()
    return {"deleted": True, "token": token}


@admin_router.get(
    "/canvas/tokens/{token}",
    response_model=CanvasTokenResolutionResponse,
    responses={
        200: {"description": "Token resolved"},
        404: {"description": "Token not found"},
    },
)
def admin_resolve_canvas_token(
    token: str,
    session: Session = Depends(get_db_session),
) -> CanvasTokenResolutionResponse:
    """Resolve a token to its context path, owner identity and access state.

    Used by console to read a canvas from Unify contexts with the owner's API
    key. Resolution deliberately succeeds for a quarantined or draft canvas and
    reports the status: the caller needs to tell "no such canvas" apart from
    "exists but is not servable", and only the caller knows whether it is serving
    a viewer or the owner's own editor.
    """
    entry = CanvasTokenDAO(session).get_by_token(token)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found",
        )

    return CanvasTokenResolutionResponse(
        context_name=entry.context_name,
        user_id=entry.user_id,
        organization_id=entry.organization_id,
        project_id=entry.project_id,
        project_name=entry.project.name,
        visibility=entry.visibility,
        status=entry.status,
    )
