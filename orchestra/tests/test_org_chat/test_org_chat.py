"""API tests for presence, roster, teams, chat groups, DMs, and org calls."""

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


@pytest.fixture(autouse=True)
def org_call_roster_refresh_mock(monkeypatch) -> AsyncMock:
    """Skip adapters fan-out when refreshing mid-call Meet rosters."""

    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "orchestra.web.api.org_chat.views._refresh_org_call_assistant_rosters",
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
    attachment = {
        "id": "att-1",
        "filename": "spec.pdf",
        "gs_url": "gs://bucket/spec.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1234,
    }

    send_response = await client.post(
        f"/v0/organizations/{org['id']}/dms/{member['id']}/messages",
        headers=org["headers"],
        json={"content": "Hi there", "attachments": [attachment]},
    )
    assert send_response.status_code == status.HTTP_201_CREATED, send_response.json()
    sent = send_response.json()
    assert sent["content"] == "Hi there"
    assert sent["sender_user_id"] == owner["id"]
    assert sent["attachments"][0]["id"] == attachment["id"]
    assert sent["attachments"][0]["filename"] == attachment["filename"]
    assert sent["attachments"][0]["gs_url"] == attachment["gs_url"]

    payload = org_chat_dispatch_mock.await_args.args[0]
    assert payload["kind"] == "dm"
    assert set(payload["message"]["user_ids"]) == {owner["id"], member["id"]}
    assert payload["message"]["attachments"][0]["filename"] == attachment["filename"]
    assert payload["message"]["attachments"][0]["gs_url"] == attachment["gs_url"]

    history_response = await client.get(
        f"/v0/organizations/{org['id']}/dms/{member['id']}/messages",
        headers=org["headers"],
    )
    assert history_response.status_code == status.HTTP_200_OK
    history = history_response.json()
    assert [entry["content"] for entry in history["messages"]] == ["Hi there"]
    assert (
        history["messages"][0]["attachments"][0]["filename"] == attachment["filename"]
    )
    assert history["messages"][0]["attachments"][0]["gs_url"] == attachment["gs_url"]
    assert set(history["user_ids"]) == {owner["id"], member["id"]}

    # Reverse direction resolves the same thread.
    member_history = await client.get(
        f"/v0/organizations/{org['id']}/dms/{owner['id']}/messages",
        headers=member["headers"],
    )
    assert member_history.status_code == status.HTTP_200_OK
    assert member_history.json()["thread_id"] == history["thread_id"]


@pytest.mark.anyio
async def test_dm_call_create(
    client: AsyncClient,
    dbsession,
    org_chat_dispatch_mock: AsyncMock,
):
    owner, member, org = await _create_org_with_member(client, "dm-call")

    create_response = await client.post(
        f"/v0/organizations/{org['id']}/dms/{member['id']}/calls",
        headers=org["headers"],
    )
    assert (
        create_response.status_code == status.HTTP_201_CREATED
    ), create_response.json()
    body = create_response.json()
    assert body["status"] == "ringing"
    assert body["caller_user_id"] == owner["id"]
    assert body["callee_user_id"] == member["id"]
    assert body["scope"] == "dm"
    assert set(body["user_ids"]) == {owner["id"], member["id"]}
    assert body["room_name"] == f"unity_org_{org['id']}_call_{body['call_id']}"

    from orchestra.db.models.orchestra_models import OrgCallSession

    call_session = dbsession.get(OrgCallSession, body["call_id"])
    assert call_session is not None
    assert call_session.livekit_room == body["room_name"]

    payload = org_chat_dispatch_mock.await_args.args[0]
    assert payload["kind"] == "org_call"
    assert payload["action"] == "incoming"
    assert payload["call"]["call_id"] == body["call_id"]
    assert payload["call"]["room_name"] == body["room_name"]
    assert payload["call"]["caller_user_id"] == owner["id"]
    assert payload["call"]["callee_user_id"] == member["id"]


