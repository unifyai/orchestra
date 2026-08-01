"""Pending-owner invites: hand the org over when the invitee accepts.

Supports the white-glove path where Unify provisions an organization and
then invites the person who is to own it — the invitee has no account at
provisioning time, so the existing transfer endpoint (which requires an
existing member) cannot be used up front.
"""

import pytest
from fastapi import status

from orchestra.db.dao.organization_dao import OrganizationDAO
from orchestra.db.dao.organization_invite_dao import OrganizationInviteDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


async def _make_org(client, owner, name):
    resp = await client.post(
        "/v0/organizations",
        json={"name": name},
        headers=owner["headers"],
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.json()
    return resp.json()["id"]


def _role_name(dbsession, user_id, org_id):
    member = OrganizationMemberDAO(dbsession).get_member(user_id, org_id)
    if member is None:
        return None
    return RoleDAO(dbsession).get(member.role_id).name


@pytest.mark.anyio
async def test_accepting_pending_owner_invite_transfers_org(client, dbsession):
    owner = await create_test_user(client, "po_owner@test.com")
    heir = await create_test_user(client, "po_heir@test.com")
    org_id = await _make_org(client, owner, "Pending Owner Org")

    invite = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "po_heir@test.com", "transfers_ownership": True},
        headers=owner["headers"],
    )
    assert invite.status_code == status.HTTP_201_CREATED, invite.json()
    assert invite.json()["transfers_ownership"] is True

    accept = await client.post(
        f"/v0/invites/{invite.json()['token']}/accept",
        headers=heir["headers"],
    )
    assert accept.status_code == status.HTTP_200_OK, accept.json()

    dbsession.expire_all()
    org = OrganizationDAO(dbsession).get(org_id)
    assert org.owner_id == heir["id"]
    assert _role_name(dbsession, heir["id"], org_id) == "Owner"
    # The outgoing owner keeps access, demoted rather than removed.
    assert _role_name(dbsession, owner["id"], org_id) == "Admin"


@pytest.mark.anyio
async def test_ordinary_invite_leaves_ownership_alone(client, dbsession):
    owner = await create_test_user(client, "po_plain_owner@test.com")
    invitee = await create_test_user(client, "po_plain_invitee@test.com")
    org_id = await _make_org(client, owner, "Plain Invite Org")

    invite = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "po_plain_invitee@test.com"},
        headers=owner["headers"],
    )
    assert invite.json()["transfers_ownership"] is False

    accept = await client.post(
        f"/v0/invites/{invite.json()['token']}/accept",
        headers=invitee["headers"],
    )
    assert accept.status_code == status.HTTP_200_OK

    dbsession.expire_all()
    assert OrganizationDAO(dbsession).get(org_id).owner_id == owner["id"]
    assert _role_name(dbsession, owner["id"], org_id) == "Owner"


@pytest.mark.anyio
async def test_non_owner_cannot_invite_a_new_owner(client, dbsession):
    """org:write is enough to invite members, but not to give the org away."""
    owner = await create_test_user(client, "po_guard_owner@test.com")
    admin = await create_test_user(client, "po_guard_admin@test.com")
    org_id = await _make_org(client, owner, "Guard Invite Org")

    admin_role = RoleDAO(dbsession).get_by_name("Admin", organization_id=None)
    invite = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "po_guard_admin@test.com", "role_id": admin_role.id},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/invites/{invite.json()['token']}/accept",
        headers=admin["headers"],
    )

    attempt = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "po_guard_outsider@test.com", "transfers_ownership": True},
        headers=admin["headers"],
    )
    assert attempt.status_code == status.HTTP_403_FORBIDDEN
    assert "owner" in attempt.json()["detail"].lower()


@pytest.mark.anyio
async def test_only_one_pending_owner_invite_at_a_time(client, dbsession):
    owner = await create_test_user(client, "po_dupe_owner@test.com")
    org_id = await _make_org(client, owner, "Single Handover Org")

    first = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "po_dupe_first@test.com", "transfers_ownership": True},
        headers=owner["headers"],
    )
    assert first.status_code == status.HTTP_201_CREATED

    second = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "po_dupe_second@test.com", "transfers_ownership": True},
        headers=owner["headers"],
    )
    assert second.status_code == status.HTTP_409_CONFLICT
    assert "po_dupe_first@test.com" in second.json()["detail"]


