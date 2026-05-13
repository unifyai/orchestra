"""REST endpoints for shared space lifecycle operations."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.space_dao import SPACE_STATUS_ACTIVE, SpaceDAO
from orchestra.db.dependencies import get_db_session
from orchestra.db.models.orchestra_models import Assistant, Space
from orchestra.services.coordinator_service import (
    ensure_personal_coordinator_provisioned,
    get_org_coordinator,
    get_personal_coordinator,
)
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
from orchestra.web.api.space.schema import (
    SpaceCreate,
    SpaceMember,
    SpaceMemberCreate,
    SpaceMembershipResponse,
    SpaceMembershipStatus,
    SpaceRead,
    SpaceSummary,
    SpaceUpdate,
)
from orchestra.web.api.utils.assistant_infra import delete_pubsub_topic

router = APIRouter()


def _space_read(space: Space) -> SpaceRead:
    """Build the public representation of a space."""

    return SpaceRead.model_validate(space)


def _space_summary(space: Space) -> SpaceSummary:
    """Build the compact representation of a space."""

    return SpaceSummary.model_validate(space)


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


def _membership_response(
    *,
    assistant_id: int,
    space_id: int,
) -> SpaceMembershipResponse:
    """Build the response for adding a member."""

    return SpaceMembershipResponse(
        membership_status=SpaceMembershipStatus.active,
        assistant_id=assistant_id,
        space_id=space_id,
    )


def _require_org_member_target(
    org_member_dao: OrganizationMemberDAO,
    *,
    space: Space,
    member_user_id: str,
) -> None:
    """Require member-targeted adds to reference an active org member."""

    if space.organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="member_target_requires_org_space",
        )
    if org_member_dao.get_member(member_user_id, space.organization_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization member not found.",
        )


def _require_membership_target_allowed(
    org_member_dao: OrganizationMemberDAO,
    *,
    actor_user_id: str,
    space: Space,
    assistant: Assistant,
) -> None:
    """Require the target assistant to be eligible for the space."""

    if space.organization_id is None:
        if assistant.user_id != actor_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="assistant_not_eligible_for_space",
            )
        return

    if assistant.organization_id == space.organization_id:
        return

    if assistant.user_id == actor_user_id:
        return

    if assistant.organization_id is None and assistant.is_coordinator:
        if org_member_dao.get_member(assistant.user_id, space.organization_id):
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="personal_coordinator_not_org_member",
        )

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="assistant_not_eligible_for_space",
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
async def create_space(
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
    refresh_payloads = []
    if body.organization_id is not None:
        coordinator = get_org_coordinator(session, body.organization_id)
        if (
            coordinator is not None
            and space_dao.get_membership(
                space_id=space.space_id,
                assistant_id=coordinator.agent_id,
            )
            is None
        ):
            space_dao.add_membership(
                space=space,
                assistant=coordinator,
                added_by=user_id,
            )
            refresh_payloads = membership_refresh_payloads(session, [coordinator])
    session.commit()
    await publish_membership_refreshes_best_effort(refresh_payloads)
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
    response: Response,
    space_id: int,
    body: SpaceMemberCreate,
    session: Session = Depends(get_db_session),
) -> SpaceMembershipResponse:
    """Add an eligible assistant to a space using direct membership."""

    user_id = request.state.user_id
    space_dao = SpaceDAO(session)
    org_member_dao = OrganizationMemberDAO(session)
    space = _get_space_or_404(space_dao, space_id)
    _require_space_mutation(space_dao, user_id, space)
    _require_active_space(space)

    created_personal_coordinator = False
    created_personal_coordinator_id: int | None = None
    try:
        if body.member_user_id:
            _require_org_member_target(
                org_member_dao,
                space=space,
                member_user_id=body.member_user_id,
            )
            existing_personal_coordinator = get_personal_coordinator(
                session,
                body.member_user_id,
            )
            if existing_personal_coordinator is not None:
                _require_membership_target_allowed(
                    org_member_dao,
                    actor_user_id=user_id,
                    space=space,
                    assistant=existing_personal_coordinator,
                )
                if space_dao.get_membership(
                    space_id=space.space_id,
                    assistant_id=existing_personal_coordinator.agent_id,
                ):
                    response.status_code = status.HTTP_200_OK
                    return _membership_response(
                        assistant_id=existing_personal_coordinator.agent_id,
                        space_id=space.space_id,
                    )
            try:
                assistant, created_personal_coordinator = (
                    await ensure_personal_coordinator_provisioned(
                        session,
                        user_id=body.member_user_id,
                    )
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="personal_coordinator_provisioning_failed",
                ) from exc
            created_personal_coordinator_id = assistant.agent_id
        else:
            if body.assistant_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="assistant_id_or_member_user_id_required",
                )
            assistant = _get_assistant_or_404(space_dao, body.assistant_id)
        _require_membership_target_allowed(
            org_member_dao,
            actor_user_id=user_id,
            space=space,
            assistant=assistant,
        )

        if space_dao.get_membership(
            space_id=space.space_id,
            assistant_id=assistant.agent_id,
        ):
            response.status_code = status.HTTP_200_OK
            return _membership_response(
                assistant_id=assistant.agent_id,
                space_id=space.space_id,
            )

        space_dao.add_membership(
            space=space,
            assistant=assistant,
            added_by=user_id,
        )
        refresh_payloads = membership_refresh_payloads(session, [assistant])
        session.commit()
    except Exception:
        session.rollback()
        if created_personal_coordinator and created_personal_coordinator_id is not None:
            await delete_pubsub_topic(str(created_personal_coordinator_id))
        raise

    await publish_membership_refreshes_best_effort(refresh_payloads)
    return _membership_response(
        assistant_id=assistant.agent_id,
        space_id=space.space_id,
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