@pytest.mark.anyio
async def test_team_call_create_rings_members(
    client: AsyncClient,
    dbsession,
    org_chat_dispatch_mock: AsyncMock,
):
    owner, member, org = await _create_org_with_member(client, "team-call")
    team = await _create_team(
        client,
        org["headers"],
        organization_id=org["id"],
        name="Call Team",
    )
    add_member = await client.post(
        f"/v0/organizations/{org['id']}/teams/{team['id']}/members",
        headers=org["headers"],
        json={"user_id": member["id"]},
    )
    assert add_member.status_code in (
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    ), add_member.json()

    create_response = await client.post(
        f"/v0/organizations/{org['id']}/teams/{team['id']}/calls",
        headers=org["headers"],
    )
    assert (
        create_response.status_code == status.HTTP_201_CREATED
    ), create_response.json()
    body = create_response.json()
    assert body["scope"] == "team"
    assert body["team_id"] == team["id"]
    assert body["status"] == "ringing"
    assert set(body["user_ids"]) == {owner["id"], member["id"]}
    assert body["room_name"] == f"unity_org_{org['id']}_call_{body['call_id']}"

    payload = org_chat_dispatch_mock.await_args.args[0]
    assert payload["kind"] == "org_call"
    assert payload["action"] == "incoming"
    assert set(payload["call"]["user_ids"]) == {owner["id"], member["id"]}


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


@pytest.mark.anyio
async def test_team_call_accepts_multiple_assistants(
    client: AsyncClient,
    dbsession,
    org_call_roster_refresh_mock: AsyncMock,
):
    owner, member, org = await _create_org_with_member(client, "n-assist")
    await ensure_assistants_project(client, org["headers"])
    team = await _create_team(
        client,
        org["headers"],
        organization_id=org["id"],
        name="N Assist Call Team",
    )
    add_member = await client.post(
        f"/v0/organizations/{org['id']}/teams/{team['id']}/members",
        headers=org["headers"],
        json={"user_id": member["id"]},
    )
    assert add_member.status_code in (
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    ), add_member.json()

    from orchestra.db.models.orchestra_models import Assistant, TeamAssistantMembership
    from orchestra.services.org_call_contacts import (
        ORG_CALL_PEER_ASSISTANT_ID_KEY,
        ORG_CALL_USER_ID_KEY,
    )

    a1 = Assistant(
        user_id=owner["id"],
        first_name="Ada",
        surname="One",
        organization_id=org["id"],
    )
    a2 = Assistant(
        user_id=owner["id"],
        first_name="Bea",
        surname="Two",
        organization_id=org["id"],
    )
    dbsession.add_all([a1, a2])
    dbsession.flush()
    dbsession.add_all(
        [
            TeamAssistantMembership(
                team_id=team["id"],
                assistant_id=a1.agent_id,
                added_by=owner["id"],
            ),
            TeamAssistantMembership(
                team_id=team["id"],
                assistant_id=a2.agent_id,
                added_by=owner["id"],
            ),
        ],
    )
    dbsession.commit()

    create_response = await client.post(
        f"/v0/organizations/{org['id']}/teams/{team['id']}/calls",
        headers=org["headers"],
    )
    assert (
        create_response.status_code == status.HTTP_201_CREATED
    ), create_response.json()
    call_id = create_response.json()["call_id"]

    first = await client.post(
        f"/v0/organizations/{org['id']}/calls/{call_id}/assistants",
        headers=org["headers"],
        json={"assistant_id": a1.agent_id},
    )
    assert first.status_code == status.HTTP_200_OK, first.json()
    first_body = first.json()
    assert first_body["assistant_ids"] == [a1.agent_id]
    assert any(m["kind"] == "human" for m in first_body["roster"])
    assert all(m.get("contact_id") is not None for m in first_body["roster"])

    second = await client.post(
        f"/v0/organizations/{org['id']}/calls/{call_id}/assistants",
        headers=org["headers"],
        json={"assistant_id": a2.agent_id},
    )
    assert second.status_code == status.HTTP_200_OK, second.json()
    second_body = second.json()
    assert set(second_body["assistant_ids"]) == {a1.agent_id, a2.agent_id}
    peer_rows = [m for m in second_body["roster"] if m["kind"] == "assistant"]
    assert len(peer_rows) == 1
    assert peer_rows[0]["assistant_id"] == a1.agent_id
    assert peer_rows[0]["contact_id"] is not None

    # Idempotent re-add of the same assistant.
    again = await client.post(
        f"/v0/organizations/{org['id']}/calls/{call_id}/assistants",
        headers=org["headers"],
        json={"assistant_id": a2.agent_id},
    )
    assert again.status_code == status.HTTP_200_OK, again.json()
    assert set(again.json()["assistant_ids"]) == {a1.agent_id, a2.agent_id}

    # Contacts rows exist for humans + peer assistant on a2's context.
    from sqlalchemy import select

    from orchestra.db.models.orchestra_models import LogEvent

    logs = dbsession.scalars(
        select(LogEvent).where(
            LogEvent.data.has_key(ORG_CALL_USER_ID_KEY)
            | LogEvent.data.has_key(ORG_CALL_PEER_ASSISTANT_ID_KEY),
        ),
    ).all()
    human_ids = {
        str(log.data.get(ORG_CALL_USER_ID_KEY))
        for log in logs
        if log.data.get(ORG_CALL_USER_ID_KEY)
    }
    peer_ids = {
        str(log.data.get(ORG_CALL_PEER_ASSISTANT_ID_KEY))
        for log in logs
        if log.data.get(ORG_CALL_PEER_ASSISTANT_ID_KEY)
    }
    assert owner["id"] in human_ids
    assert str(a1.agent_id) in peer_ids
    assert org_call_roster_refresh_mock.await_count >= 1


