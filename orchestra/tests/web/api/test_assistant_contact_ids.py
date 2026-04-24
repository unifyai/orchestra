"""Tests for ``self_contact_id`` / ``boss_contact_id`` on ``AssistantRead``.

These exercise the ``resolve_membership_contact_ids`` helper, the
``/v0/assistant`` GET response, and the admin-list ``from_fields`` opt-out
that must skip the resolver when neither contact-id field is requested.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    LogEvent,
    LogEventContext,
    Project,
)
from orchestra.services.assistant_membership_service import (
    resolve_membership_contact_ids,
)
from orchestra.tests.utils import ADMIN_HEADERS, create_test_org, create_test_user


@pytest.fixture(autouse=True)
def mock_infra_calls():
    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake, patch(
        "orchestra.web.api.assistant.views.settings",
    ) as mock_settings:
        mock_wake.return_value = MagicMock(status_code=200)
        mock_settings.is_staging = True
        mock_settings.assistant_creation_cost = 0
        yield


@pytest.fixture
async def org_owner(client: AsyncClient):
    user = await create_test_user(client, "contact-ids-owner@test.local")
    org = await create_test_org(client, user, "ContactIdsOrg")
    return user, org


def _write_membership_row(
    session: Session,
    *,
    project_id: int,
    context_name: str,
    relationship: str,
    contact_id: int,
    created_at: datetime,
) -> None:
    context = session.execute(
        select(Context).where(
            Context.project_id == project_id,
            Context.name == context_name,
        ),
    ).scalar_one_or_none()
    if context is None:
        context = Context(project_id=project_id, name=context_name)
        session.add(context)
        session.flush()

    event = LogEvent(
        project_id=project_id,
        data={"relationship": relationship, "contact_id": contact_id},
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(event)
    session.flush()
    session.add(LogEventContext(log_event_id=event.id, context_id=context.id))
    session.flush()


def _assistants_project_id(session: Session, assistant: Assistant) -> int:
    query = select(Project.id).where(Project.name == "Assistants")
    if assistant.organization_id is not None:
        query = query.where(Project.organization_id == assistant.organization_id)
    else:
        query = query.where(
            Project.user_id == assistant.user_id,
            Project.organization_id.is_(None),
        )
    project_id = session.execute(query).scalar_one_or_none()
    assert project_id is not None, "Assistants project must exist for the body"
    return project_id


async def _create_assistant(client: AsyncClient, headers: dict, **extra) -> dict:
    payload = {"first_name": "Ada", "create_infra": False, **extra}
    resp = await client.post("/v0/assistant", json=payload, headers=headers)
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    return resp.json()["info"]


# ---------------------------------------------------------------------------
# resolve_membership_contact_ids
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resolve_returns_none_when_no_membership_rows(
    client: AsyncClient,
    org_owner,
    dbsession: Session,
):
    _, org = org_owner
    info = await _create_assistant(client, org["headers"])
    assistant = dbsession.get(Assistant, int(info["agent_id"]))

    self_cid, boss_cid = resolve_membership_contact_ids(dbsession, assistant)
    assert self_cid is None
    assert boss_cid is None


@pytest.mark.anyio
async def test_resolve_picks_latest_self_and_boss(
    client: AsyncClient,
    org_owner,
    dbsession: Session,
):
    _, org = org_owner
    info = await _create_assistant(client, org["headers"])
    assistant = dbsession.get(Assistant, int(info["agent_id"]))

    project_id = _assistants_project_id(dbsession, assistant)
    ctx_name = f"{assistant.user_id}/{assistant.agent_id}/ContactMembership"

    # Earlier rows
    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="self",
        contact_id=42,
        created_at=datetime(2026, 4, 1, 0, 0, 0),
    )
    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="boss",
        contact_id=43,
        created_at=datetime(2026, 4, 1, 0, 0, 0),
    )
    # Later rows must win
    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="self",
        contact_id=1001,
        created_at=datetime(2026, 4, 2, 0, 0, 0),
    )
    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="boss",
        contact_id=1002,
        created_at=datetime(2026, 4, 2, 0, 0, 0),
    )

    self_cid, boss_cid = resolve_membership_contact_ids(dbsession, assistant)
    assert self_cid == 1001
    assert boss_cid == 1002


@pytest.mark.anyio
async def test_resolve_skips_unknown_relationships_and_bad_payloads(
    client: AsyncClient,
    org_owner,
    dbsession: Session,
):
    _, org = org_owner
    info = await _create_assistant(client, org["headers"])
    assistant = dbsession.get(Assistant, int(info["agent_id"]))

    project_id = _assistants_project_id(dbsession, assistant)
    ctx_name = f"{assistant.user_id}/{assistant.agent_id}/ContactMembership"

    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="collaborator",
        contact_id=77,
        created_at=datetime(2026, 4, 1, 0, 0, 0),
    )
    # Missing contact_id — must be ignored.
    context = dbsession.execute(
        select(Context).where(
            Context.project_id == project_id,
            Context.name == ctx_name,
        ),
    ).scalar_one()
    bad = LogEvent(
        project_id=project_id,
        data={"relationship": "self"},
        created_at=datetime(2026, 4, 1, 1, 0, 0),
        updated_at=datetime(2026, 4, 1, 1, 0, 0),
    )
    dbsession.add(bad)
    dbsession.flush()
    dbsession.add(LogEventContext(log_event_id=bad.id, context_id=context.id))
    dbsession.flush()
    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="self",
        contact_id=555,
        created_at=datetime(2026, 4, 2, 0, 0, 0),
    )

    self_cid, boss_cid = resolve_membership_contact_ids(dbsession, assistant)
    assert self_cid == 555
    assert boss_cid is None


# ---------------------------------------------------------------------------
# AssistantRead projection: GET /v0/assistant/{id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_assistant_read_exposes_resolved_contact_ids(
    client: AsyncClient,
    org_owner,
    dbsession: Session,
):
    _, org = org_owner
    info = await _create_assistant(client, org["headers"])
    agent_id = int(info["agent_id"])
    assistant = dbsession.get(Assistant, agent_id)

    # No rows yet → both None in the create response.
    assert info["self_contact_id"] is None
    assert info["boss_contact_id"] is None

    project_id = _assistants_project_id(dbsession, assistant)
    ctx_name = f"{assistant.user_id}/{assistant.agent_id}/ContactMembership"
    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="self",
        contact_id=2024,
        created_at=datetime(2026, 4, 1, 0, 0, 0),
    )
    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="boss",
        contact_id=2025,
        created_at=datetime(2026, 4, 1, 0, 0, 0),
    )
    dbsession.commit()

    resp = await client.get(
        "/v0/assistant?list_all_org=True",
        headers=org["headers"],
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assistants = resp.json()["info"]
    row = next(r for r in assistants if int(r["agent_id"]) == agent_id)
    assert row["self_contact_id"] == 2024
    assert row["boss_contact_id"] == 2025


# ---------------------------------------------------------------------------
# Admin list: ``from_fields`` must opt out of the resolver when neither
# contact-id field is requested.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_admin_list_from_fields_skips_resolver_when_not_requested(
    client: AsyncClient,
    org_owner,
    dbsession: Session,
):
    _, org = org_owner
    info = await _create_assistant(client, org["headers"])
    agent_id = int(info["agent_id"])
    assistant = dbsession.get(Assistant, agent_id)

    project_id = _assistants_project_id(dbsession, assistant)
    ctx_name = f"{assistant.user_id}/{assistant.agent_id}/ContactMembership"
    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="self",
        contact_id=77,
        created_at=datetime(2026, 4, 1, 0, 0, 0),
    )
    dbsession.commit()

    with patch(
        "orchestra.web.api.assistant.views.resolve_membership_contact_ids",
        wraps=resolve_membership_contact_ids,
    ) as wrapped:
        resp = await client.get(
            "/v0/admin/assistant",
            params={"agent_id": agent_id, "from_fields": "agent_id,first_name"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        row = next(r for r in resp.json()["info"] if int(r["agent_id"]) == agent_id)
        assert "self_contact_id" not in row
        assert "boss_contact_id" not in row
        assert wrapped.call_count == 0


@pytest.mark.anyio
async def test_admin_list_from_fields_runs_resolver_when_requested(
    client: AsyncClient,
    org_owner,
    dbsession: Session,
):
    _, org = org_owner
    info = await _create_assistant(client, org["headers"])
    agent_id = int(info["agent_id"])
    assistant = dbsession.get(Assistant, agent_id)

    project_id = _assistants_project_id(dbsession, assistant)
    ctx_name = f"{assistant.user_id}/{assistant.agent_id}/ContactMembership"
    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="self",
        contact_id=88,
        created_at=datetime(2026, 4, 1, 0, 0, 0),
    )
    _write_membership_row(
        dbsession,
        project_id=project_id,
        context_name=ctx_name,
        relationship="boss",
        contact_id=89,
        created_at=datetime(2026, 4, 1, 0, 0, 0),
    )
    dbsession.commit()

    resp = await client.get(
        "/v0/admin/assistant",
        params={
            "agent_id": agent_id,
            "from_fields": "agent_id,self_contact_id,boss_contact_id",
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    row = next(r for r in resp.json()["info"] if int(r["agent_id"]) == agent_id)
    assert row["self_contact_id"] == 88
    assert row["boss_contact_id"] == 89
