"""Coordinator universal Discord provisioning tests."""

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
from orchestra.services.universal_droid_discord import is_universal_droid_discord_bot
from orchestra.settings import settings

BOT_ID = "1514612855071178964"
BOT_TOKEN = "fake.discord.bot.token"


def _make_user(dbsession: Session, suffix: str) -> User:
    user = User(
        id=f"droid-discord-{suffix}",
        email=f"droid-discord-{suffix}@test.com",
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _active_discord_contact(
    dbsession: Session,
    assistant: Assistant,
) -> AssistantContact | None:
    return (
        dbsession.query(AssistantContact)
        .filter(
            AssistantContact.assistant_id == assistant.agent_id,
            AssistantContact.contact_type == "discord",
            AssistantContact.status == "active",
        )
        .first()
    )


def _configure_bot(monkeypatch: pytest.MonkeyPatch, bot_id, token) -> None:
    monkeypatch.setattr(settings, "droid_coordinator_discord_id", bot_id)
    monkeypatch.setattr(settings, "droid_coordinator_discord_token", token)


def test_create_workspace_coordinator_attaches_universal_discord(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_bot(monkeypatch, BOT_ID, BOT_TOKEN)
    user = _make_user(dbsession, "create")

    coordinator, created = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
    )

    contact = _active_discord_contact(dbsession, coordinator)
    pool = (
        dbsession.query(SharedPoolNumber)
        .filter(
            SharedPoolNumber.platform == "discord",
            SharedPoolNumber.number == BOT_ID,
        )
        .one()
    )
    assert created is True
    assert pool.status == "active"
    assert pool.auth_token == BOT_TOKEN
    assert contact is not None
    assert contact.contact_value == BOT_ID
    assert contact.provider == "discord"
    assert contact.provisioned_by == "platform"
    assert contact.metadata_ == {"universal_droid": True}


def test_universal_discord_bot_excluded_from_pool_assignment(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_bot(monkeypatch, BOT_ID, BOT_TOKEN)
    user = _make_user(dbsession, "exclude")
    create_workspace_coordinator(dbsession, user_id=user.id, organization_id=None)

    assert is_universal_droid_discord_bot(BOT_ID) is True

    dao = SharedPoolDAO(dbsession, "discord")
    eligible = dao.find_eligible_pool_numbers(999999, [user.id])
    assert all(p.number != BOT_ID for p in eligible)


def test_existing_workspace_coordinator_repair_attaches_universal_discord(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_bot(monkeypatch, None, None)
    user = _make_user(dbsession, "repair")
    coordinator, created = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
    )
    assert created is True
    assert _active_discord_contact(dbsession, coordinator) is None

    _configure_bot(monkeypatch, BOT_ID, BOT_TOKEN)
    repaired, repaired_created = create_workspace_coordinator(
        dbsession,
        user_id=user.id,
        organization_id=None,
    )

    contact = _active_discord_contact(dbsession, repaired)
    assert repaired_created is False
    assert repaired.agent_id == coordinator.agent_id
    assert contact is not None
    assert contact.contact_value == BOT_ID


def test_universal_discord_token_rotation_updates_pool(
    dbsession: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_bot(monkeypatch, BOT_ID, BOT_TOKEN)
    user = _make_user(dbsession, "rotate")
    create_workspace_coordinator(dbsession, user_id=user.id, organization_id=None)

    _configure_bot(monkeypatch, BOT_ID, "rotated.discord.token")
    user2 = _make_user(dbsession, "rotate2")
    create_workspace_coordinator(dbsession, user_id=user2.id, organization_id=None)

    pool = (
        dbsession.query(SharedPoolNumber)
        .filter(
            SharedPoolNumber.platform == "discord",
            SharedPoolNumber.number == BOT_ID,
        )
        .one()
    )
    assert pool.auth_token == "rotated.discord.token"
