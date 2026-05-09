"""REST endpoints for shared space lifecycle operations."""

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestra.db.dao.space_dao import SPACE_STATUS_ACTIVE, SpaceDAO
from orchestra.db.dao.space_invite_dao import SPACE_INVITE_STATUS_PENDING
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Assistant, Space, SpaceInvite
from orchestra.services.space_cleanup_service import (
    SpaceCleanupAuthError,
    SpaceCleanupConflictError,
    SpaceCleanupFailure,
    SpaceCleanupNotFoundError,
)
from orchestra.services.space_cleanup_service import delete_space as run_space_cleanup
from orchestra.services.space_cleanup_service import (
    purge_assistant_overlay as purge_space_member_overlay,
)
from orchestra.services.space_membership_refresh_service import (
    membership_refresh_payloads,
    publish_membership_refreshes_best_effort,
)
from orchestra.settings import settings
from orchestra.web.api.space.schema import (
    SpaceCreate,
    SpaceInviteCreate,
    SpaceInviteDecision,
    SpaceInviteRead,
    SpaceMember,
    SpaceMemberCreate,
    SpaceMembershipResponse,
    SpaceMembershipStatus,
    SpaceRead,
    SpaceSummary,
    SpaceUpdate,
)

router = APIRouter()


def _space_read(space: Space) -> SpaceRead:
    """Build the public representation of a space."""

    return SpaceRead.model_validate(space)


def _space_summary(space: Space) -> SpaceSummary:
    """Build the compact representation of a space."""

    return SpaceSummary.model_validate(space)


def _space_invite_read(invite: SpaceInvite) -> SpaceInviteRead:
    """Build the public representation of a space invitation."""

    return SpaceInviteRead.model_validate(invite)


def _space_member_read(membership, assistant: Assistant) -> SpaceMember:
    """Build the public representation of a live space member."""

    return SpaceMember(
        assistant_id=assistant.agent_id,
        space_id=membership.space_id,
        user_id=assistant.user_id,
        organization_id=assistant.organization_id,
        added_by=membership.added_by,
        created_at=membership.created_at,
    )


def _get_space_or_404(space_dao: SpaceDAO, space_id: int) -> Space:
    """Load a space or raise a 404 response."""

    space = space_dao.get(space_id)
    if space is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Space not found.",
        )
    return space


def _get_assistant_or_404(space_dao: SpaceDAO, assistant_id: int) -> Assistant:
    """Load an assistant or raise a 404 response."""

    assistant = space_dao.get_assistant(assistant_id)
    if assistant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assistant not found.",
        )
    return assistant


def _require_space_read(space_dao: SpaceDAO, user_id: str, space: Space) -> None:
    """Require read access to a space."""

    if not space_dao.can_read(user_id, space):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this space.",
        )


def _require_space_mutation(space_dao: SpaceDAO, user_id: str, space: Space) -> None:
    """Require administrative access to a space."""

    if not space_dao.can_mutate(user_id, space):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify this space.",
        )


def _require_active_space(space: Space) -> None:
    """Reject mutations against spaces that are being deleted."""

    if space.status != SPACE_STATUS_ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="space_not_active",
        )


def _require_assistant_read(
    space_dao: SpaceDAO,
    user_id: str,
    assistant: Assistant,
) -> None:
    """Require visibility of an assistant before returning its spaces."""

    if assistant.user_id == user_id:
        return
    if (
        assistant.organization_id is not None
        and space_dao.resource_access_dao.check_org_member_permission(
            user_id,
            assistant.organization_id,
            "org:read",
        )
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to view this assistant.",
    )


def _require_pending_invite(invite: SpaceInvite) -> None:
    """Require an invitation to still be pending."""

    if invite.status != SPACE_INVITE_STATUS_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="invite_not_pending",
        )


def _require_unexpired_invite(invite: SpaceInvite) -> None:
    """Reject acceptance of expired invitations."""

    if invite.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invite_expired",
        )


def _membership_response(
    *,
    membership_status: SpaceMembershipStatus,
    assistant_id: int,
    space_id: int,
    invite: SpaceInvite | None = None,
) -> SpaceMembershipResponse:
    """Build the discriminator response for adding a member."""

    return SpaceMembershipResponse(
        membership_status=membership_status,
        assistant_id=assistant_id,
        space_id=space_id,
        invite_id=invite.invite_id if invite else None,
        expires_at=invite.expires_at if invite else None,
    )


