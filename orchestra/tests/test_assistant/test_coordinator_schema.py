"""Schema tests for Coordinator assistants."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestra.db.models.coordinator_voice import (
    COORDINATOR_DEFAULT_VOICE_ID,
    COORDINATOR_DEFAULT_VOICE_PROVIDER,
)
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    Assistant,
    ContactMembership,
    Organization,
    User,
    Voice,
)
from orchestra.services.coordinator_service import create_coordinator_assistant
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
        first_name="T-W1N",
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


def test_coordinator_voice_defaults_on_insert(dbsession: Session) -> None:
    """Coordinator rows fall back to the default voice when none is chosen."""
    owner = _make_user(dbsession, "coordinator-voice-default")

    assistant = _make_assistant(dbsession, owner, is_coordinator=True)

    assert assistant.voice_id == COORDINATOR_DEFAULT_VOICE_ID
    assert assistant.voice_provider == COORDINATOR_DEFAULT_VOICE_PROVIDER
    voice = (
        dbsession.query(Voice)
        .filter_by(
            user_id=owner.id,
            voice_id=COORDINATOR_DEFAULT_VOICE_ID,
            provider=COORDINATOR_DEFAULT_VOICE_PROVIDER,
        )
        .one()
    )
    assert voice.is_preset is True


def test_coordinator_voice_explicit_choice_is_kept_on_insert(
    dbsession: Session,
) -> None:
    """An explicitly chosen Coordinator voice is not overwritten by the default."""
    owner = _make_user(dbsession, "coordinator-voice-explicit")
    chosen = Voice(
        user_id=owner.id,
        voice_id="chosen-on-create",
        provider="cartesia",
        name="Chosen On Create",
        description="A voice picked at coordinator creation.",
        language="en",
        is_preset=True,
    )
    dbsession.add(chosen)
    dbsession.flush()

    assistant = Assistant(
        user_id=owner.id,
        first_name="T-W1N",
        surname="Assistant",
        is_coordinator=True,
        voice_id=chosen.voice_id,
        voice_provider=chosen.provider,
    )
    dbsession.add(assistant)
    dbsession.flush()
    dbsession.refresh(assistant)

    assert assistant.voice_id == "chosen-on-create"
    assert assistant.voice_provider == "cartesia"


def test_coordinator_voice_can_be_updated_after_insert(dbsession: Session) -> None:
    """Coordinator rows can use any registered voice."""
    owner = _make_user(dbsession, "coordinator-voice-update")
    assistant = _make_assistant(dbsession, owner, is_coordinator=True)
    custom_voice = Voice(
        user_id=owner.id,
        voice_id="coordinator-custom-voice",
        provider="elevenlabs",
        name="Coordinator Custom Voice",
        description="A configurable Coordinator voice.",
        language="en",
        is_preset=True,
    )
    dbsession.add(custom_voice)
    dbsession.flush()

    assistant.voice_id = custom_voice.voice_id
    assistant.voice_provider = custom_voice.provider
    dbsession.flush()
    dbsession.refresh(assistant)

    assert assistant.voice_id == custom_voice.voice_id
    assert assistant.voice_provider == custom_voice.provider


def test_create_coordinator_assistant_stamps_default_voice(dbsession: Session) -> None:
    """Coordinator creation sets Field Signal even without relying on ORM hooks."""
    owner = _make_user(dbsession, "create-coordinator-voice")
    assistant = create_coordinator_assistant(
        session=dbsession,
        owner_user_id=owner.id,
        organization_id=None,
    )
    assert assistant.voice_id == COORDINATOR_DEFAULT_VOICE_ID
    assert assistant.voice_provider == COORDINATOR_DEFAULT_VOICE_PROVIDER


def test_assistant_read_projects_coordinator_flag(dbsession: Session) -> None:
    """Assistant reads carry the Coordinator role flag as a concrete boolean."""
    owner = _make_user(dbsession, "read-projection")
    assistant = _make_assistant(dbsession, owner, is_coordinator=True)
    _make_personal_contact_memberships(dbsession, assistant)

    assistant_read = _build_assistant_read(assistant, dbsession)

    assert assistant_read.is_coordinator is True
