"""Team-owned assistants: creation, enrollment, and lifecycle guards."""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.models.orchestra_models import (
    Assistant,
    Context,
    Team,
    TeamAssistantMembership,
)
from orchestra.tests.utils import create_test_user


async def _create_org_with_team(
    client: AsyncClient,
    owner,
    *,
    org_name: str,
    team_name: str = "Support",
) -> tuple[int, dict, int]:
    org_response = await client.post(
        "/v0/organizations",
        json={"name": org_name, "data_sharing_mode": "private"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED, org_response.json()
    org_id = org_response.json()["id"]
    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_response.json()['api_key']}",
    }
    team_response = await client.post(
        f"/v0/organizations/{org_id}/teams",
        json={"name": team_name},
        headers=owner["headers"],
    )
    assert team_response.status_code == status.HTTP_201_CREATED, team_response.json()
    return org_id, org_headers, team_response.json()["id"]


async def _hire_team_owned(
    client: AsyncClient,
    org_headers: dict,
    team_id: int,
    *,
    first_name: str = "Teamly",
) -> dict:
    response = await client.post(
        "/v0/assistant",
        json={
            "first_name": first_name,
            "surname": "Unity",
            "create_infra": False,
            "is_local": True,
            "owner_team_id": team_id,
        },
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    return response.json()["info"]


@pytest.mark.anyio
async def test_team_owned_assistant_creation_and_enrollment(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(client, "team_owned_owner@test.com")
    org_id, org_headers, team_id = await _create_org_with_team(
        client,
        owner,
        org_name="Team Owned Org",
    )

    info = await _hire_team_owned(client, org_headers, team_id)
    assert info["owner_team_id"] == team_id
    agent_id = int(info["agent_id"])

    dbsession.expire_all()
    assistant = dbsession.get(Assistant, agent_id)
    assert assistant.owner_team_id == team_id
    # Creator is recorded as the hiring member, not a supervisor.
    assert assistant.user_id == owner["id"]

    # Enrolled in the owning team automatically.
    membership = (
        dbsession.query(TeamAssistantMembership)
        .filter_by(team_id=team_id, assistant_id=agent_id)
        .one_or_none()
    )
    assert membership is not None

    # No personal `{user}/{agent}` contexts were provisioned.
    personal_prefix = f"{owner['id']}/{agent_id}/"
    personal_contexts = (
        dbsession.query(Context)
        .filter(Context.name.like(f"{personal_prefix}%"))
        .count()
    )
    assert personal_contexts == 0


@pytest.mark.anyio
async def test_team_owned_requires_org_key_and_org_team(client: AsyncClient):
    owner = await create_test_user(client, "team_owned_guard@test.com")
    org_id, org_headers, team_id = await _create_org_with_team(
        client,
        owner,
        org_name="Team Owned Guard Org",
    )

    # Personal key cannot create team-owned assistants.
    personal_response = await client.post(
        "/v0/assistant",
        json={
            "first_name": "NoOrg",
            "surname": "Unity",
            "create_infra": False,
            "is_local": True,
            "owner_team_id": team_id,
        },
        headers=owner["headers"],
    )
    assert personal_response.status_code == status.HTTP_400_BAD_REQUEST

    # Unknown/foreign team is rejected.
    missing_response = await client.post(
        "/v0/assistant",
        json={
            "first_name": "GhostTeam",
            "surname": "Unity",
            "create_infra": False,
            "is_local": True,
            "owner_team_id": 999999,
        },
        headers=org_headers,
    )
    assert missing_response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_owning_team_cannot_be_deleted_or_left(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(client, "team_owned_delete@test.com")
    org_id, org_headers, team_id = await _create_org_with_team(
        client,
        owner,
        org_name="Team Owned Delete Org",
    )
    info = await _hire_team_owned(client, org_headers, team_id)
    agent_id = int(info["agent_id"])

    # Removing the assistant from its owning team is structural — refused.
    remove_response = await client.delete(
        f"/v0/organizations/{org_id}/teams/{team_id}/assistant-members/{agent_id}",
        headers=owner["headers"],
    )
    assert remove_response.status_code == status.HTTP_409_CONFLICT
    assert remove_response.json()["detail"] == "assistant_owned_by_team"

    # Deleting the owning team is refused while it owns assistants.
    delete_response = await client.delete(
        f"/v0/organizations/{org_id}/teams/{team_id}",
        headers=owner["headers"],
    )
    assert delete_response.status_code == status.HTTP_409_CONFLICT
    assert delete_response.json()["detail"].startswith("team_owns_assistants")

    dbsession.expire_all()
    team = dbsession.get(Team, team_id)
    assert team is not None
