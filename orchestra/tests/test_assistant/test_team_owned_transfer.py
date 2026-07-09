"""Tests for converting org assistants to team-owned scope."""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.context_dao import ContextDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.project_dao import ProjectDAO
from orchestra.db.models.core_models import Context
from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    CONTACT_MEMBERSHIP_SCOPE_TEAM,
    Assistant,
    ContactMembership,
)
from orchestra.tests.utils import create_test_user, ensure_assistants_project

_SHARED_TEAM_SHELLS = (
    "Contacts",
    "Guidance",
    "Knowledge",
    "Secrets",
    "Transcripts",
)


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


def _assistants_project_id(dbsession, org_id: int) -> int:
    context_dao = ContextDAO(dbsession)
    org_member_dao = OrganizationMemberDAO(dbsession)
    project_dao = ProjectDAO(dbsession, org_member_dao, context_dao)
    org_projects = project_dao.filter(
        organization_id=org_id,
        name="Assistants",
    )
    assert org_projects
    return org_projects[0][0].id


def _seed_empty_team_shells(
    dbsession,
    *,
    project_id: int,
    team_id: int,
) -> None:
    context_dao = ContextDAO(dbsession)
    for table_name in _SHARED_TEAM_SHELLS:
        context_dao.create(project_id, f"Teams/{team_id}/{table_name}")
    dbsession.commit()


async def _write_personal_logs(
    client: AsyncClient,
    org_headers: dict,
    *,
    personal_prefix: str,
    contexts: list[str],
) -> None:
    for context_suffix in contexts:
        context_name = f"{personal_prefix}/{context_suffix}"
        log_resp = await client.post(
            "/v0/logs",
            json={
                "project_name": "Assistants",
                "context": context_name,
                "entries": [{"message": f"log in {context_suffix}"}],
            },
            headers=org_headers,
        )
        assert log_resp.status_code == status.HTTP_200_OK, log_resp.json()


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


@pytest.mark.anyio
async def test_transfer_succeeds_when_team_has_empty_shared_shells(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(client, "team_owned_empty_shells@test.com")
    org_id, org_headers, team_id = await _create_org_with_team(
        client,
        owner,
        org_name="Team Owned Empty Shells Org",
    )
    await ensure_assistants_project(client, org_headers)
    info = await _hire_org_assistant(client, org_headers)
    agent_id = int(info["agent_id"])
    dbsession.expire_all()
    assistant = dbsession.get(Assistant, agent_id)
    personal_prefix = f"{assistant.user_id}/{agent_id}"

    project_id = _assistants_project_id(dbsession, org_id)
    context_dao = ContextDAO(dbsession)
    _seed_empty_team_shells(dbsession, project_id=project_id, team_id=team_id)
    await _write_personal_logs(
        client,
        org_headers,
        personal_prefix=personal_prefix,
        contexts=["Contacts", "Tasks"],
    )
    dbsession.expire_all()
    assert context_dao.subtree_has_logs(
        project_id,
        f"{personal_prefix}/Contacts",
    )
    assert context_dao.subtree_has_logs(project_id, f"{personal_prefix}/Tasks")

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_200_OK, response.json()

    dbsession.expire_all()
    personal_contexts = (
        dbsession.query(Context)
        .filter(Context.name.like(f"{personal_prefix}%"))
        .count()
    )
    assert personal_contexts == 0

    dbsession.expire_all()
    assert context_dao.subtree_has_logs(project_id, f"Teams/{team_id}/Contacts")
    assert context_dao.subtree_has_logs(project_id, f"Teams/{team_id}/Tasks")


@pytest.mark.anyio
async def test_transfer_rejects_both_sides_have_data(
    client: AsyncClient,
    dbsession,
):
    owner = await create_test_user(client, "team_owned_collision@test.com")
    org_id, org_headers, team_id = await _create_org_with_team(
        client,
        owner,
        org_name="Team Owned Collision Org",
    )
    await ensure_assistants_project(client, org_headers)
    info = await _hire_org_assistant(client, org_headers)
    agent_id = int(info["agent_id"])
    dbsession.expire_all()
    assistant = dbsession.get(Assistant, agent_id)
    personal_prefix = f"{assistant.user_id}/{agent_id}"

    project_id = _assistants_project_id(dbsession, org_id)
    _seed_empty_team_shells(dbsession, project_id=project_id, team_id=team_id)

    await _write_personal_logs(
        client,
        org_headers,
        personal_prefix=personal_prefix,
        contexts=["Contacts"],
    )
    team_contacts = f"Teams/{team_id}/Contacts"
    team_log_resp = await client.post(
        "/v0/logs",
        json={
            "project_name": "Assistants",
            "context": team_contacts,
            "entries": [{"message": "team contact log"}],
        },
        headers=org_headers,
    )
    assert team_log_resp.status_code == status.HTTP_200_OK, team_log_resp.json()

    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-team-owned",
        json={"owner_team_id": team_id},
        headers=org_headers,
    )
    assert response.status_code == status.HTTP_409_CONFLICT, response.json()
    assert response.json()["detail"].startswith("team_memory_collision_both_have_data")
