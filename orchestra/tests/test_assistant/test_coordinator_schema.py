"""Schema tests for Coordinator assistants and team-only spaces."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, Organization, Space, User
from orchestra.web.api.assistant.views import _build_assistant_read


def _make_user(dbsession: Session, suffix: str) -> User:
    user = User(
        id=f"coordinator-user-{suffix}",
        email=f"coordinator-user-{suffix}@test.com",
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
        name=f"Coordinator Org {suffix}",
    )
    dbsession.add(organization)
    dbsession.flush()
    return organization


def _make_assistant(
    dbsession: Session,
    owner: User,
    *,
    organization: Organization | None = None,
    is_coordinator: bool = False,
) -> Assistant:
    assistant = Assistant(
        user_id=owner.id,
        organization_id=organization.id if organization else None,
        first_name="Coordinator",
        surname="Assistant",
        is_coordinator=is_coordinator,
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _make_space(
    dbsession: Session,
    owner: User,
    suffix: str,
    *,
    organization: Organization | None = None,
    kind: str | None = None,
) -> Space:
    space = Space(
        name=f"Coordinator Space {suffix}",
        description=f"Coordinator schema workspace for {suffix} tests.",
        owner_user_id=owner.id,
        organization_id=organization.id if organization else None,
    )
    if kind is not None:
        space.kind = kind
    dbsession.add(space)
    dbsession.flush()
    return space


def test_personal_coordinator_unique_index_scopes_to_personal_rows(
    dbsession: Session,
) -> None:
    """A user can have one personal Coordinator while regular assistants remain allowed."""
    owner = _make_user(dbsession, "personal-unique")
    organization = _make_organization(dbsession, owner, "personal-unique")
    _make_assistant(dbsession, owner, is_coordinator=True)
    _make_assistant(dbsession, owner)
    _make_assistant(dbsession, owner, organization=organization, is_coordinator=True)

    duplicate = Assistant(
        user_id=owner.id,
        first_name="Duplicate",
        surname="Coordinator",
        is_coordinator=True,
    )
    dbsession.add(duplicate)
    with pytest.raises(
        IntegrityError,
        match="ux_assistants_one_personal_" "coordinator_per_user",
    ):
        dbsession.flush()


def test_org_coordinator_unique_index_scopes_to_org_rows(
    dbsession: Session,
) -> None:
    """Each organization can have one Coordinator while other scopes remain valid."""
    owner = _make_user(dbsession, "org-coordinator-unique")
    organization = _make_organization(dbsession, owner, "org-coordinator-unique")
    other_org = _make_organization(dbsession, owner, "org-coordinator-other")
    _make_assistant(dbsession, owner, organization=organization, is_coordinator=True)
    _make_assistant(dbsession, owner, organization=organization)
    _make_assistant(dbsession, owner, organization=other_org, is_coordinator=True)
    _make_assistant(dbsession, owner, is_coordinator=True)

    duplicate = Assistant(
        user_id=owner.id,
        organization_id=organization.id,
        first_name="Duplicate",
        surname="Coordinator",
        is_coordinator=True,
    )
    dbsession.add(duplicate)
    with pytest.raises(
        IntegrityError,
        match="ux_assistants_one_coordinator_per_org",
    ):
        dbsession.flush()


def test_is_coordinator_is_immutable_after_persistence(
    dbsession: Session,
) -> None:
    """Coordinator role assignment is allowed on insert but not after persistence."""
    owner = _make_user(dbsession, "coordinator-immutable")
    assistant = _make_assistant(dbsession, owner, is_coordinator=True)

    with pytest.raises(ValueError, match="is_coordinator is immutable"):
        assistant.is_coordinator = False


def test_space_kind_defaults_to_team_and_rejects_org_default(
    dbsession: Session,
) -> None:
    """Spaces are team-only; org_default values are rejected."""
    owner = _make_user(dbsession, "org-default")
    organization = _make_organization(dbsession, owner, "org-default")

    default_space = _make_space(dbsession, owner, "default", organization=organization)
    assert default_space.kind == "team"
    _make_space(dbsession, owner, "team-sibling", organization=organization)

    rejected_space = Space(
        name="Rejected org default",
        description="Rejected workspace kind for coordinator schema tests.",
        owner_user_id=owner.id,
        organization_id=organization.id,
        kind="org_default",
    )
    dbsession.add(rejected_space)
    with pytest.raises(IntegrityError, match="ck_spaces_kind"):
        dbsession.flush()


def test_space_kind_rejects_unknown_values(dbsession: Session) -> None:
    """Only team spaces are valid kinds."""
    owner = _make_user(dbsession, "invalid-kind")
    space = Space(
        name="Archived Space",
        description="Archived space kind row used to exercise the kind constraint.",
        owner_user_id=owner.id,
        kind="archived",
    )
    dbsession.add(space)

    with pytest.raises(IntegrityError, match="ck_spaces_kind"):
        dbsession.flush()


def test_assistant_read_projects_coordinator_flag(dbsession: Session) -> None:
    """Assistant reads carry the Coordinator role flag as a concrete boolean."""
    owner = _make_user(dbsession, "read-projection")
    assistant = _make_assistant(dbsession, owner, is_coordinator=True)

    assistant_read = _build_assistant_read(assistant, dbsession)

    assert assistant_read.is_coordinator is True