@pytest.mark.anyio
async def test_refreshing_the_same_pending_owner_invite_is_allowed(client, dbsession):
    """Re-inviting the same person must not trip the one-outstanding rule."""
    owner = await create_test_user(client, "po_refresh_owner@test.com")
    org_id = await _make_org(client, owner, "Refresh Handover Org")

    payload = {"email": "po_refresh_heir@test.com", "transfers_ownership": True}
    first = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json=payload,
        headers=owner["headers"],
    )
    assert first.status_code == status.HTTP_201_CREATED

    again = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json=payload,
        headers=owner["headers"],
    )
    assert again.status_code == status.HTTP_201_CREATED
    assert again.json()["transfers_ownership"] is True


@pytest.mark.anyio
async def test_admin_can_stage_a_handover_on_a_provisioned_org(client, dbsession):
    """The white-glove flow: Unify provisions, then names the future owner."""
    staff = await create_test_user(client, "po_staff@test.com")
    heir = await create_test_user(client, "po_admin_heir@test.com")

    created = await client.post(
        "/v0/admin/organizations",
        json={"name": "White Glove Org", "creator_user_id": staff["id"]},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == status.HTTP_201_CREATED, created.json()
    org_id = created.json()["id"]

    invited = await client.post(
        f"/v0/admin/organization/{org_id}/invite",
        json={
            "email": "po_admin_heir@test.com",
            "role_name": "Admin",
            "transfers_ownership": True,
        },
        headers=ADMIN_HEADERS,
    )
    assert invited.status_code == status.HTTP_201_CREATED, invited.json()

    listed = await client.get(
        f"/v0/admin/organization/{org_id}/invites",
        headers=ADMIN_HEADERS,
    )
    assert listed.json()[0]["transfers_ownership"] is True

    # The admin endpoints deliberately never expose the token (it reaches the
    # invitee only by email), so read it directly for the test.
    token = (
        OrganizationInviteDAO(dbsession)
        .get_by_id(
            invited.json()["invite_id"],
        )
        .token
    )

    accept = await client.post(f"/v0/invites/{token}/accept", headers=heir["headers"])
    assert accept.status_code == status.HTTP_200_OK, accept.json()

    dbsession.expire_all()
    assert OrganizationDAO(dbsession).get(org_id).owner_id == heir["id"]
    assert _role_name(dbsession, staff["id"], org_id) == "Admin"


@pytest.mark.anyio
async def test_handover_follows_the_current_owner_not_the_inviter(client, dbsession):
    """Ownership moved while the invite was pending — the live owner is demoted."""
    owner = await create_test_user(client, "po_chain_owner@test.com")
    interim = await create_test_user(client, "po_chain_interim@test.com")
    heir = await create_test_user(client, "po_chain_heir@test.com")
    org_id = await _make_org(client, owner, "Chained Handover Org")

    pending = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "po_chain_heir@test.com", "transfers_ownership": True},
        headers=owner["headers"],
    )
    pending_token = pending.json()["token"]

    # Interim joins and ownership is handed to them the ordinary way.
    interim_invite = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "po_chain_interim@test.com"},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/invites/{interim_invite.json()['token']}/accept",
        headers=interim["headers"],
    )
    transferred = await client.post(
        f"/v0/organizations/{org_id}/transfer-ownership",
        json={"new_owner_id": interim["id"]},
        headers=owner["headers"],
    )
    assert transferred.status_code == status.HTTP_200_OK, transferred.json()

    accept = await client.post(
        f"/v0/invites/{pending_token}/accept",
        headers=heir["headers"],
    )
    assert accept.status_code == status.HTTP_200_OK

    dbsession.expire_all()
    assert OrganizationDAO(dbsession).get(org_id).owner_id == heir["id"]
    # Demotion lands on whoever actually held the org, not the original inviter.
    assert _role_name(dbsession, interim["id"], org_id) == "Admin"
