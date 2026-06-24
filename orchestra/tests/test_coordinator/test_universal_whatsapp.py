"""Coordinator universal WhatsApp provisioning tests."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from orchestra.db.dao.shared_pool_dao import SharedPoolDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    SharedPlatformRoute,
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
    monkeypatch.setattr(settings, "unity_coordinator_whatsapp_number", "+15550700001")
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
    monkeypatch.setattr(settings, "unity_coordinator_whatsapp_number", None)
    user = _make_user(dbsession, "repair")
    coordinator, created = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
    )
    assert created is True
    assert _active_whatsapp_contact(dbsession, coordinator) is None

    monkeypatch.setattr(settings, "unity_coordinator_whatsapp_number", "+15550700002")
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


def _setup_universal_coordinator(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    suffix: str,
    pool_number: str,
    sender: str,
) -> Assistant:
    monkeypatch.setattr(settings, "unity_coordinator_whatsapp_number", pool_number)
    user = _make_user(dbsession, suffix)
    user.whatsapp_number = sender
    dbsession.flush()
    coordinator, _ = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
    )
    dbsession.flush()
    return coordinator


def test_universal_inbound_opens_freeform_window(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inbound from the owner must persist last_inbound_at so the outbound
    free-form window opens (otherwise every reply is forced to a template)."""
    pool_number = "+15550700010"
    sender = "+15551230010"
    coordinator = _setup_universal_coordinator(
        dbsession,
        monkeypatch,
        suffix="inbound",
        pool_number=pool_number,
        sender=sender,
    )

    dao = SharedPoolDAO(dbsession)
    result = dao.resolve_inbound(pool_number, sender)
    assert result == {"assistant_id": coordinator.agent_id, "role": "owner"}

    pool = dao.get_pool_number_by_value(pool_number)
    route = (
        dbsession.query(SharedPlatformRoute)
        .filter(
            SharedPlatformRoute.pool_number_id == pool.id,
            SharedPlatformRoute.contact_number == sender,
        )
        .one()
    )
    assert route.last_inbound_at is not None

    # Outbound now reuses the persisted route, so the window reads as open.
    built, resolution = dao.get_or_create_route(coordinator.agent_id, sender)
    assert resolution is None
    assert built.id == route.id
    assert built.last_inbound_at is not None


def test_universal_outbound_without_inbound_is_window_closed(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no prior inbound, the universal owner route has no last_inbound_at,
    so the window is closed and the gateway correctly falls back to a template."""
    pool_number = "+15550700011"
    sender = "+15551230011"
    coordinator = _setup_universal_coordinator(
        dbsession,
        monkeypatch,
        suffix="outbound",
        pool_number=pool_number,
        sender=sender,
    )

    dao = SharedPoolDAO(dbsession)
    built, resolution = dao.get_or_create_route(coordinator.agent_id, sender)
    assert resolution is None
    assert built.last_inbound_at is None