@router.post(
    "/spaces",
    response_model=SpaceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Spaces"],
    responses={
        409: {
            "description": "Workspace already exists for this scope and name key.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "error": "space_already_exists",
                            "message": "Workspace with this name already exists in this scope.",
                            "existing_id": 123,
                        },
                    },
                },
            },
        },
    },
)
def create_space(
    request: Request,
    body: SpaceCreate,
    session: Session = Depends(get_db_session),
) -> SpaceRead:
    """Create a personal or organization-scoped space."""

    user_id = request.state.user_id
    space_dao = SpaceDAO(session)
    if body.organization_id is not None and not space_dao.can_create_in_organization(
        user_id,
        body.organization_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to create spaces in this organization.",
        )

    existing_space = space_dao.find_team_space_by_natural_key(
        owner_user_id=user_id,
        organization_id=body.organization_id,
        name=body.name,
    )
    if existing_space is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "space_already_exists",
                "message": "Workspace with this name already exists in this scope.",
                "existing_id": existing_space.space_id,
                "name": body.name,
                "organization_id": body.organization_id,
                "kind": "team",
            },
        )

    space = space_dao.create(
        name=body.name,
        description=body.description,
        organization_id=body.organization_id,
        owner_user_id=user_id,
    )
    session.commit()
    return _space_read(space)


@router.get(
    "/spaces",
    response_model=List[SpaceRead],
    status_code=status.HTTP_200_OK,
    tags=["Spaces"],
)
def list_spaces(
    request: Request,
    session: Session = Depends(get_db_session),
) -> list[SpaceRead]:
    """List spaces visible to the authenticated user."""

    space_dao = SpaceDAO(session)
    return [
        _space_read(space) for space in space_dao.list_visible(request.state.user_id)
    ]


@router.get(
    "/spaces/{space_id}",
    response_model=SpaceRead,
    status_code=status.HTTP_200_OK,
    tags=["Spaces"],
)
def get_space(
    request: Request,
    space_id: int,
    session: Session = Depends(get_db_session),
) -> SpaceRead:
    """Return one visible space."""

    space_dao = SpaceDAO(session)
    space = _get_space_or_404(space_dao, space_id)
    _require_space_read(space_dao, request.state.user_id, space)
    return _space_read(space)


@router.patch(
    "/spaces/{space_id}",
    response_model=SpaceRead,
    status_code=status.HTTP_200_OK,
    tags=["Spaces"],
)
async def update_space(
    request: Request,
    space_id: int,
    body: SpaceUpdate,
    session: Session = Depends(get_db_session),
) -> SpaceRead:
    """Update display fields on an administrable space."""

    space_dao = SpaceDAO(session)
    space = _get_space_or_404(space_dao, space_id)
    _require_space_mutation(space_dao, request.state.user_id, space)
    _require_active_space(space)
    updated_space = space_dao.update(
        space,
        name=body.name,
        description=body.description,
    )
    refresh_payloads = []
    if body.name is not None or body.description is not None:
        refresh_payloads = membership_refresh_payloads(
            session,
            [assistant for _, assistant in space_dao.list_members(space_id)],
        )
    session.commit()
    await publish_membership_refreshes_best_effort(refresh_payloads)
    return _space_read(updated_space)


