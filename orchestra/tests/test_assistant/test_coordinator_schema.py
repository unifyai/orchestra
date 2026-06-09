"""Schema tests for Coordinator assistants."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    Assistant,
    ContactMembership,
    Organization,
    User,
)
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
        first_name="Unity",
        surname="Assistant",
        is_coordinator=is_coordinator,
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _make_personal_contact_memberships(
    dbsession: Session,
    assistant: Assistant,
) -> None:
    dbsession.add_all(
        [
            ContactMembership(
                assistant_id=assistant.agent_id,
                authoring_assistant_id=assistant.agent_id,
                contact_id=0,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                should_respond=True,
                response_policy="",
                can_edit=True,
            ),
            ContactMembership(
                assistant_id=assistant.agent_id,
                authoring_assistant_id=assistant.agent_id,
                contact_id=1,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                should_respond=True,
                response_policy="",
                can_edit=True,
            ),
        ],
    )
    dbsession.flush()


def test_personal_coordinator_unique_index_scopes_to_personal_rows(
    dbsession: Session,
) -> None:
    """A user can have one personal Coordinator while regular assistants remain allowed."""
    owner = _make_user(dbsession, "personal-unique")
    _make_assistant(dbsession, owner, is_coordinator=True)
    _make_assistant(dbsession, owner)
    _make_assistant(
        dbsession,
        owner,
        organization=_make_organization(dbsession, owner, "personal-unique"),
    )

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
        match="ux_assistants_one_workspace_coordinator_per_membership",
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


def test_assistant_read_projects_coordinator_flag(dbsession: Session) -> None:
    """Assistant reads carry the Coordinator role flag as a concrete boolean."""
    owner = _make_user(dbsession, "read-projection")
    assistant = _make_assistant(dbsession, owner, is_coordinator=True)
    _make_personal_contact_memberships(dbsession, assistant)

    assistant_read = _build_assistant_read(assistant, dbsession)

    assert assistant_read.is_coordinator is True
