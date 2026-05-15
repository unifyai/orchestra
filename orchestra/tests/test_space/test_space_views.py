"""API tests for shared space lifecycle operations."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
    CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
    CONTACT_MEMBERSHIP_SCOPE_SPACE,
    Assistant,
    AssistantSpaceMembership,
    ContactMembership,
    Space,
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
    dbsession.add_all(
        [
            ContactMembership(
                assistant_id=assistant.agent_id,
                contact_id=0,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                can_edit=True,
            ),
            ContactMembership(
                assistant_id=assistant.agent_id,
                contact_id=1,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship=CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                can_edit=True,
            ),
        ],
    )
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


@pytest.fixture(autouse=True)
def reawaken_assistant_mock(monkeypatch) -> AsyncMock:
    """Prevent space membership tests from calling live Communication services."""

    mock = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(
        "orchestra.services.space_membership_refresh_service.reawaken_assistant",
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
    monkeypatch.setattr(
        "orchestra.web.api.organization.views.create_pubsub_topic",
        mock,
    )
    return mock


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
        json={
            "name": "Personal Dispatch",
            "description": "Personal dispatch workstream for customer operations.",
        },
    )
    assert patched.status_code == status.HTTP_200_OK, patched.json()
    assert patched.json()["name"] == "Personal Dispatch"
    assert (
        patched.json()["description"]
        == "Personal dispatch workstream for customer operations."
    )

    deleted = await client.delete(
        f"/v0/spaces/{created['space_id']}",
        headers=owner["headers"],
    )
    assert deleted.status_code == status.HTTP_204_NO_CONTENT, deleted.text


@pytest.mark.anyio
async def test_create_space_conflicts_on_normalized_name_in_same_scope(
    client: AsyncClient,
) -> None:
    owner = await create_test_user(
        client,
        f"space-dedupe-owner-{uuid4().hex[:8]}@test.com",
    )
    first = await client.post(
        "/v0/spaces",
        headers=owner["headers"],
        json={
            "name": "Revenue Desk",
            "description": "Revenue workspace for planning and execution outcomes.",
            "organization_id": None,
        },
    )
    assert first.status_code == status.HTTP_201_CREATED, first.json()
    first_id = first.json()["space_id"]

    duplicate = await client.post(
        "/v0/spaces",
        headers=owner["headers"],
        json={
            "name": "  revenue desk ",
            "description": "Revenue workspace for planning and execution outcomes.",
            "organization_id": None,
        },
    )
    assert duplicate.status_code == status.HTTP_409_CONFLICT, duplicate.json()
    detail = duplicate.json()["detail"]
    assert detail["error"] == "space_already_exists"
    assert detail["existing_id"] == first_id


@pytest.mark.anyio
async def test_create_space_allows_same_name_across_personal_users(
    client: AsyncClient,
) -> None:
    first_owner = await create_test_user(
        client,
        f"space-cross-scope-owner-a-{uuid4().hex[:8]}@test.com",
    )
    second_owner = await create_test_user(
        client,
        f"space-cross-scope-owner-b-{uuid4().hex[:8]}@test.com",
    )
    personal = await client.post(
        "/v0/spaces",
        headers=first_owner["headers"],
        json={
            "name": "Support Desk",
            "description": "Support workspace for customer escalations and triage.",
            "organization_id": None,
        },
    )
    assert personal.status_code == status.HTTP_201_CREATED, personal.json()

    second_scope_space = await client.post(
        "/v0/spaces",
        headers=second_owner["headers"],
        json={
            "name": "Support Desk",
            "description": "Support workspace for customer escalations and triage.",
            "organization_id": None,
        },
    )
    assert (
        second_scope_space.status_code == status.HTTP_201_CREATED
    ), second_scope_space.json()
    assert second_scope_space.json()["space_id"] != personal.json()["space_id"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "description",
    [
        "too short",
        "x" * 1001,
        "." * 20,
        "a" * 20,
        "Placeholder description for space Example",
    ],
)
async def test_create_rejects_unhelpful_space_descriptions(
    client: AsyncClient,
    description: str,
) -> None:
    """Space creation requires descriptions that help assistants route memory."""

    owner = await create_test_user(client, "space-description-invalid@test.com")

    response = await client.post(
        "/v0/spaces",
        headers=owner["headers"],
        json={
            "name": "Description Guard",
            "description": description,
            "organization_id": None,
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_update_accepts_meaningful_space_description(
    client: AsyncClient,
    dbsession: Session,
    reawaken_assistant_mock: AsyncMock,
) -> None:
    """Patch validation uses the same meaningful-description contract."""

    owner = await create_test_user(client, "space-description-valid@test.com")
    assistant = _make_assistant(dbsession, owner_id=owner["id"])
    created = await _create_space(
        client,
        owner["headers"],
        name="Description Valid",
    )
    add_member = await client.post(
        f"/v0/spaces/{created['space_id']}/members",
        headers=owner["headers"],
        json={"assistant_id": assistant.agent_id},
    )
    assert add_member.status_code == status.HTTP_201_CREATED, add_member.json()
    reawaken_assistant_mock.reset_mock()

    response = await client.patch(
        f"/v0/spaces/{created['space_id']}",
        headers=owner["headers"],
        json={
            "description": "Meaningful project workspace for support operations.",
        },
    )

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert (
        response.json()["description"]
        == "Meaningful project workspace for support operations."
    )
    reawaken_assistant_mock.assert_awaited_once()
    assert reawaken_assistant_mock.await_args.kwargs["data"] == {
        "assistant_id": str(assistant.agent_id),
        "space_ids": json.dumps([created["space_id"]]),
        "space_summaries": json.dumps(
            [
                {
                    "space_id": created["space_id"],
                    "name": "Description Valid",
                    "description": "Meaningful project workspace for support operations.",
                },
            ],
        ),
        "update_kind": "membership",
    }


@pytest.mark.anyio
async def test_same_owner_member_add_projects_sorted_space_ids(
    client: AsyncClient,
    dbsession: Session,
    reawaken_assistant_mock: AsyncMock,
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
    assert reawaken_assistant_mock.await_count == 2
    assert reawaken_assistant_mock.await_args.kwargs["data"] == {
        "assistant_id": str(assistant.agent_id),
        "space_ids": json.dumps([first_space["space_id"], second_space["space_id"]]),
        "space_summaries": json.dumps(
            [
                {
                    "space_id": first_space["space_id"],
                    "name": "Alpha",
                    "description": "Alpha shared workspace",
                },
                {
                    "space_id": second_space["space_id"],
                    "name": "Beta",
                    "description": "Beta shared workspace",
                },
            ],
        ),
        "update_kind": "membership",
    }

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
    assert public_read.json()["info"][0]["space_summaries"] == [
        {
            "space_id": first_space["space_id"],
            "name": "Alpha",
            "description": "Alpha shared workspace",
        },
        {
            "space_id": second_space["space_id"],
            "name": "Beta",
            "description": "Beta shared workspace",
        },
    ]

    admin_read = await client.get(
        f"/v0/admin/assistant?agent_id={assistant.agent_id}"
        "&from_fields=agent_id,space_ids,space_summaries",
        headers=ADMIN_HEADERS,
    )
    assert admin_read.status_code == status.HTTP_200_OK, admin_read.json()
    assert admin_read.json()["info"] == [
        {
            "agent_id": str(assistant.agent_id),
            "space_ids": [first_space["space_id"], second_space["space_id"]],
            "space_summaries": [
                {
                    "space_id": first_space["space_id"],
                    "name": "Alpha",
                    "description": "Alpha shared workspace",
                },
                {
                    "space_id": second_space["space_id"],
                    "name": "Beta",
                    "description": "Beta shared workspace",
                },
            ],
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

    dbsession.get(Space, second_space["space_id"]).status = "deleting"
    dbsession.commit()

    public_read = await client.get(
        f"/v0/assistant?agent_id={assistant.agent_id}",
        headers=owner["headers"],
    )
    assert public_read.status_code == status.HTTP_200_OK, public_read.json()
    assert public_read.json()["info"][0]["space_ids"] == [first_space["space_id"]]
    assert public_read.json()["info"][0]["space_summaries"] == [
        {
            "space_id": first_space["space_id"],
            "name": "Alpha",
            "description": "Alpha shared workspace",
        },
    ]

    admin_read = await client.get(
        f"/v0/admin/assistant?agent_id={assistant.agent_id}"
        "&from_fields=agent_id,space_ids,space_summaries",
        headers=ADMIN_HEADERS,
    )
    assert admin_read.status_code == status.HTTP_200_OK, admin_read.json()
    assert admin_read.json()["info"] == [
        {
            "agent_id": str(assistant.agent_id),
            "space_ids": [first_space["space_id"]],
            "space_summaries": [
                {
                    "space_id": first_space["space_id"],
                    "name": "Alpha",
                    "description": "Alpha shared workspace",
                },
            ],
        },
    ]

    summaries = await client.get(
        f"/v0/assistants/{assistant.agent_id}/spaces",
        headers=owner["headers"],
    )
    assert summaries.status_code == status.HTTP_200_OK, summaries.json()
    assert [space["space_id"] for space in summaries.json()] == [
        first_space["space_id"],
    ]


@pytest.mark.anyio
async def test_remove_space_member_cleans_space_contact_overlays(
    client: AsyncClient,
    dbsession: Session,
    reawaken_assistant_mock: AsyncMock,
) -> None:
    """Direct member removal drops only overlays owned by that membership."""
    owner = await create_test_user(client, "space-member-remove@test.com")
    assistant = _make_assistant(dbsession, owner_id=owner["id"])
    removed_space = await _create_space(client, owner["headers"], name="Removed")
    retained_space = await _create_space(client, owner["headers"], name="Retained")
    for space in (removed_space, retained_space):
        add_member = await client.post(
            f"/v0/spaces/{space['space_id']}/members",
            headers=owner["headers"],
            json={"assistant_id": assistant.agent_id},
        )
        assert add_member.status_code == status.HTTP_201_CREATED, add_member.json()
    reawaken_assistant_mock.reset_mock()
    dbsession.add_all(
        [
            ContactMembership(
                assistant_id=assistant.agent_id,
                contact_id=7,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=removed_space["space_id"],
                relationship="coworker",
            ),
            ContactMembership(
                assistant_id=assistant.agent_id,
                contact_id=8,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_SPACE,
                target_space_id=retained_space["space_id"],
                relationship="coworker",
            ),
            ContactMembership(
                assistant_id=assistant.agent_id,
                contact_id=3,
                target_scope=CONTACT_MEMBERSHIP_SCOPE_PERSONAL,
                relationship="self",
            ),
        ],
    )
    dbsession.commit()

    response = await client.delete(
        f"/v0/spaces/{removed_space['space_id']}/members/{assistant.agent_id}",
        headers=owner["headers"],
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    reawaken_assistant_mock.assert_awaited_once()
    assert reawaken_assistant_mock.await_args.kwargs["data"] == {
        "assistant_id": str(assistant.agent_id),
        "space_ids": json.dumps([retained_space["space_id"]]),
        "space_summaries": json.dumps(
            [
                {
                    "space_id": retained_space["space_id"],
                    "name": "Retained",
                    "description": "Retained shared workspace",
                },
            ],
        ),
        "update_kind": "membership",
    }
    remaining = (
        dbsession.query(ContactMembership)
        .filter(ContactMembership.assistant_id == assistant.agent_id)
        .order_by(
            ContactMembership.target_scope,
            ContactMembership.target_space_id,
            ContactMembership.contact_id,
        )
        .all()
    )
    assert all(
        not (
            row.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE
            and row.target_space_id == removed_space["space_id"]
        )
        for row in remaining
    )
    retained_rows = [
        row
        for row in remaining
        if row.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE
        and row.target_space_id == retained_space["space_id"]
    ]
    assert {(row.contact_id, row.relationship) for row in retained_rows} == {
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
        (8, "coworker"),
    }
    personal_rows = [
        row
        for row in remaining
        if row.target_scope == CONTACT_MEMBERSHIP_SCOPE_PERSONAL
    ]
    assert {(row.contact_id, row.relationship) for row in personal_rows} == {
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
        (3, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
    }
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(
            AssistantSpaceMembership.assistant_id == assistant.agent_id,
            AssistantSpaceMembership.space_id == removed_space["space_id"],
        )
        .one_or_none()
        is None
    )
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(
            AssistantSpaceMembership.assistant_id == assistant.agent_id,
            AssistantSpaceMembership.space_id == retained_space["space_id"],
        )
        .one_or_none()
        is not None
    )


@pytest.mark.anyio
async def test_create_org_space_auto_adds_coordinator_and_publishes_refresh(
    client: AsyncClient,
    dbsession: Session,
    reawaken_assistant_mock: AsyncMock,
    monkeypatch,
) -> None:
    """Organization-scoped spaces always add the Coordinator as a live member."""

    monkeypatch.setattr(
        "orchestra.web.api.organization.views.create_pubsub_topic",
        AsyncMock(return_value={"success": True, "skipped": True}),
    )
    owner = await create_test_user(client, "space-org-coordinator-owner@test.com")
    organization = await create_test_org(client, owner, "Coordinator Space Org")
    reawaken_assistant_mock.reset_mock()

    created = await _create_space(
        client,
        owner["headers"],
        name="Org Setup",
        organization_id=organization["id"],
    )

    coordinator = (
        dbsession.query(Assistant)
        .filter(
            Assistant.organization_id == organization["id"],
            Assistant.is_coordinator.is_(True),
        )
        .one()
    )
    membership = (
        dbsession.query(AssistantSpaceMembership)
        .filter(
            AssistantSpaceMembership.assistant_id == coordinator.agent_id,
            AssistantSpaceMembership.space_id == created["space_id"],
        )
        .one_or_none()
    )
    assert membership is not None
    coordinator_overlays = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == coordinator.agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE,
            ContactMembership.target_space_id == created["space_id"],
        )
        .order_by(ContactMembership.contact_id.asc())
        .all()
    )
    assert [(row.contact_id, row.relationship) for row in coordinator_overlays] == [
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
    ]

    reawaken_assistant_mock.assert_awaited_once()
    payload = reawaken_assistant_mock.await_args.kwargs["data"]
    assert payload["assistant_id"] == str(coordinator.agent_id)
    assert payload["update_kind"] == "membership"
    assert created["space_id"] in json.loads(payload["space_ids"])
    assert any(
        summary["space_id"] == created["space_id"] and summary["name"] == "Org Setup"
        for summary in json.loads(payload["space_summaries"])
    )


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
    expected_boss_policy = (
        "Your immediate manager, please do whatever they ask you to do within reason, "
        "and do *not* withhold any information from them."
    )
    assert (
        dbsession.query(AssistantSpaceMembership)
        .filter(
            AssistantSpaceMembership.assistant_id == assistant.agent_id,
            AssistantSpaceMembership.space_id == space["space_id"],
        )
        .one()
    )
    overlays = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == assistant.agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE,
            ContactMembership.target_space_id == space["space_id"],
            ContactMembership.relationship.in_(
                [
                    CONTACT_MEMBERSHIP_RELATIONSHIP_SELF,
                    CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
                ],
            ),
        )
        .order_by(ContactMembership.contact_id.asc())
        .all()
    )
    assert [
        (
            row.contact_id,
            row.relationship,
            row.should_respond,
            row.response_policy,
            row.can_edit,
        )
        for row in overlays
    ] == [
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF, True, "", True),
        (
            1,
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
            True,
            expected_boss_policy,
            True,
        ),
    ]
    dbsession.query(ContactMembership).filter(
        ContactMembership.assistant_id == assistant.agent_id,
        ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE,
        ContactMembership.target_space_id == space["space_id"],
        ContactMembership.relationship == CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
    ).delete()
    dbsession.commit()

    second_add = await client.post(
        f"/v0/spaces/{space['space_id']}/members",
        headers=owner["headers"],
        json={"assistant_id": assistant.agent_id},
    )
    assert second_add.status_code == status.HTTP_200_OK, second_add.json()
    assert second_add.json()["membership_status"] == "active"
    overlays_after_second_add = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == assistant.agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE,
            ContactMembership.target_space_id == space["space_id"],
        )
        .order_by(ContactMembership.contact_id.asc())
        .all()
    )
    assert [
        (
            row.contact_id,
            row.relationship,
            row.should_respond,
            row.response_policy,
            row.can_edit,
        )
        for row in overlays_after_second_add
    ] == [
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF, True, "", True),
        (
            1,
            CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS,
            True,
            expected_boss_policy,
            True,
        ),
    ]


@pytest.mark.anyio
async def test_cross_owner_member_add_to_personal_space_is_forbidden(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    """Personal spaces reject cross-owner assistant membership adds."""

    inviter = await create_test_user(client, "space-inviter@test.com")
    invited_owner = await create_test_user(client, "space-invited-owner@test.com")
    assistant = _make_assistant(dbsession, owner_id=invited_owner["id"])
    space = await _create_space(client, inviter["headers"], name="Shared Home")

    add_response = await client.post(
        f"/v0/spaces/{space['space_id']}/members",
        headers=inviter["headers"],
        json={"assistant_id": assistant.agent_id},
    )
    assert add_response.status_code == status.HTTP_403_FORBIDDEN, add_response.json()
    assert add_response.json()["detail"] == "assistant_not_eligible_for_space"


@pytest.mark.anyio
async def test_org_member_target_add_auto_provisions_personal_coordinator_and_is_idempotent(
    client: AsyncClient,
    dbsession: Session,
    reawaken_assistant_mock: AsyncMock,
    monkeypatch,
) -> None:
    """Member-target adds auto-provision personal Coordinators and stay idempotent."""

    create_topic_mock = AsyncMock(return_value={"success": True, "skipped": True})
    monkeypatch.setattr(
        "orchestra.services.coordinator_service.create_pubsub_topic",
        create_topic_mock,
    )
    owner = await create_test_user(client, "space-member-target-owner@test.com")
    member = await create_test_user(client, "space-member-target-member@test.com")
    organization = await create_test_org(client, owner, "Space Member Target Org")
    add_member = await client.post(
        f"/v0/organizations/{organization['id']}/members",
        headers=owner["headers"],
        json={"user_id": member["id"]},
    )
    assert add_member.status_code == status.HTTP_201_CREATED, add_member.json()
    space = await _create_space(
        client,
        owner["headers"],
        name="Org Member Target",
        organization_id=organization["id"],
    )
    reawaken_assistant_mock.reset_mock()

    first_add = await client.post(
        f"/v0/spaces/{space['space_id']}/members",
        headers=owner["headers"],
        json={"member_user_id": member["id"]},
    )
    assert first_add.status_code == status.HTTP_201_CREATED, first_add.json()
    first_body = first_add.json()
    assert first_body["membership_status"] == "active"

    coordinator = (
        dbsession.query(Assistant)
        .filter(
            Assistant.user_id == member["id"],
            Assistant.organization_id.is_(None),
            Assistant.is_coordinator.is_(True),
        )
        .one()
    )
    assert first_body["assistant_id"] == coordinator.agent_id
    first_topic_calls = create_topic_mock.await_count
    assert first_topic_calls >= 1
    first_overlays = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == coordinator.agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE,
            ContactMembership.target_space_id == space["space_id"],
        )
        .order_by(ContactMembership.contact_id.asc())
        .all()
    )
    assert [(row.contact_id, row.relationship) for row in first_overlays] == [
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
    ]

    create_topic_mock.side_effect = RuntimeError(
        "idempotent membership add should not reprovision topic",
    )

    second_add = await client.post(
        f"/v0/spaces/{space['space_id']}/members",
        headers=owner["headers"],
        json={"member_user_id": member["id"]},
    )
    assert second_add.status_code == status.HTTP_200_OK, second_add.json()
    assert second_add.json()["assistant_id"] == coordinator.agent_id
    assert second_add.json()["membership_status"] == "active"
    assert create_topic_mock.await_count == first_topic_calls
    second_overlays = (
        dbsession.query(ContactMembership)
        .filter(
            ContactMembership.assistant_id == coordinator.agent_id,
            ContactMembership.target_scope == CONTACT_MEMBERSHIP_SCOPE_SPACE,
            ContactMembership.target_space_id == space["space_id"],
        )
        .order_by(ContactMembership.contact_id.asc())
        .all()
    )
    assert [(row.contact_id, row.relationship) for row in second_overlays] == [
        (0, CONTACT_MEMBERSHIP_RELATIONSHIP_SELF),
        (1, CONTACT_MEMBERSHIP_RELATIONSHIP_BOSS),
    ]

    memberships = (
        dbsession.query(AssistantSpaceMembership)
        .filter(
            AssistantSpaceMembership.assistant_id == coordinator.agent_id,
            AssistantSpaceMembership.space_id == space["space_id"],
        )
        .all()
    )
    assert len(memberships) == 1
    reawaken_assistant_mock.assert_awaited_once()


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
    assert get_response.status_code == status.HTTP_403_FORBIDDEN
    assert patch_response.status_code == status.HTTP_403_FORBIDDEN
    assert add_member_response.status_code == status.HTTP_403_FORBIDDEN
    assert list_members_response.status_code == status.HTTP_403_FORBIDDEN