@router.delete(
    "/spaces/{space_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Spaces"],
)
async def delete_space(
    request: Request,
    space_id: int,
    session: Session = Depends(get_db_session),
) -> Response:
    """Delete a space and its shared data through the cleanup cascade."""

    try:
        await run_space_cleanup(
            session,
            space_id=space_id,
            user_id=request.state.user_id,
        )
    except SpaceCleanupNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Space not found.",
        )
    except SpaceCleanupAuthError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="space_mutation_forbidden",
        )
    except SpaceCleanupConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="space_cleanup_in_progress",
        )
    except SpaceCleanupFailure as exc:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"phase": exc.phase, "reason": exc.reason},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/spaces/{space_id}/members",
    response_model=SpaceMembershipResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Spaces"],
)
async def add_space_member(
    request: Request,
    space_id: int,
    body: SpaceMemberCreate,
    session: Session = Depends(get_db_session),
) -> SpaceMembershipResponse:
    """Add an assistant to a space or create an owner approval request."""

    user_id = request.state.user_id
    space_dao = SpaceDAO(session)
    space = _get_space_or_404(space_dao, space_id)
    assistant = _get_assistant_or_404(space_dao, body.assistant_id)
    _require_space_mutation(space_dao, user_id, space)
    _require_active_space(space)

    if space_dao.get_membership(
        space_id=space.space_id,
        assistant_id=assistant.agent_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already_member",
        )

    if space_dao.should_add_directly(user_id=user_id, space=space, assistant=assistant):
        space_dao.add_membership(space=space, assistant=assistant, added_by=user_id)
        refresh_payloads = membership_refresh_payloads(session, [assistant])
        session.commit()
        await publish_membership_refreshes_best_effort(refresh_payloads)
        return _membership_response(
            membership_status=SpaceMembershipStatus.active,
            assistant_id=assistant.agent_id,
            space_id=space.space_id,
        )

    invite, _ = space_dao.create_or_refresh_invite(
        space=space,
        assistant=assistant,
        invited_by=user_id,
        expiry_days=settings.space_invite_expiry_days,
    )
    session.commit()
    return _membership_response(
        membership_status=SpaceMembershipStatus.pending_invitation,
        assistant_id=assistant.agent_id,
        space_id=space.space_id,
        invite=invite,
    )


@router.delete(
    "/spaces/{space_id}/members/{assistant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Spaces"],
)
async def remove_space_member(
    request: Request,
    space_id: int,
    assistant_id: int,
    session: Session = Depends(get_db_session),
) -> Response:
    """Remove a live assistant membership from a space."""

    space_dao = SpaceDAO(session)
    space = _get_space_or_404(space_dao, space_id)
    _require_space_mutation(space_dao, request.state.user_id, space)
    _require_active_space(space)
    membership = space_dao.get_membership(
        space_id=space_id,
        assistant_id=assistant_id,
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Membership not found.",
        )
    try:
        await purge_space_member_overlay(
            session,
            assistant_id=assistant_id,
            space_id=space_id,
        )
    except SpaceCleanupFailure as exc:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"phase": exc.phase, "reason": exc.reason},
        )
    assistant = _get_assistant_or_404(space_dao, assistant_id)
    refresh_payloads = membership_refresh_payloads(session, [assistant])
    session.commit()
    await publish_membership_refreshes_best_effort(refresh_payloads)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/spaces/{space_id}/members",
    response_model=List[SpaceMember],
    status_code=status.HTTP_200_OK,
    tags=["Spaces"],
)
def list_space_members(
    request: Request,
    space_id: int,
    session: Session = Depends(get_db_session),
) -> list[SpaceMember]:
    """List live members of a visible space."""

    space_dao = SpaceDAO(session)
    space = _get_space_or_404(space_dao, space_id)
    _require_space_read(space_dao, request.state.user_id, space)
    return [
        _space_member_read(membership, assistant)
        for membership, assistant in space_dao.list_members(space_id)
    ]


@router.get(
    "/assistants/{assistant_id}/spaces",
    response_model=List[SpaceSummary],
    status_code=status.HTTP_200_OK,
    tags=["Spaces"],
)
def list_spaces_for_assistant(
    request: Request,
    assistant_id: int,
    session: Session = Depends(get_db_session),
) -> list[SpaceSummary]:
    """List spaces where an assistant is a live member."""

    space_dao = SpaceDAO(session)
    assistant = _get_assistant_or_404(space_dao, assistant_id)
    _require_assistant_read(space_dao, request.state.user_id, assistant)
    return [
        _space_summary(space)
        for space in space_dao.list_spaces_for_assistant(assistant_id)
        if space_dao.can_read(request.state.user_id, space)
    ]


