"""Tests for cron-driven space invitation expiry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.space_invite_dao import (
    SPACE_INVITE_STATUS_ACCEPTED,
    SPACE_INVITE_STATUS_CANCELLED,
    SPACE_INVITE_STATUS_DECLINED,
    SPACE_INVITE_STATUS_EXPIRED,
    SPACE_INVITE_STATUS_PENDING,
    SpaceInviteDAO,
)
from orchestra.db.models.orchestra_models import Assistant, Space, SpaceInvite, User
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


def _past_expiry() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


def _future_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=1)


def _unique_suffix(name: str) -> str:
    return f"{name}-{uuid4().hex}"


def _make_user(dbsession: Session, name: str) -> User:
    suffix = _unique_suffix(name)
    user = User(
        id=f"space-expiry-user-{suffix}",
        email=f"space-expiry-user-{suffix}@test.com",
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_assistant(dbsession: Session, owner: User) -> Assistant:
    assistant = Assistant(
        user_id=owner.id,
        first_name="Expiry",
        surname="Bot",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


def _make_space(dbsession: Session, owner: User, name: str) -> Space:
    space = Space(
        name=f"Expiry Space {name}",
        owner_user_id=owner.id,
    )
    dbsession.add(space)
    dbsession.flush()
    return space


def _make_invite(
    dbsession: Session,
    *,
    space: Space,
    assistant: Assistant,
    inviter: User,
    invited_owner: User,
    status_value: str = SPACE_INVITE_STATUS_PENDING,
    expires_at: datetime | None = None,
    decided_at: datetime | None = None,
) -> SpaceInvite:
    invite = SpaceInvite(
        space_id=space.space_id,
        assistant_id=assistant.agent_id,
        invited_by=inviter.id,
        invited_owner_id=invited_owner.id,
        status=status_value,
        expires_at=expires_at or _future_expiry(),
        decided_at=decided_at,
    )
    dbsession.add(invite)
    dbsession.flush()
    return invite


def _make_invite_fixture(
    dbsession: Session,
    name: str,
    *,
    status_value: str = SPACE_INVITE_STATUS_PENDING,
    expires_at: datetime | None = None,
    decided_at: datetime | None = None,
) -> SpaceInvite:
    inviter = _make_user(dbsession, f"{name}-inviter")
    invited_owner = _make_user(dbsession, f"{name}-owner")
    assistant = _make_assistant(dbsession, invited_owner)
    space = _make_space(dbsession, inviter, name)
    return _make_invite(
        dbsession,
        space=space,
        assistant=assistant,
        inviter=inviter,
        invited_owner=invited_owner,
        status_value=status_value,
        expires_at=expires_at,
        decided_at=decided_at,
    )


def test_expire_pending_invites_transitions_only_past_pending_rows(
    dbsession: Session,
) -> None:
    """Expired pending invitations transition while future and terminal rows stay put."""

    past_pending = _make_invite_fixture(
        dbsession,
        "past-pending",
        expires_at=_past_expiry(),
    )
    future_pending = _make_invite_fixture(
        dbsession,
        "future-pending",
        expires_at=_future_expiry(),
    )
    decided_at = datetime.now(timezone.utc) - timedelta(hours=2)
    terminal_invites = [
        (
            status_value,
            _make_invite_fixture(
                dbsession,
                f"terminal-{status_value}",
                status_value=status_value,
                expires_at=_past_expiry(),
                decided_at=decided_at,
            ),
        )
        for status_value in (
            SPACE_INVITE_STATUS_ACCEPTED,
            SPACE_INVITE_STATUS_DECLINED,
            SPACE_INVITE_STATUS_CANCELLED,
            SPACE_INVITE_STATUS_EXPIRED,
        )
    ]

    transitioned_count = SpaceInviteDAO(dbsession).expire_pending_invites()

    assert transitioned_count == 1
    dbsession.refresh(past_pending)
    dbsession.refresh(future_pending)
    assert past_pending.status == SPACE_INVITE_STATUS_EXPIRED
    assert past_pending.decided_at is not None
    assert future_pending.status == SPACE_INVITE_STATUS_PENDING
    assert future_pending.decided_at is None
    for expected_status, invite in terminal_invites:
        dbsession.refresh(invite)
        assert invite.status == expected_status
        assert invite.decided_at == decided_at

    replacement = SpaceInvite(
        space_id=past_pending.space_id,
        assistant_id=past_pending.assistant_id,
        invited_by=past_pending.invited_by,
        invited_owner_id=past_pending.invited_owner_id,
        expires_at=_future_expiry(),
    )
    dbsession.add(replacement)
    dbsession.flush()

    pending_count = dbsession.scalar(
        sa.select(sa.func.count())
        .select_from(SpaceInvite)
        .where(
            SpaceInvite.space_id == past_pending.space_id,
            SpaceInvite.assistant_id == past_pending.assistant_id,
            SpaceInvite.status == SPACE_INVITE_STATUS_PENDING,
        ),
    )
    assert pending_count == 1


@pytest.mark.anyio
async def test_cleanup_expired_space_invites_endpoint_is_admin_only_and_idempotent(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """The admin cleanup endpoint transitions due invitations exactly once."""

    regular_user = await create_test_user(
        client,
        f"space-expiry-regular-{uuid4().hex}@test.com",
    )
    unauthorized = await client.post(
        "/v0/admin/cleanup/expired-space-invites",
        headers=regular_user["headers"],
    )
    assert unauthorized.status_code in {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    }

    expired_pending = _make_invite_fixture(
        dbsession,
        "endpoint-past-pending",
        expires_at=_past_expiry(),
    )
    future_pending = _make_invite_fixture(
        dbsession,
        "endpoint-future-pending",
        expires_at=_future_expiry(),
    )
    dbsession.commit()

    response = await client.post(
        "/v0/admin/cleanup/expired-space-invites",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    body = response.json()
    assert set(body) == {"transitioned_count", "timestamp", "message"}
    assert body["transitioned_count"] == 1
    assert body["message"] == "Transitioned 1 pending space-invite(s) to expired"
    datetime.fromisoformat(body["timestamp"])

    dbsession.expire_all()
    assert dbsession.get(SpaceInvite, expired_pending.invite_id).status == (
        SPACE_INVITE_STATUS_EXPIRED
    )
    assert dbsession.get(SpaceInvite, expired_pending.invite_id).decided_at is not None
    assert dbsession.get(SpaceInvite, future_pending.invite_id).status == (
        SPACE_INVITE_STATUS_PENDING
    )
    assert dbsession.get(SpaceInvite, future_pending.invite_id).decided_at is None

    second_response = await client.post(
        "/v0/admin/cleanup/expired-space-invites",
        headers=ADMIN_HEADERS,
    )
    assert second_response.status_code == status.HTTP_200_OK, second_response.json()
    assert second_response.json()["transitioned_count"] == 0
    assert second_response.json()["message"] == (
        "Transitioned 0 pending space-invite(s) to expired"
    )


def test_expired_space_invites_workflow_calls_admin_cleanup_endpoint() -> None:
    """The scheduled workflow points at the admin cleanup route."""

    workflow_path = (
        Path(__file__).resolve().parents[3]
        / ".github"
        / "workflows"
        / "cleanup-expired-space-invites.yml"
    )

    workflow = workflow_path.read_text()

    assert "/v0/admin/cleanup/expired-space-invites" in workflow
    assert "secrets.ORCHESTRA_ADMIN_KEY" in workflow
    assert "# schedule:" in workflow
