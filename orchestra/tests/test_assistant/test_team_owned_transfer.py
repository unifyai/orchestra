"""Tests for converting org assistants to team-owned scope."""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.models.core_models import Context
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    CONTACT_MEMBERSHIP_SCOPE_TEAM,
    Assistant,
    ContactMembership,
)
from orchestra.tests.utils import create_test_user


async def _create_org_with_team(
    client: AsyncClient,
    owner,
    *,
    org_name: str,
    team_name: str = "Automation",
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


async def _hire_org_assistant(
    client: AsyncClient,
    org_headers: dict,
    *,
    first_name: str = "Brain",
) -> dict:
    response = await client.post(
        "/v0/assistant",
        json={
            "first_name": first_name,
            "surname": "Operator",
            "create_infra": False,
            "is_local": True,
        },
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    return response.json()["info"]


@pytest.mark.anyio
async def test_transfer_org_assistant_to_team_owned(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(client, "team_owned_transfer_owner@test.com")
    org_id, org_headers, team_id = await _create_org_with_team(
        client,
        owner,
        org_name="Team Owned Transfer Org",
    )
    info = await _hire_org_assistant(client, org_headers)
    agent_id = int(info["agent_id"])
    personal_prefix = f"{owner['id']}/{agent_id}"

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    body = response.json()["info"]
    assert body["owner_team_id"] == team_id
    assert body["memory_root"] == f"Teams/{team_id}"

    dbsession.expire_all()
    assistant = dbsession.get(Assistant, agent_id)
    assert assistant.owner_team_id == team_id

    personal_contexts = (
        dbsession.query(Context)
        .filter(Context.name.like(f"{personal_prefix}%"))
        .count()
    )
    assert personal_contexts == 0

    team_contexts = (
        dbsession.query(Context).filter(Context.name.like(f"Teams/{team_id}%")).count()
    )
    assert team_contexts >= 0

    personal_overlays = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
        )
        .count()
    )
    assert personal_overlays == 0

    team_overlays = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_TEAM,
            ContactMembership.target_team_id == team_id,
        )
        .count()
    )
    assert team_overlays >= 1