@router.post(
    "/spaces/{space_id}/invites",
    response_model=SpaceInviteRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Spaces"],
)
def create_space_invite(
    request: Request,
    response: Response,
    space_id: int,
    body: SpaceInviteCreate,
    session: Session = Depends(get_db_session),
) -> SpaceInviteRead:
    """Create or refresh an owner-user invitation for an assistant."""

    user_id = request.state.user_id
    space_dao = SpaceDAO(session)
    space = _get_space_or_404(space_dao, space_id)
    assistant = _get_assistant_or_404(space_dao, body.assistant_id)
    _require_space_mutation(space_dao, user_id, space)
    _require_active_space(space)

    if space_dao.get_membership(
        space_id=space.space_id,
        assistant_id=assistant.agent_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already_member",
        )

    invite, created = space_dao.create_or_refresh_invite(
        space=space,
        assistant=assistant,
        invited_by=user_id,
        expiry_days=settings.space_invite_expiry_days,
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    session.commit()
    return _space_invite_read(invite)


@router.get(
    "/spaces/{space_id}/invites",
    response_model=List[SpaceInviteRead],
    status_code=status.HTTP_200_OK,
    tags=["Spaces"],
)
def list_space_invites(
    request: Request,
    space_id: int,
    session: Session = Depends(get_db_session),
) -> list[SpaceInviteRead]:
    """List invitations for an administrable space."""

    space_dao = SpaceDAO(session)
    space = _get_space_or_404(space_dao, space_id)
    _require_space_mutation(space_dao, request.state.user_id, space)
    return [
        _space_invite_read(invite)
        for invite in space_dao.list_invites_for_space(space_id)
    ]


@router.get(
    "/space-invites/pending",
    response_model=List[SpaceInviteRead],
    status_code=status.HTTP_200_OK,
    tags=["Spaces"],
)
def list_pending_space_invites(
    request: Request,
    session: Session = Depends(get_db_session),
) -> list[SpaceInviteRead]:
    """List pending invitations for assistants owned by the caller."""

    space_dao = SpaceDAO(session)
    return [
        _space_invite_read(invite)
        for invite in space_dao.list_pending_invites_for_owner(request.state.user_id)
    ]


@router.post(
    "/space-invites/{invite_id}/accept",
    response_model=SpaceInviteDecision,
    status_code=status.HTTP_200_OK,
    tags=["Spaces"],
)
async def accept_space_invite(
    request: Request,
    invite_id: int,
    session: Session = Depends(get_db_session),
) -> SpaceInviteDecision:
    """Accept a pending space invitation for one of the caller's assistants."""

    space_dao = SpaceDAO(session)
    invite = space_dao.get_invite(invite_id)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite not found.",
        )
    if invite.invited_owner_id != request.state.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to accept this invite.",
        )
    _require_pending_invite(invite)
    _require_unexpired_invite(invite)
    space_dao.accept_invite(invite)
    assistant = _get_assistant_or_404(space_dao, invite.assistant_id)
    refresh_payloads = membership_refresh_payloads(session, [assistant])
    session.commit()
    await publish_membership_refreshes_best_effort(refresh_payloads)
    return SpaceInviteDecision(status="accepted")


@router.post(
    "/space-invites/{invite_id}/decline",
    response_model=SpaceInviteDecision,
    status_code=status.HTTP_200_OK,
    tags=["Spaces"],
)
def decline_space_invite(
    request: Request,
    invite_id: int,
    session: Session = Depends(get_db_session),
) -> SpaceInviteDecision:
    """Decline a pending space invitation."""

    space_dao = SpaceDAO(session)
    invite = space_dao.get_invite(invite_id)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite not found.",
        )
    if invite.invited_owner_id != request.state.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to decline this invite.",
        )
    _require_pending_invite(invite)
    space_dao.decline_invite(invite)
    session.commit()
    return SpaceInviteDecision(status="declined")


@router.delete(
    "/space-invites/{invite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Spaces"],
)
def cancel_space_invite(
    request: Request,
    invite_id: int,
    session: Session = Depends(get_db_session),
) -> Response:
    """Cancel a pending invitation created by the caller."""

    space_dao = SpaceDAO(session)
    invite = space_dao.get_invite(invite_id)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invite not found.",
        )
    if invite.invited_by != request.state.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to cancel this invite.",
        )
    _require_pending_invite(invite)
    space_dao.cancel_invite(invite)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
