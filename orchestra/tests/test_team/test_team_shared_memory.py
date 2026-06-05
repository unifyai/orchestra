"""API tests for organization team shared-memory lifecycle."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_TEAM,
    Assistant,
    ContactMembership,
    TeamAssistantMembership,
)
from orchestra.tests.utils import create_test_org, create_test_user


@pytest.fixture(autouse=True)
def reawaken_assistant_mock(monkeypatch) -> AsyncMock:
    """Keep membership refresh publishes on the in-process test boundary."""

    mock = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(
        "orchestra.services.team_membership_refresh_service.reawaken_assistant",
        mock,
    )
    return mock


@pytest.fixture(autouse=True)
def coordinator_pubsub_mock(monkeypatch) -> AsyncMock:
    """Keep coordinator provisioning on the in-process test boundary."""

    mock = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(
        "orchestra.services.coordinator_service.create_pubsub_topic",
        mock,
    )
    return mock


async def _create_team(
    client: AsyncClient,
    headers: dict,
    *,
    organization_id: int,
    name: str,
) -> dict:
    response = await client.post(
        f"/v0/organizations/{organization_id}/teams",
        headers=headers,
        json={"name": name, "description": f"{name} shared team"},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    return response.json()


@pytest.mark.anyio
async def test_create_team_auto_adds_acting_user_workspace_coordinator(
    client: AsyncClient,
    dbsession: Session,
    reawaken_assistant_mock: AsyncMock,
) -> None:
    """Creating a team adds the acting user's workspace coordinator as a member."""

    owner = await create_test_user(client, "team-shared-create-owner@test.com")
    organization = await create_test_org(client, owner, "Team Shared Create Org")
    team = await _create_team(
        client,
        organization["headers"],
        organization_id=organization["id"],
        name="Central South P1",
    )

    coordinator = (
        dbsession.query(Assistant)
        .filter(
            Assistant.user_id == owner["id"],
            Assistant.organization_id == organization["id"],
            Assistant.is_coordinator.is_(True),
        )
        .one()
    )
    membership = (
        dbsession.query(TeamAssistantMembership)
        .filter(
            TeamAssistantMembership.team_id == team["id"],
            TeamAssistantMembership.assistant_id == coordinator.agent_id,
        )
        .one()
    )
    assert membership.added_by == owner["id"]

    overlays = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == coordinator.agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_TEAM,
            ContactMembership.target_team_id == team["id"],
        )
        .all()
    )
    assert {(row.contact_id, row.relationship) for row in overlays} == {
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
    }

    reawaken_assistant_mock.assert_awaited_once()
    payload = reawaken_assistant_mock.await_args.args[0]
    data = reawaken_assistant_mock.await_args.kwargs["data"]
    assert payload == str(coordinator.agent_id)
    assert json.loads(data["team_ids"]) == [team["id"]]


@pytest.mark.anyio
async def test_add_user_to_team_adds_workspace_coordinator(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Adding a human member also adds that member's workspace coordinator."""

    owner = await create_test_user(client, "team-user-member-owner@test.com")
    member = await create_test_user(client, "team-user-member@test.com")
    organization = await create_test_org(client, owner, "Team User Member Org")

    await client.post(
        f"/v0/organizations/{organization['id']}/members",
        json={"user_id": member["id"]},
        headers=organization["headers"],
    )

    team = await _create_team(
        client,
        organization["headers"],
        organization_id=organization["id"],
        name="Patch Team",
    )

    response = await client.post(
        f"/v0/organizations/{organization['id']}/teams/{team['id']}/members",
        headers=organization["headers"],
        json={"user_ids": [member["id"]]},
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    assert member["id"] in response.json()["members"]

    coordinator = (
        dbsession.query(Assistant)
        .filter(
            Assistant.user_id == member["id"],
            Assistant.organization_id == organization["id"],
            Assistant.is_coordinator.is_(True),
        )
        .one()
    )
    membership = (
        dbsession.query(TeamAssistantMembership)
        .filter(
            TeamAssistantMembership.team_id == team["id"],
            TeamAssistantMembership.assistant_id == coordinator.agent_id,
        )
        .one()
    )
    assert membership.added_by == owner["id"]


@pytest.mark.anyio
async def test_add_assistant_member_to_team(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Doer colleagues can join a team directly by assistant id."""

    owner = await create_test_user(client, "team-assistant-member-owner@test.com")
    organization = await create_test_org(client, owner, "Team Assistant Member Org")
    team = await _create_team(
        client,
        organization["headers"],
        organization_id=organization["id"],
        name="Doer Team",
    )

    colleague = Assistant(
        user_id=owner["id"],
        organization_id=organization["id"],
        first_name="Doer",
        surname="Colleague",
    )
    dbsession.add(colleague)
    dbsession.flush()

    response = await client.post(
        f"/v0/organizations/{organization['id']}/teams/{team['id']}/assistant-members",
        headers=organization["headers"],
        json={"assistant_id": colleague.agent_id},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    assert response.json()["assistant_id"] == colleague.agent_id
    assert response.json()["team_id"] == team["id"]

    listed = await client.get(
        f"/v0/organizations/{organization['id']}/teams/{team['id']}/assistant-members",
        headers=organization["headers"],
    )
    assert listed.status_code == status.HTTP_200_OK
    listed_ids = [row["assistant_id"] for row in listed.json()]
    assert colleague.agent_id in listed_ids


@pytest.mark.anyio
async def test_assistant_read_returns_assistant_team_memberships(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Assistant payloads expose shared-memory team memberships, not user RBAC teams."""

    owner = await create_test_user(client, "team-assistant-read-owner@test.com")
    organization = await create_test_org(client, owner, "Team Assistant Read Org")
    team = await _create_team(
        client,
        organization["headers"],
        organization_id=organization["id"],
        name="Read Team",
    )

    coordinator = (
        dbsession.query(Assistant)
        .filter(
            Assistant.user_id == owner["id"],
            Assistant.organization_id == organization["id"],
            Assistant.is_coordinator.is_(True),
        )
        .one()
    )

    response = await client.get(
        f"/v0/assistant?agent_id={coordinator.agent_id}",
        headers=organization["headers"],
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    body = response.json()["info"][0]
    assert body["team_ids"] == [team["id"]]
    assert body["team_summaries"] == [
        {
            "team_id": team["id"],
            "name": "Read Team",
            "description": "Read Team shared team",
        },
    ]
    assert body["space_ids"] == []
    assert body["space_summaries"] == []


@pytest.mark.anyio
async def test_spaces_route_is_not_mounted(client: AsyncClient) -> None:
    """Shared-memory space endpoints are retired in favor of team endpoints."""

    owner = await create_test_user(client, "team-no-spaces-owner@test.com")
    response = await client.post(
        "/v0/spaces",
        headers=owner["headers"],
        json={
            "name": "Retired",
            "description": "Retired shared workspace path",
            "organization_id": None,
        },
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
