"""API tests for shared space lifecycle operations."""

from __future__ import annotations

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantSpaceMembership,
    SpaceInvite,
)
from orchestra.tests.utils import ADMIN_HEADERS, create_test_org, create_test_user


def _make_assistant(
    dbsession: Session,
    *,
    owner_id: str,
    organization_id: int | None = None,
    first_name: str = "Space",
) -> Assistant:
    assistant = Assistant(
        user_id=owner_id,
        organization_id=organization_id,
        first_name=first_name,
        surname="Bot",
    )
    dbsession.add(assistant)
    dbsession.flush()
    return assistant


async def _create_space(
    client: AsyncClient,
    headers: dict,
    *,
    name: str,
    organization_id: int | None = None,
) -> dict:
    response = await client.post(
        "/v0/spaces",
        headers=headers,
        json={
            "name": name,
            "description": f"{name} shared workspace",
            "organization_id": organization_id,
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    return response.json()


@pytest.mark.anyio
async def test_space_crud_returns_space_shape_without_membership_status(
    client: AsyncClient,
) -> None:
    """Owners can manage empty personal spaces through the real API surface."""

    owner = await create_test_user(client, "space-crud-owner@test.com")

    created = await _create_space(
        client,
        owner["headers"],
        name="Personal Operations",
    )

    assert created["space_id"]
    assert created["owner_user_id"] == owner["id"]
    assert created["organization_id"] is None
    assert "membership_status" not in created

    listed = await client.get("/v0/spaces", headers=owner["headers"])
    assert listed.status_code == status.HTTP_200_OK, listed.json()
    assert [space["space_id"] for space in listed.json()] == [created["space_id"]]

    fetched = await client.get(
        f"/v0/spaces/{created['space_id']}",
        headers=owner["headers"],
    )
    assert fetched.status_code == status.HTTP_200_OK, fetched.json()
    assert fetched.json()["name"] == "Personal Operations"

    patched = await client.patch(
        f"/v0/spaces/{created['space_id']}",
        headers=owner["headers"],
        json={"name": "Personal Dispatch", "description": "Updated"},
    )
    assert patched.status_code == status.HTTP_200_OK, patched.json()
    assert patched.json()["name"] == "Personal Dispatch"
    assert patched.json()["description"] == "Updated"

    deleted = await client.delete(
        f"/v0/spaces/{created['space_id']}",
        headers=owner["headers"],
    )
    assert deleted.status_code == status.HTTP_204_NO_CONTENT, deleted.text


@pytest.mark.anyio
async def test_same_owner_member_add_projects_sorted_space_ids(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Live memberships immediately appear as sorted assistant space ids."""

    owner = await create_test_user(client, "space-member-owner@test.com")
    assistant = _make_assistant(dbsession, owner_id=owner["id"])
    first_space = await _create_space(client, owner["headers"], name="Alpha")
    second_space = await _create_space(client, owner["headers"], name="Beta")

    add_second = await client.post(
        f"/v0/spaces/{second_space['space_id']}/members",
        headers=owner["headers"],
        json={"assistant_id": assistant.agent_id},
    )
    assert add_second.status_code == status.HTTP_201_CREATED, add_second.json()
    assert add_second.json()["membership_status"] == "active"

    add_first = await client.post(
        f"/v0/spaces/{first_space['space_id']}/members",
        headers=owner["headers"],
        json={"assistant_id": assistant.agent_id},
    )
    assert add_first.status_code == status.HTTP_201_CREATED, add_first.json()
    assert add_first.json()["membership_status"] == "active"

    memberships = (
        dbsession.query(AssistantSpaceMembership)
        .filter(AssistantSpaceMembership.assistant_id == assistant.agent_id)
        .all()
    )
    assert {membership.space_id for membership in memberships} == {
        first_space["space_id"],
        second_space["space_id"],
    }

    public_read = await client.get(
        f"/v0/assistant?agent_id={assistant.agent_id}",
        headers=owner["headers"],
    )
    assert public_read.status_code == status.HTTP_200_OK, public_read.json()
    assert public_read.json()["info"][0]["space_ids"] == [
        first_space["space_id"],
        second_space["space_id"],
    ]

    admin_read = await client.get(
        f"/v0/admin/assistant?agent_id={assistant.agent_id}&from_fields=agent_id,space_ids",
        headers=ADMIN_HEADERS,
    )
    assert admin_read.status_code == status.HTTP_200_OK, admin_read.json()
    assert admin_read.json()["info"] == [
        {
            "agent_id": str(assistant.agent_id),
            "space_ids": [first_space["space_id"], second_space["space_id"]],
        },
    ]

    summaries = await client.get(
        f"/v0/assistants/{assistant.agent_id}/spaces",
        headers=owner["headers"],
    )
    assert summaries.status_code == status.HTTP_200_OK, summaries.json()
    assert [space["space_id"] for space in summaries.json()] == [
        first_space["space_id"],
        second_space["space_id"],
    ]

    blocked_delete = await client.delete(
        f"/v0/spaces/{first_space['space_id']}",
        headers=owner["headers"],
    )
    assert blocked_delete.status_code == status.HTTP_409_CONFLICT
    assert blocked_delete.json()["detail"] == "space_not_empty"


@pytest.mark.anyio
async def test_org_admin_adds_org_assistant_directly(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Org admins can directly provision org-owned assistants into org spaces."""

    owner = await create_test_user(client, "space-org-owner@test.com")
    member = await create_test_user(client, "space-org-member@test.com")
    organization = await create_test_org(client, owner, "Space Direct Add Org")
    add_member = await client.post(
        f"/v0/organizations/{organization['id']}/members",
        headers=owner["headers"],
        json={"user_id": member["id"]},
    )
    assert add_member.status_code == status.HTTP_201_CREATED, add_member.json()
    assistant = _make_assistant(
        dbsession,
        owner_id=member["id"],
        organization_id=organization["id"],
        first_name="Org",
    )
    space = await _create_space(
        client,
        owner["headers"],
        name="Org Operations",
        organization_id=organization["id"],
    )

    response = await client.post(
        f"/v0/spaces/{space['space_id']}/members",
        headers=owner["headers"],
        json={"assistant_id": assistant.agent_id},
    )

    assert response.status_code == status.HTTP_201_CREATED, response.json()
    assert response.json()["membership_status"] == "active"
    assert response.json()["invite_id"] is None
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(
            AssistantSpaceMembership.assistant_id == assistant.agent_id,
            AssistantSpaceMembership.space_id == space["space_id"],
        )
        .one()
    )


@pytest.mark.anyio
async def test_cross_owner_member_add_creates_pending_invitation_until_accept(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Cross-owner personal adds require the invited owner to accept."""

    inviter = await create_test_user(client, "space-inviter@test.com")
    invited_owner = await create_test_user(client, "space-invited-owner@test.com")
    assistant = _make_assistant(dbsession, owner_id=invited_owner["id"])
    space = await _create_space(client, inviter["headers"], name="Shared Home")

    add_response = await client.post(
        f"/v0/spaces/{space['space_id']}/members",
        headers=inviter["headers"],
        json={"assistant_id": assistant.agent_id},
    )
    assert add_response.status_code == status.HTTP_201_CREATED, add_response.json()
    body = add_response.json()
    assert body["membership_status"] == "pending_invitation"
    assert body["invite_id"]
    assert body["expires_at"]

    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(
            AssistantSpaceMembership.assistant_id == assistant.agent_id,
            AssistantSpaceMembership.space_id == space["space_id"],
        )
        .one_or_none()
        is None
    )

    pending = await client.get(
        "/v0/space-invites/pending",
        headers=invited_owner["headers"],
    )
    assert pending.status_code == status.HTTP_200_OK, pending.json()
    assert [invite["invite_id"] for invite in pending.json()] == [body["invite_id"]]

    accepted = await client.post(
        f"/v0/space-invites/{body['invite_id']}/accept",
        headers=invited_owner["headers"],
    )
    assert accepted.status_code == status.HTTP_200_OK, accepted.json()
    assert accepted.json() == {"status": "accepted"}

    invite = dbsession.get(SpaceInvite, body["invite_id"])
    assert invite.status == "accepted"
    assert invite.decided_at is not None
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(
            AssistantSpaceMembership.assistant_id == assistant.agent_id,
            AssistantSpaceMembership.space_id == space["space_id"],
        )
        .one()
    )


@pytest.mark.anyio
async def test_invite_reissue_cancel_and_already_member_contract(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Pending invites are idempotent and terminal rows persist for audit."""

    inviter = await create_test_user(client, "space-reissue-inviter@test.com")
    invited_owner = await create_test_user(client, "space-reissue-owner@test.com")
    invited_assistant = _make_assistant(dbsession, owner_id=invited_owner["id"])
    member_assistant = _make_assistant(dbsession, owner_id=inviter["id"])
    invite_space = await _create_space(client, inviter["headers"], name="Invite Space")
    member_space = await _create_space(client, inviter["headers"], name="Member Space")

    first_invite = await client.post(
        f"/v0/spaces/{invite_space['space_id']}/invites",
        headers=inviter["headers"],
        json={"assistant_id": invited_assistant.agent_id},
    )
    assert first_invite.status_code == status.HTTP_201_CREATED, first_invite.json()
    first_body = first_invite.json()

    blocked_delete = await client.delete(
        f"/v0/spaces/{invite_space['space_id']}",
        headers=inviter["headers"],
    )
    assert blocked_delete.status_code == status.HTTP_409_CONFLICT

    reissued = await client.post(
        f"/v0/spaces/{invite_space['space_id']}/invites",
        headers=inviter["headers"],
        json={"assistant_id": invited_assistant.agent_id},
    )
    assert reissued.status_code == status.HTTP_200_OK, reissued.json()
    assert reissued.json()["invite_id"] == first_body["invite_id"]
    assert reissued.json()["expires_at"] >= first_body["expires_at"]

    cancelled = await client.delete(
        f"/v0/space-invites/{first_body['invite_id']}",
        headers=inviter["headers"],
    )
    assert cancelled.status_code == status.HTTP_204_NO_CONTENT, cancelled.text

    invite = dbsession.get(SpaceInvite, first_body["invite_id"])
    assert invite.status == "cancelled"
    assert invite.decided_at is not None

    add_member = await client.post(
        f"/v0/spaces/{member_space['space_id']}/members",
        headers=inviter["headers"],
        json={"assistant_id": member_assistant.agent_id},
    )
    assert add_member.status_code == status.HTTP_201_CREATED, add_member.json()

    already_member = await client.post(
        f"/v0/spaces/{member_space['space_id']}/invites",
        headers=inviter["headers"],
        json={"assistant_id": member_assistant.agent_id},
    )
    assert already_member.status_code == status.HTTP_409_CONFLICT
    assert already_member.json()["detail"] == "already_member"


@pytest.mark.anyio
async def test_unrelated_org_admin_cannot_access_another_org_space(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Organization permissions do not cross space ownership boundaries."""

    org_a_owner = await create_test_user(client, "space-org-a-owner@test.com")
    org_b_owner = await create_test_user(client, "space-org-b-owner@test.com")
    org_a = await create_test_org(client, org_a_owner, "Space Org A")
    org_b = await create_test_org(client, org_b_owner, "Space Org B")
    org_b_assistant = _make_assistant(
        dbsession,
        owner_id=org_b_owner["id"],
        organization_id=org_b["id"],
    )
    org_a_space = await _create_space(
        client,
        org_a_owner["headers"],
        name="Org A Space",
        organization_id=org_a["id"],
    )

    get_response = await client.get(
        f"/v0/spaces/{org_a_space['space_id']}",
        headers=org_b_owner["headers"],
    )
    patch_response = await client.patch(
        f"/v0/spaces/{org_a_space['space_id']}",
        headers=org_b_owner["headers"],
        json={"name": "Cross Org"},
    )
    add_member_response = await client.post(
        f"/v0/spaces/{org_a_space['space_id']}/members",
        headers=org_b_owner["headers"],
        json={"assistant_id": org_b_assistant.agent_id},
    )
    list_members_response = await client.get(
        f"/v0/spaces/{org_a_space['space_id']}/members",
        headers=org_b_owner["headers"],
    )
    list_invites_response = await client.get(
        f"/v0/spaces/{org_a_space['space_id']}/invites",
        headers=org_b_owner["headers"],
    )

    assert get_response.status_code == status.HTTP_403_FORBIDDEN
    assert patch_response.status_code == status.HTTP_403_FORBIDDEN
    assert add_member_response.status_code == status.HTTP_403_FORBIDDEN
    assert list_members_response.status_code == status.HTTP_403_FORBIDDEN
    assert list_invites_response.status_code == status.HTTP_403_FORBIDDEN
