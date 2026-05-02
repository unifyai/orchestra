"""Schema tests for shared spaces, memberships, and invitations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantSpaceMembership,
    Organization,
    Space,
    SpaceInvite,
    User,
)


def _future_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=7)


def _make_user(dbsession: Session, suffix: str) -> User:
    user = User(
        id=f"space-user-{suffix}",
        email=f"space-user-{suffix}@test.com",
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_organization(
    dbsession: Session,
    owner: User,
    suffix: str,
) -> Organization:
    organization = Organization(
        owner_id=owner.id,
        name=f"Space Org {suffix}",
    )
    dbsession.add(organization)
    dbsession.flush()
    return organization


def _make_assistant(
    dbsession: Session,
    owner: User,
    organization: Organization | None = None,
) -> Assistant:
    assistant = Assistant(
        user_id=owner.id,
        organization_id=organization.id if organization else None,
        first_name="Space",
        surname="Bot",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _make_space(
    dbsession: Session,
    owner: User,
    suffix: str,
    organization: Organization | None = None,
    status: str | None = None,
) -> Space:
    space = Space(
        name=f"Shared Memory {suffix}",
        description=f"Shared memory workspace for {suffix} tests.",
        owner_user_id=owner.id,
        organization_id=organization.id if organization else None,
    )
    if status is not None:
        space.status = status
    dbsession.add(space)
    dbsession.flush()
    return space


def _make_invite(
    dbsession: Session,
    space: Space,
    assistant: Assistant,
    inviter: User,
    invited_owner: User,
    status: str | None = None,
) -> SpaceInvite:
    invite = SpaceInvite(
        space_id=space.space_id,
        assistant_id=assistant.agent_id,
        invited_by=inviter.id,
        invited_owner_id=invited_owner.id,
        expires_at=_future_expiry(),
    )
    if status is not None:
        invite.status = status
    dbsession.add(invite)
    dbsession.flush()
    return invite


def test_membership_composite_pk_enforced(dbsession: Session) -> None:
    """The junction key is exactly one live row per assistant and space."""
    owner = _make_user(dbsession, "membership")
    assistant = _make_assistant(dbsession, owner)
    first_space = _make_space(dbsession, owner, "membership-a")
    second_space = _make_space(dbsession, owner, "membership-b")

    dbsession.add_all(
        [
            AssistantSpaceMembership(
                assistant_id=assistant.agent_id,
                space_id=first_space.space_id,
                added_by=owner.id,
            ),
            AssistantSpaceMembership(
                assistant_id=assistant.agent_id,
                space_id=second_space.space_id,
                added_by=owner.id,
            ),
        ],
    )
    dbsession.flush()

    dbsession.add(
        AssistantSpaceMembership(
            assistant_id=assistant.agent_id,
            space_id=first_space.space_id,
            added_by=owner.id,
        ),
    )
    with pytest.raises(IntegrityError, match="assistant_space_memberships_pkey"):
        dbsession.flush()


@pytest.mark.parametrize("status", ["active", "deleting"])
def test_space_status_check_constraint_accepts_canonical_values(
    dbsession: Session,
    status: str,
) -> None:
    """Spaces accept only the lifecycle statuses the runtime guards read."""
    owner = _make_user(dbsession, f"space-status-{status}")
    space = _make_space(dbsession, owner, f"space-status-{status}", status=status)

    assert space.status == status
    assert space.created_at is not None
    assert space.updated_at is not None


@pytest.mark.parametrize("name", ["", "x" * 201])
def test_space_name_check_constraint_rejects_invalid_lengths(
    dbsession: Session,
    name: str,
) -> None:
    """Space names must be present and short enough for display surfaces."""
    owner = _make_user(dbsession, f"space-name-{len(name)}")
    space = Space(
        name=name,
        description="Valid description for invalid name constraint tests.",
        owner_user_id=owner.id,
    )
    dbsession.add(space)

    with pytest.raises(IntegrityError, match="ck_spaces_name_length"):
        dbsession.flush()


@pytest.mark.parametrize("description", ["short", "x" * 1001])
def test_space_description_check_constraint_rejects_invalid_lengths(
    dbsession: Session,
    description: str,
) -> None:
    """Space descriptions stay within the prompt context budget."""
    owner = _make_user(dbsession, f"space-description-{len(description)}")
    space = Space(
        name="Description constraint",
        description=description,
        owner_user_id=owner.id,
    )
    dbsession.add(space)

    with pytest.raises(IntegrityError, match="ck_spaces_description_length"):
        dbsession.flush()


def test_space_status_check_constraint_rejects_unknown_status(
    dbsession: Session,
) -> None:
    """Unknown space lifecycle states are rejected at the database boundary."""
    owner = _make_user(dbsession, "space-status-invalid")
    space = Space(
        name="Invalid status",
        description="Valid description for invalid status constraint tests.",
        owner_user_id=owner.id,
        status="archived",
    )
    dbsession.add(space)

    with pytest.raises(IntegrityError, match="ck_spaces_status"):
        dbsession.flush()


def test_space_invite_pending_unique_per_pair(dbsession: Session) -> None:
    """Only one pending invite can exist for a space and assistant pair."""
    inviter = _make_user(dbsession, "invite-unique-inviter")
    invited_owner = _make_user(dbsession, "invite-unique-owner")
    space = _make_space(dbsession, inviter, "invite-unique")
    assistant = _make_assistant(dbsession, invited_owner)

    _make_invite(
        dbsession,
        space,
        assistant,
        inviter,
        invited_owner,
        status="accepted",
    )
    _make_invite(dbsession, space, assistant, inviter, invited_owner)

    duplicate_pending = SpaceInvite(
        space_id=space.space_id,
        assistant_id=assistant.agent_id,
        invited_by=inviter.id,
        invited_owner_id=invited_owner.id,
        expires_at=_future_expiry(),
    )
    dbsession.add(duplicate_pending)
    with pytest.raises(IntegrityError, match="ix_space_invites_pending"):
        dbsession.flush()


@pytest.mark.parametrize(
    "status",
    ["pending", "accepted", "declined", "cancelled", "expired"],
)
def test_space_invite_status_check_constraint_accepts_canonical_values(
    dbsession: Session,
    status: str,
) -> None:
    """Invitation rows persist every canonical state-machine status."""
    inviter = _make_user(dbsession, f"invite-status-inviter-{status}")
    invited_owner = _make_user(dbsession, f"invite-status-owner-{status}")
    space = _make_space(dbsession, inviter, f"invite-status-{status}")
    assistant = _make_assistant(dbsession, invited_owner)

    invite = _make_invite(
        dbsession,
        space,
        assistant,
        inviter,
        invited_owner,
        status=status,
    )

    assert invite.status == status


def test_space_invite_status_check_constraint_rejects_revoked(
    dbsession: Session,
) -> None:
    """The cancel-by-inviter terminal state is named cancelled."""
    inviter = _make_user(dbsession, "invite-status-invalid-inviter")
    invited_owner = _make_user(dbsession, "invite-status-invalid-owner")
    space = _make_space(dbsession, inviter, "invite-status-invalid")
    assistant = _make_assistant(dbsession, invited_owner)
    invite = SpaceInvite(
        space_id=space.space_id,
        assistant_id=assistant.agent_id,
        invited_by=inviter.id,
        invited_owner_id=invited_owner.id,
        status="revoked",
        expires_at=_future_expiry(),
    )
    dbsession.add(invite)

    with pytest.raises(IntegrityError, match="ck_space_invites_status"):
        dbsession.flush()


def test_organization_delete_restricts_when_spaces_exist(
    dbsession: Session,
) -> None:
    """Organization rows cannot disappear while spaces still reference them."""
    owner = _make_user(dbsession, "org-restrict")
    organization = _make_organization(dbsession, owner, "restrict")
    _make_space(dbsession, owner, "org-restrict", organization=organization)

    with pytest.raises(IntegrityError, match="spaces_organization_id_fkey"):
        dbsession.execute(
            sa.delete(Organization).where(Organization.id == organization.id),
        )


def test_space_delete_cascades_to_memberships_and_invites(
    dbsession: Session,
) -> None:
    """Deleting a space removes membership and invitation rows it owns."""
    owner = _make_user(dbsession, "space-cascade-owner")
    invited_owner = _make_user(dbsession, "space-cascade-invited")
    space = _make_space(dbsession, owner, "space-cascade")
    member_assistant = _make_assistant(dbsession, owner)
    invited_assistant = _make_assistant(dbsession, invited_owner)
    dbsession.add(
        AssistantSpaceMembership(
            assistant_id=member_assistant.agent_id,
            space_id=space.space_id,
            added_by=owner.id,
        ),
    )
    _make_invite(dbsession, space, invited_assistant, owner, invited_owner)

    dbsession.execute(sa.delete(Space).where(Space.space_id == space.space_id))
    dbsession.flush()

    membership_count = dbsession.scalar(
        sa.select(sa.func.count()).select_from(AssistantSpaceMembership),
    )
    invite_count = dbsession.scalar(
        sa.select(sa.func.count()).select_from(SpaceInvite),
    )
    assert membership_count == 0
    assert invite_count == 0
