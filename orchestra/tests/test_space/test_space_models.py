"""Schema tests for shared spaces and memberships."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantSpaceMembership,
    Organization,
    Space,
    User,
)


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


def test_space_delete_cascades_to_memberships(
    dbsession: Session,
) -> None:
    """Deleting a space removes membership rows it owns."""
    owner = _make_user(dbsession, "space-cascade-owner")
    space = _make_space(dbsession, owner, "space-cascade")
    member_assistant = _make_assistant(dbsession, owner)
    dbsession.add(
        AssistantSpaceMembership(
            assistant_id=member_assistant.agent_id,
            space_id=space.space_id,
            added_by=owner.id,
        ),
    )

    dbsession.execute(sa.delete(Space).where(Space.space_id == space.space_id))
    dbsession.flush()

    membership_count = dbsession.scalar(
        sa.select(sa.func.count()).select_from(AssistantSpaceMembership),
    )
    assert membership_count == 0
