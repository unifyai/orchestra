"""Staff access: marking Unify people embedded in a customer org.

Covers the marker itself, the bounded-by-default grant, the unbounded
override for standing arrangements, and the invariant that matters most —
a lapsed grant stops authorising without anything being deleted, and the
organization owner is never subject to either.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import status

from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.resource_access_dao import ResourceAccessDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.services.personal_workspace_service import UNIFY_ORGANIZATION_NAME
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


async def _make_org(client, owner, name):
    resp = await client.post(
        "/v0/organizations",
        json={"name": name},
        headers=owner["headers"],
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.json()
    return resp.json()["id"]


async def _join(client, dbsession, org_id, owner, joiner, role_name="Admin"):
    """Invite *joiner* into *org_id* and accept, returning their membership."""
    role = RoleDAO(dbsession).get_by_name(role_name, organization_id=None)
    invite = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": joiner["email"], "role_id": role.id},
        headers=owner["headers"],
    )
    assert invite.status_code == status.HTTP_201_CREATED, invite.json()
    accept = await client.post(
        f"/v0/invites/{invite.json()['token']}/accept",
        headers=joiner["headers"],
    )
    assert accept.status_code == status.HTTP_200_OK, accept.json()
    dbsession.expire_all()
    return OrganizationMemberDAO(dbsession).get_member(joiner["id"], org_id)


async def _unify_staff(client, dbsession, email):
    """Staff — a verified unify.ai mailbox — who belongs to the Unify org.

    Staff identity now requires the domain as well as membership, so the
    caller's label is normalised onto ``@unify.ai`` regardless of what it
    was passed.
    """
    local = email.split("@", 1)[0]
    founder = await create_test_user(client, f"founder_{local}@unify.ai")
    staff = await create_test_user(client, f"{local}@unify.ai")
    unify_id = await _make_org(client, founder, UNIFY_ORGANIZATION_NAME)
    await _join(client, dbsession, unify_id, founder, staff, role_name="Member")
    return staff


@pytest.mark.anyio
async def test_unify_staff_joining_a_customer_org_is_marked(client, dbsession):
    staff = await _unify_staff(client, dbsession, "sa_staff@test.com")
    customer = await create_test_user(client, "sa_customer@test.com")
    org_id = await _make_org(client, customer, "Marked Customer Org")

    member = await _join(client, dbsession, org_id, customer, staff)

    assert member.is_staff_access is True
    # Bounded by default — access cannot become permanent through neglect.
    assert member.staff_access_expires_at is not None
    assert member.staff_access_expires_at > datetime.now(timezone.utc)


@pytest.mark.anyio
async def test_ordinary_member_is_not_marked(client, dbsession):
    customer = await create_test_user(client, "sa_plain_owner@test.com")
    colleague = await create_test_user(client, "sa_plain_colleague@test.com")
    org_id = await _make_org(client, customer, "Plain Customer Org")

    member = await _join(client, dbsession, org_id, customer, colleague)

    assert member.is_staff_access is False
    assert member.staff_access_expires_at is None


@pytest.mark.anyio
async def test_unify_members_are_not_marked_inside_unify(client, dbsession):
    """The marker means "guest in someone else's tenant", not "works here"."""
    founder = await create_test_user(client, "sa_founder@test.com")
    colleague = await create_test_user(client, "sa_colleague@test.com")
    unify_id = await _make_org(client, founder, UNIFY_ORGANIZATION_NAME)

    member = await _join(client, dbsession, unify_id, founder, colleague)

    assert member.is_staff_access is False


@pytest.mark.anyio
async def test_lapsed_grant_stops_authorising(client, dbsession):
    """The core invariant: expiry denies permission, deleting nothing."""
    staff = await _unify_staff(client, dbsession, "sa_lapse@test.com")
    customer = await create_test_user(client, "sa_lapse_owner@test.com")
    org_id = await _make_org(client, customer, "Lapsing Org")
    member = await _join(client, dbsession, org_id, customer, staff)

    access = ResourceAccessDAO(dbsession)
    assert access.check_org_member_permission(staff["id"], org_id, "org:write") is True

    member.staff_access_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    dbsession.flush()

    assert access.check_org_member_permission(staff["id"], org_id, "org:write") is False
    assert access.check_org_member_permission(staff["id"], org_id, "org:read") is False
    # Denied, not removed — the seat is still there to extend or clean up.
    assert OrganizationMemberDAO(dbsession).get_member(staff["id"], org_id) is not None


@pytest.mark.anyio
async def test_unbounded_grant_never_lapses(client, dbsession):
    """The partner case: Unify sits in the org indefinitely."""
    staff = await _unify_staff(client, dbsession, "sa_partner@test.com")
    customer = await create_test_user(client, "sa_partner_owner@test.com")
    org_id = await _make_org(client, customer, "Partner Org")
    await _join(client, dbsession, org_id, customer, staff)

    resp = await client.put(
        f"/v0/admin/organization/{org_id}/members/{staff['id']}/staff-access",
        json={"staff_access": True},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK, resp.json()
    assert resp.json()["is_staff_access"] is True
    assert resp.json()["staff_access_expires_at"] is None

    dbsession.expire_all()
    access = ResourceAccessDAO(dbsession)
    assert access.check_org_member_permission(staff["id"], org_id, "org:write") is True


@pytest.mark.anyio
async def test_override_can_set_an_explicit_window(client, dbsession):
    staff = await _unify_staff(client, dbsession, "sa_window@test.com")
    customer = await create_test_user(client, "sa_window_owner@test.com")
    org_id = await _make_org(client, customer, "Window Org")
    await _join(client, dbsession, org_id, customer, staff)

    resp = await client.put(
        f"/v0/admin/organization/{org_id}/members/{staff['id']}/staff-access",
        json={"staff_access": True, "expires_in_days": 90},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK

    dbsession.expire_all()
    member = OrganizationMemberDAO(dbsession).get_member(staff["id"], org_id)
    assert member.staff_access_expires_at > datetime.now(timezone.utc) + timedelta(
        days=85,
    )


@pytest.mark.anyio
async def test_clearing_the_marker_also_clears_the_expiry(client, dbsession):
    staff = await _unify_staff(client, dbsession, "sa_clear@test.com")
    customer = await create_test_user(client, "sa_clear_owner@test.com")
    org_id = await _make_org(client, customer, "Clearing Org")
    await _join(client, dbsession, org_id, customer, staff)

    resp = await client.put(
        f"/v0/admin/organization/{org_id}/members/{staff['id']}/staff-access",
        json={"staff_access": False},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_200_OK

    dbsession.expire_all()
    member = OrganizationMemberDAO(dbsession).get_member(staff["id"], org_id)
    assert member.is_staff_access is False
    assert member.staff_access_expires_at is None


@pytest.mark.anyio
async def test_owner_cannot_be_marked_as_staff(client, dbsession):
    """An expiry on the owner's seat would revoke the owner's own access."""
    owner = await create_test_user(client, "sa_owner_guard@test.com")
    org_id = await _make_org(client, owner, "Owner Guard Org")

    resp = await client.put(
        f"/v0/admin/organization/{org_id}/members/{owner['id']}/staff-access",
        json={"staff_access": True},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "owner" in resp.json()["detail"].lower()


@pytest.mark.anyio
async def test_members_listing_exposes_the_badge(client, dbsession):
    """The console badge reads off the member listing."""
    staff = await _unify_staff(client, dbsession, "sa_badge@test.com")
    customer = await create_test_user(client, "sa_badge_owner@test.com")
    org_id = await _make_org(client, customer, "Badge Org")
    await _join(client, dbsession, org_id, customer, staff)

    resp = await client.get(
        f"/v0/organizations/{org_id}/members",
        headers=customer["headers"],
    )
    assert resp.status_code == status.HTTP_200_OK
    by_user = {m["user_id"]: m for m in resp.json()}

    assert by_user[staff["id"]]["is_staff_access"] is True
    assert by_user[staff["id"]]["staff_access_expires_at"] is not None
    assert by_user[customer["id"]]["is_staff_access"] is False


@pytest.mark.anyio
async def test_lapsed_report_lists_expired_seats(client, dbsession):
    staff = await _unify_staff(client, dbsession, "sa_report@test.com")
    customer = await create_test_user(client, "sa_report_owner@test.com")
    org_id = await _make_org(client, customer, "Report Org")
    member = await _join(client, dbsession, org_id, customer, staff)

    member.staff_access_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    dbsession.commit()

    resp = await client.get("/v0/admin/staff-access/lapsed", headers=ADMIN_HEADERS)
    assert resp.status_code == status.HTTP_200_OK
    listed = {(m["organization_id"], m["user_id"]) for m in resp.json()["members"]}
    assert (org_id, staff["id"]) in listed


@pytest.mark.anyio
async def test_handover_marks_the_outgoing_unify_owner(client, dbsession):
    """White-glove: the provisioner becomes a guest once ownership moves."""
    staff = await _unify_staff(client, dbsession, "sa_handover@test.com")
    heir = await create_test_user(client, "sa_handover_heir@test.com")
    org_id = await _make_org(client, staff, "Handover Org")

    invite = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": heir["email"], "transfers_ownership": True},
        headers=staff["headers"],
    )
    assert invite.status_code == status.HTTP_201_CREATED, invite.json()
    accept = await client.post(
        f"/v0/invites/{invite.json()['token']}/accept",
        headers=heir["headers"],
    )
    assert accept.status_code == status.HTTP_200_OK, accept.json()

    dbsession.expire_all()
    outgoing = OrganizationMemberDAO(dbsession).get_member(staff["id"], org_id)
    assert outgoing.is_staff_access is True
    assert outgoing.staff_access_expires_at is not None
    # The new owner is the customer, and is never badged.
    incoming = OrganizationMemberDAO(dbsession).get_member(heir["id"], org_id)
    assert incoming.is_staff_access is False
