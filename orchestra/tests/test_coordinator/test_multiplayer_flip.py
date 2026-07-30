"""Tests for the one-way coordinator multiplayer flip."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    Organization,
    ResourceAccess,
    Role,
    User,
    Voice,
)
from orchestra.services.coordinator_multiplayer import (
    TWIN_ALIAS_EMAIL_METADATA,
    MultiplayerFlipError,
    find_twin_by_alias_email,
    flip_coordinator_to_multiplayer,
    generate_twin_alias_email,
    is_reserved_coordinator_name,
    is_twin_alias_email_address,
)
from orchestra.services.coordinator_service import create_coordinator_assistant
from orchestra.services.shared_coordinator_routing import find_owned_shared_coordinators
from orchestra.services.universal_unity_email import (
    ensure_coordinator_universal_email_contact,
)
from orchestra.settings import settings
from orchestra.web.api.assistant.views import _build_assistant_read


def _make_user(dbsession: Session, suffix: str) -> User:
    user = User(
        id=f"multiplayer-user-{suffix}",
        email=f"multiplayer-user-{suffix}@test.com",
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_coordinator(dbsession: Session, owner: User) -> Assistant:
    coordinator = create_coordinator_assistant(
        dbsession,
        owner_user_id=owner.id,
        organization_id=None,
    )
    ensure_coordinator_universal_email_contact(dbsession, coordinator=coordinator)
    return coordinator


def _make_voice(dbsession: Session, owner: User, voice_id: str = "picked") -> Voice:
    voice = Voice(
        user_id=owner.id,
        voice_id=voice_id,
        provider="elevenlabs",
        name="Picked Voice",
        description="Voice chosen during the multiplayer flip.",
        language="en",
        is_preset=True,
    )
    dbsession.add(voice)
    dbsession.flush()
    return voice


def _flip(dbsession: Session, coordinator: Assistant, **overrides) -> Assistant:
    kwargs = dict(
        first_name="Max",
        surname="Vector",
        voice_id="picked",
        voice_provider="elevenlabs",
    )
    kwargs.update(overrides)
    return flip_coordinator_to_multiplayer(dbsession, coordinator=coordinator, **kwargs)


def _active_contacts(
    dbsession: Session,
    assistant: Assistant,
) -> list[AssistantContact]:
    return AssistantContactDAO(dbsession).get_active_contacts_for_assistant(
        assistant.agent_id,
    )


def test_flip_applies_identity_and_swaps_contacts(dbsession: Session) -> None:
    owner = _make_user(dbsession, "happy")
    coordinator = _make_coordinator(dbsession, owner)
    _make_voice(dbsession, owner)
    # A second pool contact type proves the swap covers every universal row.
    AssistantContactDAO(dbsession).upsert_assistant_contact(
        assistant_id=coordinator.agent_id,
        contact_type="whatsapp",
        contact_value="+15550000001",
        provisioned_by="platform",
        metadata={"universal_unity": True},
    )
    dbsession.flush()

    flipped = _flip(dbsession, coordinator)

    assert flipped.is_multiplayer is True
    assert flipped.is_private_coordinator is False
    assert flipped.first_name == "Max"
    assert flipped.surname == "Vector"
    assert flipped.voice_id == "picked"

    contacts = _active_contacts(dbsession, flipped)
    assert len(contacts) == 1
    alias = contacts[0]
    assert alias.contact_type == "email"
    assert alias.metadata_ == TWIN_ALIAS_EMAIL_METADATA
    assert is_twin_alias_email_address(alias.contact_value)
    assert alias.contact_value.startswith("max.vector@")


def test_flip_rejects_default_name_and_blank(dbsession: Session) -> None:
    owner = _make_user(dbsession, "name")
    coordinator = _make_coordinator(dbsession, owner)
    _make_voice(dbsession, owner)
    with pytest.raises(MultiplayerFlipError, match="differ from the shared"):
        _flip(dbsession, coordinator, first_name="t-w1n")
    with pytest.raises(MultiplayerFlipError, match="first name is required"):
        _flip(dbsession, coordinator, first_name="   ")


def test_flip_rejects_non_coordinator_and_repeat(dbsession: Session) -> None:
    owner = _make_user(dbsession, "state")
    hire = Assistant(user_id=owner.id, first_name="Ada", is_coordinator=False)
    dbsession.add(hire)
    dbsession.flush()
    with pytest.raises(MultiplayerFlipError, match="Only coordinators"):
        _flip(dbsession, hire)

    coordinator = _make_coordinator(dbsession, owner)
    _make_voice(dbsession, owner)
    _flip(dbsession, coordinator)
    with pytest.raises(MultiplayerFlipError, match="already multiplayer"):
        _flip(dbsession, coordinator, first_name="Other")


def test_is_multiplayer_is_one_way_and_coordinator_only(dbsession: Session) -> None:
    owner = _make_user(dbsession, "oneway")
    hire = Assistant(user_id=owner.id, first_name="Ada", is_coordinator=False)
    dbsession.add(hire)
    dbsession.flush()
    with pytest.raises(ValueError, match="requires is_coordinator"):
        hire.is_multiplayer = True

    coordinator = _make_coordinator(dbsession, owner)
    _make_voice(dbsession, owner)
    _flip(dbsession, coordinator)
    dbsession.flush()
    with pytest.raises(ValueError, match="cannot be reverted"):
        coordinator.is_multiplayer = False


def test_alias_collision_falls_back_to_agent_id(dbsession: Session) -> None:
    owner = _make_user(dbsession, "collision")
    coordinator = _make_coordinator(dbsession, owner)
    domain = settings.unity_twin_alias_email_domain
    other_owner = _make_user(dbsession, "collision-other")
    squatter = Assistant(user_id=other_owner.id, first_name="Squat")
    dbsession.add(squatter)
    dbsession.flush()
    AssistantContactDAO(dbsession).upsert_assistant_contact(
        assistant_id=squatter.agent_id,
        contact_type="email",
        contact_value=f"max.vector@{domain}",
        provisioned_by="platform",
    )
    dbsession.flush()

    address = generate_twin_alias_email(
        dbsession,
        coordinator=coordinator,
        first_name="Max",
        surname="Vector",
    )
    assert address == f"max.vector.{coordinator.agent_id}@{domain}"


def test_pool_reattach_and_shared_routing_skip_multiplayer(
    dbsession: Session,
) -> None:
    owner = _make_user(dbsession, "routing")
    coordinator = _make_coordinator(dbsession, owner)
    _make_voice(dbsession, owner)
    _flip(dbsession, coordinator)
    dbsession.flush()

    # The heal path must not re-attach the shared mailbox.
    assert (
        ensure_coordinator_universal_email_contact(dbsession, coordinator=coordinator)
        is None
    )
    contacts = _active_contacts(dbsession, coordinator)
    assert [c.metadata_ for c in contacts] == [TWIN_ALIAS_EMAIL_METADATA]

    # Even with a stale pool row, sender-based routing must skip the twin.
    AssistantContactDAO(dbsession).upsert_assistant_contact(
        assistant_id=coordinator.agent_id,
        contact_type="whatsapp",
        contact_value="+15550000002",
        provisioned_by="platform",
        metadata={"universal_unity": True},
    )
    dbsession.flush()
    assert (
        find_owned_shared_coordinators(
            dbsession,
            user_id=owner.id,
            contact_type="whatsapp",
            contact_value="+15550000002",
        )
        == []
    )


def test_alias_lookup_resolves_multiplayer_twin(dbsession: Session) -> None:
    owner = _make_user(dbsession, "lookup")
    coordinator = _make_coordinator(dbsession, owner)
    _make_voice(dbsession, owner)
    flipped = _flip(dbsession, coordinator)
    dbsession.flush()
    alias = _active_contacts(dbsession, flipped)[0].contact_value

    resolved = find_twin_by_alias_email(dbsession, alias)
    assert resolved is not None
    assert resolved.agent_id == flipped.agent_id
    assert find_twin_by_alias_email(dbsession, "nobody@example.com") is None


def test_assistant_read_projects_multiplayer_flag(dbsession: Session) -> None:
    owner = _make_user(dbsession, "read")
    coordinator = _make_coordinator(dbsession, owner)
    _make_voice(dbsession, owner)
    read = _build_assistant_read(coordinator, dbsession)
    assert read.is_multiplayer is False
    _flip(dbsession, coordinator)
    read = _build_assistant_read(coordinator, dbsession)
    assert read.is_multiplayer is True


def _make_org(dbsession: Session, owner: User, suffix: str) -> Organization:
    org = Organization(owner_id=owner.id, name=f"Multiplayer Org {suffix}")
    dbsession.add(org)
    dbsession.flush()
    return org


def test_reserved_name_helper_matches_default_case_insensitively() -> None:
    assert is_reserved_coordinator_name("T-W1N")
    assert is_reserved_coordinator_name("  t-w1n  ")
    assert not is_reserved_coordinator_name("Max")
    assert not is_reserved_coordinator_name(None)


def test_flip_rejects_duplicate_org_display_name(dbsession: Session) -> None:
    owner = _make_user(dbsession, "unique")
    org = _make_org(dbsession, owner, "unique")
    hire = Assistant(
        user_id=owner.id,
        organization_id=org.id,
        first_name="Max",
        surname="Vector",
    )
    dbsession.add(hire)
    dbsession.flush()

    coordinator = create_coordinator_assistant(
        dbsession,
        owner_user_id=owner.id,
        organization_id=org.id,
    )
    _make_voice(dbsession, owner)
    with pytest.raises(MultiplayerFlipError, match="already named"):
        _flip(dbsession, coordinator, first_name="  max ", surname="VECTOR")

    # A distinct name passes, and personal-workspace twins never collide.
    flipped = _flip(dbsession, coordinator, first_name="Maxine", surname="Vector")
    assert flipped.is_multiplayer is True


def test_flip_grants_owner_assistant_access_for_org_twins(
    dbsession: Session,
) -> None:
    owner = _make_user(dbsession, "grants")
    org = _make_org(dbsession, owner, "grants")
    if (
        dbsession.query(Role)
        .filter(Role.name == "Owner", Role.organization_id.is_(None))
        .first()
    ) is None:
        dbsession.add(Role(name="Owner", organization_id=None))
        dbsession.flush()

    coordinator = create_coordinator_assistant(
        dbsession,
        owner_user_id=owner.id,
        organization_id=org.id,
    )
    _make_voice(dbsession, owner)
    _flip(dbsession, coordinator)

    grant = (
        dbsession.query(ResourceAccess)
        .filter(
            ResourceAccess.resource_type == "assistant",
            ResourceAccess.resource_id == coordinator.agent_id,
            ResourceAccess.grantee_type == "user",
            ResourceAccess.grantee_id == owner.id,
        )
        .first()
    )
    assert grant is not None, "flip must mirror the hire-creation Owner grant"
