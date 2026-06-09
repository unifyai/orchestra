"""Coordinator universal WhatsApp provisioning tests."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    SharedPoolNumber,
    User,
)
from orchestra.services.coordinator_service import create_workspace_coordinator
from orchestra.settings import settings


def _make_user(dbsession: Session, suffix: str) -> User:
    user = User(
        id=f"unity-whatsapp-{suffix}",
        email=f"unity-whatsapp-{suffix}@test.com",
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _active_whatsapp_contact(
    dbsession: Session,
    assistant: Assistant,
) -> AssistantContact | None:
    return (
        dbsession.query(AssistantContact)
        .filter(
            AssistantContact.assistant_id == assistant.agent_id,
            AssistantContact.contact_type == "whatsapp",
            AssistantContact.status == "active",
        )
        .first()
    )


def test_create_workspace_coordinator_attaches_universal_whatsapp(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "unity_whatsapp_pool_number", "+15550700001")
    user = _make_user(dbsession, "create")

    coordinator, created = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
    )

    contact = _active_whatsapp_contact(dbsession, coordinator)
    pool = (
        dbsession.query(SharedPoolNumber)
        .filter(
            SharedPoolNumber.platform == "whatsapp",
            SharedPoolNumber.number == "+15550700001",
        )
        .one()
    )
    assert created is True
    assert pool.status == "active"
    assert contact is not None
    assert contact.contact_value == pool.number
    assert contact.provider == "twilio"
    assert contact.provisioned_by == "platform"
    assert contact.metadata_ == {"universal_unity": True}


def test_existing_workspace_coordinator_repair_attaches_universal_whatsapp(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "unity_whatsapp_pool_number", None)
    user = _make_user(dbsession, "repair")
    coordinator, created = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
    )
    assert created is True
    assert _active_whatsapp_contact(dbsession, coordinator) is None

    monkeypatch.setattr(settings, "unity_whatsapp_pool_number", "+15550700002")
    repaired, repaired_created = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
    )

    contact = _active_whatsapp_contact(dbsession, repaired)
    assert repaired_created is False
    assert repaired.agent_id == coordinator.agent_id
    assert contact is not None
    assert contact.contact_value == "+15550700002"