async def _create_group(
    client: AsyncClient,
    headers: dict,
    *,
    organization_id: int,
    name: str | None = None,
    user_ids: list[str] | None = None,
    assistant_ids: list[int] | None = None,
) -> dict:
    response = await client.post(
        f"/v0/organizations/{organization_id}/groups",
        headers=headers,
        json={
            "name": name,
            "user_ids": user_ids or [],
            "assistant_ids": assistant_ids or [],
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.json()
    return response.json()


@pytest.mark.anyio
async def test_create_and_list_group_with_humans_and_assistants(
    client: AsyncClient,
    dbsession,
):
    owner, member, org = await _create_org_with_member(client, "grp-create")
    await ensure_assistants_project(client, org["headers"])

    from orchestra.db.models.orchestra_models import Assistant

    assistant = Assistant(
        user_id=owner["id"],
        first_name="Gigi",
        surname="Group",
        organization_id=org["id"],
    )
    dbsession.add(assistant)
    dbsession.commit()

    created = await _create_group(
        client,
        org["headers"],
        organization_id=org["id"],
        name="Project Sync",
        user_ids=[member["id"]],
        assistant_ids=[assistant.agent_id],
    )
    assert created["name"] == "Project Sync"
    assert created["organization_id"] == org["id"]
    assert created["created_by_user_id"] == owner["id"]
    assert set(created["member_user_ids"]) == {owner["id"], member["id"]}
    assert created["assistant_member_ids"] == [assistant.agent_id]

    owner_list = await client.get(
        f"/v0/organizations/{org['id']}/groups",
        headers=org["headers"],
    )
    assert owner_list.status_code == status.HTTP_200_OK, owner_list.json()
    owner_groups = {g["group_id"]: g for g in owner_list.json()["groups"]}
    assert created["group_id"] in owner_groups
    assert owner_groups[created["group_id"]]["assistant_member_ids"] == [
        assistant.agent_id,
    ]

    member_list = await client.get(
        f"/v0/organizations/{org['id']}/groups",
        headers=member["headers"],
    )
    assert member_list.status_code == status.HTTP_200_OK
    assert created["group_id"] in {g["group_id"] for g in member_list.json()["groups"]}

    # An org member who is not in the group does not see it.
    outsider = await create_test_user(client, "grp-create-outsider@test.com")
    add_outsider = await client.post(
        f"/v0/organizations/{org['id']}/members",
        json={"user_id": outsider["id"]},
        headers=owner["headers"],
    )
    assert add_outsider.status_code in (
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    ), add_outsider.json()
    outsider_list = await client.get(
        f"/v0/organizations/{org['id']}/groups",
        headers=outsider["headers"],
    )
    assert outsider_list.status_code == status.HTTP_200_OK
    assert created["group_id"] not in {
        g["group_id"] for g in outsider_list.json()["groups"]
    }


@pytest.mark.anyio
async def test_group_membership_patch_is_idempotent(
    client: AsyncClient,
    dbsession,
):
    owner, member, org = await _create_org_with_member(client, "grp-patch")

    from orchestra.db.models.orchestra_models import Assistant

    a1 = Assistant(
        user_id=owner["id"],
        first_name="Pat",
        surname="One",
        organization_id=org["id"],
    )
    a2 = Assistant(
        user_id=owner["id"],
        first_name="Pat",
        surname="Two",
        organization_id=org["id"],
    )
    dbsession.add_all([a1, a2])
    dbsession.commit()

    group = await _create_group(
        client,
        org["headers"],
        organization_id=org["id"],
        name="Patch Group",
        user_ids=[member["id"]],
        assistant_ids=[a1.agent_id],
    )

    payload = {
        "user_ids": [owner["id"], member["id"]],
        "assistant_ids": [a1.agent_id, a2.agent_id],
    }
    first = await client.patch(
        f"/v0/organizations/{org['id']}/groups/{group['group_id']}",
        headers=org["headers"],
        json=payload,
    )
    assert first.status_code == status.HTTP_200_OK, first.json()
    first_body = first.json()
    assert set(first_body["member_user_ids"]) == {owner["id"], member["id"]}
    assert set(first_body["assistant_member_ids"]) == {a1.agent_id, a2.agent_id}

    # Re-applying the same membership is a no-op for callers.
    second = await client.patch(
        f"/v0/organizations/{org['id']}/groups/{group['group_id']}",
        headers=org["headers"],
        json=payload,
    )
    assert second.status_code == status.HTTP_200_OK, second.json()
    second_body = second.json()
    assert set(second_body["member_user_ids"]) == set(first_body["member_user_ids"])
    assert set(second_body["assistant_member_ids"]) == set(
        first_body["assistant_member_ids"],
    )
    assert second_body["name"] == first_body["name"]


@pytest.mark.anyio
async def test_group_messages_post_and_dispatch(
    client: AsyncClient,
    dbsession,
    org_chat_dispatch_mock: AsyncMock,
):
    owner, member, org = await _create_org_with_member(client, "grp-msg")
    await ensure_assistants_project(client, org["headers"])

    from orchestra.db.models.orchestra_models import Assistant

    assistant = Assistant(
        user_id=owner["id"],
        first_name="Msg",
        surname="Bot",
        organization_id=org["id"],
    )
    dbsession.add(assistant)
    dbsession.commit()

    group = await _create_group(
        client,
        org["headers"],
        organization_id=org["id"],
        name="Message Group",
        user_ids=[member["id"]],
        assistant_ids=[assistant.agent_id],
    )

    post_response = await client.post(
        f"/v0/organizations/{org['id']}/groups/{group['group_id']}/messages",
        headers=org["headers"],
        json={"content": "Hello group!"},
    )
    assert post_response.status_code == status.HTTP_201_CREATED, post_response.json()
    message = post_response.json()
    assert message["content"] == "Hello group!"
    assert message["sender_kind"] == "user"
    assert message["sender_user_id"] == owner["id"]
    assert isinstance(message["message_id"], int)
    assert message["group_id"] == group["group_id"]

    history_response = await client.get(
        f"/v0/organizations/{org['id']}/groups/{group['group_id']}/messages",
        headers=org["headers"],
    )
    assert history_response.status_code == status.HTTP_200_OK
    messages = history_response.json()["messages"]
    assert [entry["content"] for entry in messages] == ["Hello group!"]

    org_chat_dispatch_mock.assert_awaited()
    payload = org_chat_dispatch_mock.await_args.args[0]
    assert payload["kind"] == "group"
    assert payload["group_id"] == group["group_id"]
    assert payload["fanout_assistant_ids"] == [assistant.agent_id]
    assert payload["assistant_event"]["body"] == "Hello group!"
    assert payload["assistant_event"]["sender_kind"] == "user"
    assert payload["assistant_event"]["sender_user_id"] == owner["id"]
    assert payload["assistant_event"]["sender_email"] == "grp-msg-owner@test.com"

    # Non-member org human cannot read or post.
    outsider = await create_test_user(client, "grp-msg-outsider@test.com")
    add_outsider = await client.post(
        f"/v0/organizations/{org['id']}/members",
        json={"user_id": outsider["id"]},
        headers=owner["headers"],
    )
    assert add_outsider.status_code in (
        status.HTTP_200_OK,
        status.HTTP_201_CREATED,
    ), add_outsider.json()
    non_member_read = await client.get(
        f"/v0/organizations/{org['id']}/groups/{group['group_id']}/messages",
        headers=outsider["headers"],
    )
    assert non_member_read.status_code == status.HTTP_403_FORBIDDEN
    non_member_post = await client.post(
        f"/v0/organizations/{org['id']}/groups/{group['group_id']}/messages",
        headers=outsider["headers"],
        json={"content": "Should fail"},
    )
    assert non_member_post.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_group_call_create_and_add_assistant(
    client: AsyncClient,
    dbsession,
    org_chat_dispatch_mock: AsyncMock,
    org_call_roster_refresh_mock: AsyncMock,
):
    owner, member, org = await _create_org_with_member(client, "grp-call")
    await ensure_assistants_project(client, org["headers"])

    from orchestra.db.models.orchestra_models import Assistant

    a1 = Assistant(
        user_id=owner["id"],
        first_name="Call",
        surname="One",
        organization_id=org["id"],
    )
    a2 = Assistant(
        user_id=owner["id"],
        first_name="Call",
        surname="Two",
        organization_id=org["id"],
    )
    dbsession.add_all([a1, a2])
    dbsession.commit()

    group = await _create_group(
        client,
        org["headers"],
        organization_id=org["id"],
        name="Call Group",
        user_ids=[member["id"]],
        assistant_ids=[a1.agent_id, a2.agent_id],
    )

    create_response = await client.post(
        f"/v0/organizations/{org['id']}/groups/{group['group_id']}/calls",
        headers=org["headers"],
    )
    assert (
        create_response.status_code == status.HTTP_201_CREATED
    ), create_response.json()
    body = create_response.json()
    assert body["scope"] == "group"
    assert body["group_id"] == group["group_id"]
    assert body["status"] == "ringing"
    assert set(body["user_ids"]) == {owner["id"], member["id"]}
    assert body["room_name"] == f"unity_org_{org['id']}_call_{body['call_id']}"

    payload = org_chat_dispatch_mock.await_args.args[0]
    assert payload["kind"] == "org_call"
    assert payload["action"] == "incoming"
    assert payload["call"]["scope"] == "group"
    assert payload["call"]["group_id"] == group["group_id"]
    assert set(payload["call"]["user_ids"]) == {owner["id"], member["id"]}

    first = await client.post(
        f"/v0/organizations/{org['id']}/calls/{body['call_id']}/assistants",
        headers=org["headers"],
        json={"assistant_id": a1.agent_id},
    )
    assert first.status_code == status.HTTP_200_OK, first.json()
    assert first.json()["assistant_ids"] == [a1.agent_id]
    assert any(m["kind"] == "human" for m in first.json()["roster"])

    second = await client.post(
        f"/v0/organizations/{org['id']}/calls/{body['call_id']}/assistants",
        headers=org["headers"],
        json={"assistant_id": a2.agent_id},
    )
    assert second.status_code == status.HTTP_200_OK, second.json()
    assert set(second.json()["assistant_ids"]) == {a1.agent_id, a2.agent_id}
    # Roster refresh fans out to peers already on the call.
    assert org_call_roster_refresh_mock.await_count >= 1

    # Non-member assistant is rejected.
    other = Assistant(
        user_id=owner["id"],
        first_name="Not",
        surname="Member",
        organization_id=org["id"],
    )
    dbsession.add(other)
    dbsession.commit()
    rejected = await client.post(
        f"/v0/organizations/{org['id']}/calls/{body['call_id']}/assistants",
        headers=org["headers"],
        json={"assistant_id": other.agent_id},
    )
    assert rejected.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_delete_group(client: AsyncClient):
    owner, member, org = await _create_org_with_member(client, "grp-del")
    group = await _create_group(
        client,
        org["headers"],
        organization_id=org["id"],
        name="Doomed Group",
        user_ids=[member["id"]],
    )

    delete_response = await client.delete(
        f"/v0/organizations/{org['id']}/groups/{group['group_id']}",
        headers=org["headers"],
    )
    assert delete_response.status_code == status.HTTP_204_NO_CONTENT

    get_response = await client.get(
        f"/v0/organizations/{org['id']}/groups/{group['group_id']}",
        headers=org["headers"],
    )
    assert get_response.status_code == status.HTTP_404_NOT_FOUND

    list_response = await client.get(
        f"/v0/organizations/{org['id']}/groups",
        headers=org["headers"],
    )
    assert list_response.status_code == status.HTTP_200_OK
    assert group["group_id"] not in {
        g["group_id"] for g in list_response.json()["groups"]
    }


@pytest.mark.anyio
async def test_roster_includes_groups(client: AsyncClient, dbsession):
    owner, member, org = await _create_org_with_member(client, "grp-roster")

    from orchestra.db.models.orchestra_models import Assistant

    assistant = Assistant(
        user_id=owner["id"],
        first_name="Roster",
        surname="Aide",
        organization_id=org["id"],
    )
    dbsession.add(assistant)
    dbsession.commit()

    group = await _create_group(
        client,
        org["headers"],
        organization_id=org["id"],
        name="Roster Group",
        user_ids=[member["id"]],
        assistant_ids=[assistant.agent_id],
    )

    roster_response = await client.get(
        f"/v0/organizations/{org['id']}/roster",
        headers=org["headers"],
    )
    assert roster_response.status_code == status.HTTP_200_OK, roster_response.json()
    roster = roster_response.json()
    assert "groups" in roster
    groups_by_id = {entry["group_id"]: entry for entry in roster["groups"]}
    assert group["group_id"] in groups_by_id
    roster_group = groups_by_id[group["group_id"]]
    assert roster_group["name"] == "Roster Group"
    assert roster_group["created_by_user_id"] == owner["id"]
    assert set(roster_group["member_user_ids"]) == {owner["id"], member["id"]}
    assert roster_group["assistant_member_ids"] == [assistant.agent_id]
