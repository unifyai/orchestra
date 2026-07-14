"""API tests for presence heartbeats, the org roster, team group chat, and DMs."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import (
    ADMIN_HEADERS,
    create_test_org,
    create_test_user,
    ensure_assistants_project,
)


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


@pytest.fixture(autouse=True)
def org_chat_dispatch_mock(monkeypatch) -> AsyncMock:
    """Capture hosted org-chat dispatches instead of calling adapters."""

    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "orchestra.web.api.org_chat.views.dispatch_org_chat_best_effort",
        mock,
    )
    return mock


async def _create_org_with_member(client: AsyncClient, prefix: str):
    owner = await create_test_user(client, f"{prefix}-owner@test.com")
    member = await create_test_user(client, f"{prefix}-member@test.com")
    org = await create_test_org(client, owner, f"{prefix} Org")
    add_response = await client.post(
        f"/v0/organizations/{org['id']}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_response.status_code in (
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    ), add_response.json()
    return owner, member, org


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
        json={"name": name},
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    return response.json()


@pytest.mark.anyio
async def test_presence_heartbeat_reflected_in_roster(client: AsyncClient):
    owner, member, org = await _create_org_with_member(client, "presence")

    beat = await client.put("/v0/user/presence", headers=member["headers"])
    assert beat.status_code == status.HTTP_200_OK
    assert beat.json() == {"online": True}

    profile_update = await client.put(
        "/v0/admin/user",
        headers=ADMIN_HEADERS,
        json={
            "user_id": member["id"],
            "bio": "Roster profile bio",
            "job_title": "Engineer",
            "timezone": "America/New_York",
        },
    )
    assert profile_update.status_code == status.HTTP_200_OK, profile_update.json()

    roster_response = await client.get(
        f"/v0/organizations/{org['id']}/roster",
        headers=org["headers"],
    )
    assert roster_response.status_code == status.HTTP_200_OK, roster_response.json()
    roster = roster_response.json()
    humans_by_id = {human["user_id"]: human for human in roster["humans"]}

    assert member["id"] in humans_by_id
    assert humans_by_id[member["id"]]["online"] is True
    assert humans_by_id[member["id"]]["last_seen_at"] is not None
    assert humans_by_id[member["id"]]["email"] == f"presence-member@test.com"
    assert humans_by_id[member["id"]]["bio"] == "Roster profile bio"
    assert humans_by_id[member["id"]]["job_title"] == "Engineer"
    assert humans_by_id[member["id"]]["timezone"] == "America/New_York"

    assert owner["id"] in humans_by_id
    assert humans_by_id[owner["id"]]["online"] is False


@pytest.mark.anyio
async def test_roster_lists_teams_without_coordinators(client: AsyncClient):
    owner, member, org = await _create_org_with_member(client, "roster")
    team = await _create_team(
        client,
        org["headers"],
        organization_id=org["id"],
        name="Roster Team",
    )

    roster_response = await client.get(
        f"/v0/organizations/{org['id']}/roster",
        headers=org["headers"],
    )
    assert roster_response.status_code == status.HTTP_200_OK
    roster = roster_response.json()

    teams_by_id = {entry["team_id"]: entry for entry in roster["teams"]}
    assert team["id"] in teams_by_id
    roster_team = teams_by_id[team["id"]]
    assert owner["id"] in roster_team["member_user_ids"]
    # Team creation auto-adds the creator's workspace coordinator; the roster
    # must not surface coordinators as team assistant members.
    assert roster_team["assistant_member_ids"] == []


@pytest.mark.anyio
async def test_roster_requires_org_membership(client: AsyncClient):
    _owner, _member, org = await _create_org_with_member(client, "roster-auth")
    outsider = await create_test_user(client, "roster-auth-outsider@test.com")

    response = await client.get(
        f"/v0/organizations/{org['id']}/roster",
        headers=outsider["headers"],
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_team_messages_post_and_history(
    client: AsyncClient,
    org_chat_dispatch_mock: AsyncMock,
):
    owner, member, org = await _create_org_with_member(client, "teamchat")
    await ensure_assistants_project(client, org["headers"])
    team = await _create_team(
        client,
        org["headers"],
        organization_id=org["id"],
        name="Chat Team",
    )

    post_response = await client.post(
        f"/v0/organizations/{org['id']}/teams/{team['id']}/messages",
        headers=org["headers"],
        json={"content": "Hello team!"},
    )
    assert post_response.status_code == status.HTTP_201_CREATED, post_response.json()
    message = post_response.json()
    assert message["content"] == "Hello team!"
    assert message["sender_kind"] == "user"
    assert message["sender_user_id"] == owner["id"]
    assert isinstance(message["message_id"], int)
    assert message["team_id"] == team["id"]

    history_response = await client.get(
        f"/v0/organizations/{org['id']}/teams/{team['id']}/messages",
        headers=org["headers"],
    )
    assert history_response.status_code == status.HTTP_200_OK
    messages = history_response.json()["messages"]
    assert [entry["content"] for entry in messages] == ["Hello team!"]

    # A human message dispatches with team fan-out enabled (empty here: the
    # only team assistant is the coordinator, which is excluded). The
    # assistant event is a standard unify_message payload with team context.
    org_chat_dispatch_mock.assert_awaited()
    payload = org_chat_dispatch_mock.await_args.args[0]
    assert payload["kind"] == "team"
    assert payload["team_id"] == team["id"]
    assert payload["fanout_assistant_ids"] == []
    assert payload["assistant_event"]["body"] == "Hello team!"
    assert payload["assistant_event"]["sender_kind"] == "user"
    assert payload["assistant_event"]["sender_user_id"] == owner["id"]
    assert payload["assistant_event"]["sender_email"] == "teamchat-owner@test.com"

    # An org member who is not on the team can neither read nor post.
    non_member_read = await client.get(
        f"/v0/organizations/{org['id']}/teams/{team['id']}/messages",
        headers=member["headers"],
    )
    assert non_member_read.status_code == status.HTTP_403_FORBIDDEN
    non_member_post = await client.post(
        f"/v0/organizations/{org['id']}/teams/{team['id']}/messages",
        headers=member["headers"],
        json={"content": "Should fail"},
    )
    assert non_member_post.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_assistant_team_reply_fans_out_to_peers(
    client: AsyncClient,
    dbsession,
    org_chat_dispatch_mock: AsyncMock,
):
    owner, _member, org = await _create_org_with_member(client, "aichat")
    await ensure_assistants_project(client, org["headers"])
    team = await _create_team(
        client,
        org["headers"],
        organization_id=org["id"],
        name="AI Chat Team",
    )

    from orchestra.db.models.orchestra_models import Assistant, TeamAssistantMembership

    # Two non-coordinator assistants on the team: the author and one peer.
    author = Assistant(user_id=owner["id"], first_name="Ada", surname="Author")
    peer = Assistant(user_id=owner["id"], first_name="Pat", surname="Peer")
    dbsession.add_all([author, peer])
    dbsession.flush()
    dbsession.add_all(
        [
            TeamAssistantMembership(
                team_id=team["id"],
                assistant_id=author.agent_id,
                added_by=owner["id"],
            ),
            TeamAssistantMembership(
                team_id=team["id"],
                assistant_id=peer.agent_id,
                added_by=owner["id"],
            ),
        ],
    )
    dbsession.commit()

    reply_response = await client.post(
        f"/v0/admin/teams/{team['id']}/messages",
        headers=ADMIN_HEADERS,
        json={"assistant_id": author.agent_id, "content": "On it."},
    )
    assert reply_response.status_code == status.HTTP_201_CREATED, reply_response.json()
    reply = reply_response.json()
    assert reply["sender_kind"] == "assistant"
    assert reply["sender_assistant_id"] == author.agent_id

    # Assistant replies fan out to peer assistants (waking them if needed),
    # excluding the author, exactly like a human message.
    payload = org_chat_dispatch_mock.await_args.args[0]
    assert payload["kind"] == "team"
    assert payload["fanout_assistant_ids"] == [peer.agent_id]
    assert payload["assistant_event"]["sender_kind"] == "assistant"
    assert payload["assistant_event"]["sender_assistant_id"] == author.agent_id
    assert payload["assistant_event"]["sender_name"] == "Ada Author"
    assert payload["assistant_event"]["body"] == "On it."

    # A non-member assistant is rejected.
    bad_reply = await client.post(
        f"/v0/admin/teams/{team['id']}/messages",
        headers=ADMIN_HEADERS,
        json={"assistant_id": peer.agent_id + 999999, "content": "Nope"},
    )
    assert bad_reply.status_code in (
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    )


@pytest.mark.anyio
async def test_dm_send_and_history(
    client: AsyncClient,
    org_chat_dispatch_mock: AsyncMock,
):
    owner, member, org = await _create_org_with_member(client, "dm")

    send_response = await client.post(
        f"/v0/organizations/{org['id']}/dms/{member['id']}/messages",
        headers=org["headers"],
        json={"content": "Hi there"},
    )
    assert send_response.status_code == status.HTTP_201_CREATED, send_response.json()
    sent = send_response.json()
    assert sent["content"] == "Hi there"
    assert sent["sender_user_id"] == owner["id"]

    payload = org_chat_dispatch_mock.await_args.args[0]
    assert payload["kind"] == "dm"
    assert set(payload["message"]["user_ids"]) == {owner["id"], member["id"]}

    history_response = await client.get(
        f"/v0/organizations/{org['id']}/dms/{member['id']}/messages",
        headers=org["headers"],
    )
    assert history_response.status_code == status.HTTP_200_OK
    history = history_response.json()
    assert [entry["content"] for entry in history["messages"]] == ["Hi there"]
    assert set(history["user_ids"]) == {owner["id"], member["id"]}

    # Reverse direction resolves the same thread.
    member_history = await client.get(
        f"/v0/organizations/{org['id']}/dms/{owner['id']}/messages",
        headers=member["headers"],
    )
    assert member_history.status_code == status.HTTP_200_OK
    assert member_history.json()["thread_id"] == history["thread_id"]


@pytest.mark.anyio
async def test_dm_rejects_self_and_outsiders(client: AsyncClient):
    owner, _member, org = await _create_org_with_member(client, "dm-guard")
    outsider = await create_test_user(client, "dm-guard-outsider@test.com")

    self_response = await client.post(
        f"/v0/organizations/{org['id']}/dms/{owner['id']}/messages",
        headers=org["headers"],
        json={"content": "Talking to myself"},
    )
    assert self_response.status_code == status.HTTP_400_BAD_REQUEST

    outsider_response = await client.post(
        f"/v0/organizations/{org['id']}/dms/{outsider['id']}/messages",
        headers=org["headers"],
        json={"content": "You are not in the org"},
    )
    assert outsider_response.status_code == status.HTTP_403_FORBIDDEN
