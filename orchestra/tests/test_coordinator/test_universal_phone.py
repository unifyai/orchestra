"""Coordinator universal phone provisioning tests."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from orchestra.db.dao.shared_pool_dao import SharedPoolDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    SharedPoolNumber,
    User,
)
from orchestra.services.coordinator_service import create_workspace_coordinator
from orchestra.settings import settings


def _make_user(
    dbsession: Session,
    suffix: str,
    *,
    phone_number: str | None = None,
) -> User:
    user = User(
        id=f"droid-phone-{suffix}",
        email=f"droid-phone-{suffix}@test.com",
        phone_number=phone_number,
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _active_phone_contact(
    dbsession: Session,
    assistant: Assistant,
) -> AssistantContact | None:
    return (
        dbsession.query(AssistantContact)
        .filter(
            AssistantContact.assistant_id == assistant.agent_id,
            AssistantContact.contact_type == "phone",
            AssistantContact.status == "active",
        )
        .first()
    )


@pytest.fixture(autouse=True)
def universal_phone_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "droid_coordinator_phone_us", "+14155552671")
    monkeypatch.setattr(settings, "droid_coordinator_phone_uk", "+447911123456")
    monkeypatch.setattr(settings, "droid_coordinator_default_phone_country", "US")


def test_create_workspace_coordinator_attaches_universal_phone_by_preferred_country(
    dbsession: Session,
) -> None:
    user = _make_user(dbsession, "create", phone_number="+14155550123")

    coordinator, created = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
        preferred_phone_country="GB",
    )

    contact = _active_phone_contact(dbsession, coordinator)
    pool = (
        dbsession.query(SharedPoolNumber)
        .filter(
            SharedPoolNumber.platform == "phone",
            SharedPoolNumber.number == "+447911123456",
        )
        .one()
    )
    assert created is True
    assert pool.status == "active"
    assert contact is not None
    assert contact.contact_value == pool.number
    assert contact.country_code == "GB"
    assert contact.provider == "twilio"
    assert contact.provisioned_by == "platform"
    assert contact.metadata_["universal_droid"] is True
    assert contact.metadata_["assignment_source"] == "geo"


def test_existing_workspace_coordinator_repair_preserves_universal_phone_country(
    dbsession: Session,
) -> None:
    user = _make_user(dbsession, "repair", phone_number="+14155550124")
    coordinator, created = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
        preferred_phone_country="GB",
    )
    assert created is True
    assert _active_phone_contact(dbsession, coordinator).country_code == "GB"

    repaired, repaired_created = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
    )

    contact = _active_phone_contact(dbsession, repaired)
    assert repaired_created is False
    assert repaired.agent_id == coordinator.agent_id
    assert contact is not None
    assert contact.contact_value == "+447911123456"
    assert contact.country_code == "GB"


def test_phone_pool_resolves_verified_owner_to_their_coordinator(
    dbsession: Session,
) -> None:
    owner = _make_user(dbsession, "owner", phone_number="+14155550125")
    other = _make_user(dbsession, "other", phone_number="+14155550126")
    owner_coordinator, _ = create_workspace_coordinator(
        dbsession,
        user_id=owner.id,
        organization_id=None,
        preferred_phone_country="US",
    )
    create_workspace_coordinator(
        dbsession,
        user_id=other.id,
        organization_id=None,
        preferred_phone_country="US",
    )

    result = SharedPoolDAO(dbsession, platform="phone").resolve_inbound(
        "+14155552671",
        "+14155550125",
    )

    assert result == {"assistant_id": owner_coordinator.agent_id, "role": "owner"}
