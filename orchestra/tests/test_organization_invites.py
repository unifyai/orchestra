"""Tests for organization invite functionality."""


import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.dao.organization_invite_dao import OrganizationInviteDAO
from orchestra.db.dao.organization_member_dao import OrganizationMemberDAO
from orchestra.db.dao.role_dao import RoleDAO
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user


@pytest.mark.anyio
async def test_invite_user_to_organization(client: AsyncClient, dbsession):
    """Test inviting a user to an organization."""
    owner = await create_test_user(client, "invite_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Invite Test Org"},
        headers=owner["headers"],
    )
    assert org_response.status_code == status.HTTP_201_CREATED
    org_id = org_response.json()["id"]

    # Invite a new user
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "new_user@test.com"},
        headers=owner["headers"],
    )
    assert invite_response.status_code == status.HTTP_201_CREATED

    invite = invite_response.json()
    assert invite["invitee_email"] == "new_user@test.com"
    assert invite["organization_id"] == org_id
    assert invite["token"] is not None
    assert invite["role_name"] == "Member"  # Default role


@pytest.mark.anyio
async def test_invite_with_custom_role(client: AsyncClient, dbsession):
    """Test inviting a user with a specific role."""
    owner = await create_test_user(client, "invite_role_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Role Invite Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Admin role
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)

    # Invite with Admin role
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "admin_invite@test.com", "role_id": admin_role.id},
        headers=owner["headers"],
    )
    assert invite_response.status_code == status.HTTP_201_CREATED
    assert invite_response.json()["role_name"] == "Admin"


@pytest.mark.anyio
async def test_cannot_invite_with_owner_role(client: AsyncClient, dbsession):
    """Test that Owner role cannot be assigned via invite."""
    owner = await create_test_user(client, "owner_invite_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Owner Invite Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Owner role
    role_dao = RoleDAO(dbsession)
    owner_role = role_dao.get_by_name("Owner", organization_id=None)

    # Try to invite with Owner role
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "owner_invite@test.com", "role_id": owner_role.id},
        headers=owner["headers"],
    )
    assert invite_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "Owner role" in invite_response.json()["detail"]


@pytest.mark.anyio
async def test_invite_existing_member_returns_conflict(client: AsyncClient, dbsession):
    """Test that inviting an existing member returns conflict."""
    owner = await create_test_user(client, "existing_owner@test.com")
    member = await create_test_user(client, "existing_member@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Existing Member Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Add member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )

    # Try to invite the same member
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "existing_member@test.com"},
        headers=owner["headers"],
    )
    assert invite_response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.anyio
async def test_resend_pending_invite(client: AsyncClient, dbsession):
    """Test that resending to same email returns existing invite."""
    owner = await create_test_user(client, "resend_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Resend Invite Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Send first invite
    invite1_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "resend_user@test.com"},
        headers=owner["headers"],
    )
    assert invite1_response.status_code == status.HTTP_201_CREATED
    invite1 = invite1_response.json()

    # Send second invite to same email
    invite2_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "resend_user@test.com"},
        headers=owner["headers"],
    )
    assert invite2_response.status_code == status.HTTP_201_CREATED
    invite2 = invite2_response.json()

    # Should be the same invite (same token)
    assert invite1["token"] == invite2["token"]
    assert invite1["id"] == invite2["id"]


@pytest.mark.anyio
async def test_list_organization_invites(client: AsyncClient, dbsession):
    """Test listing organization invites."""
    owner = await create_test_user(client, "list_inv_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "List Invites Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Send multiple invites
    await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "invite1@test.com"},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "invite2@test.com"},
        headers=owner["headers"],
    )

    # List invites
    list_response = await client.get(
        f"/v0/organizations/{org_id}/invites",
        headers=owner["headers"],
    )
    assert list_response.status_code == status.HTTP_200_OK
    invites = list_response.json()["invites"]
    assert len(invites) == 2


@pytest.mark.anyio
async def test_cancel_invite(client: AsyncClient, dbsession):
    """Test cancelling an invite."""
    owner = await create_test_user(client, "cancel_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Cancel Invite Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Send invite
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "cancel_user@test.com"},
        headers=owner["headers"],
    )
    invite_id = invite_response.json()["id"]

    # Cancel invite
    cancel_response = await client.delete(
        f"/v0/organizations/{org_id}/invites/{invite_id}",
        headers=owner["headers"],
    )
    assert cancel_response.status_code == status.HTTP_204_NO_CONTENT

    # Verify invite is gone
    list_response = await client.get(
        f"/v0/organizations/{org_id}/invites",
        headers=owner["headers"],
    )
    assert len(list_response.json()["invites"]) == 0


@pytest.mark.anyio
async def test_accept_invite(client: AsyncClient, dbsession):
    """Test accepting an invite."""
    owner = await create_test_user(client, "accept_owner@test.com")
    invitee = await create_test_user(client, "accept_invitee@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Accept Invite Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Send invite to invitee's email
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "accept_invitee@test.com"},
        headers=owner["headers"],
    )
    token = invite_response.json()["token"]

    # Accept invite as invitee
    accept_response = await client.post(
        f"/v0/invites/{token}/accept",
        headers=invitee["headers"],
    )
    assert accept_response.status_code == status.HTTP_200_OK
    result = accept_response.json()
    assert result["organization_id"] == org_id
    assert "api_key" in result

    # Verify invitee is now a member
    org_member_dao = OrganizationMemberDAO(dbsession)
    member = org_member_dao.get_member(invitee["id"], org_id)
    assert member is not None


@pytest.mark.anyio
async def test_accept_invite_assigns_correct_role(client: AsyncClient, dbsession):
    """Test that accepting an invite assigns the correct role to the member."""
    owner = await create_test_user(client, "role_accept_owner@test.com")
    invitee = await create_test_user(client, "role_accept_invitee@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Role Accept Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get Admin role
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)

    # Send invite with Admin role
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "role_accept_invitee@test.com", "role_id": admin_role.id},
        headers=owner["headers"],
    )
    assert invite_response.status_code == status.HTTP_201_CREATED
    token = invite_response.json()["token"]

    # Accept invite as invitee
    accept_response = await client.post(
        f"/v0/invites/{token}/accept",
        headers=invitee["headers"],
    )
    assert accept_response.status_code == status.HTTP_200_OK

    # Verify member has the correct role
    org_member_dao = OrganizationMemberDAO(dbsession)
    member = org_member_dao.get_member(invitee["id"], org_id)
    assert member is not None
    assert member.role_id == admin_role.id


@pytest.mark.anyio
async def test_decline_invite(client: AsyncClient, dbsession):
    """Test declining an invite."""
    owner = await create_test_user(client, "decline_owner@test.com")
    invitee = await create_test_user(client, "decline_invitee@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Decline Invite Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Send invite
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "decline_invitee@test.com"},
        headers=owner["headers"],
    )
    token = invite_response.json()["token"]

    # Decline invite
    decline_response = await client.post(
        f"/v0/invites/{token}/decline",
        headers=invitee["headers"],
    )
    assert decline_response.status_code == status.HTTP_200_OK

    # Verify invite is deleted
    invite_dao = OrganizationInviteDAO(dbsession)
    invite = invite_dao.get_by_token(token)
    assert invite is None


@pytest.mark.anyio
async def test_wrong_user_cannot_accept_invite(client: AsyncClient, dbsession):
    """Test that a different user cannot accept someone else's invite."""
    owner = await create_test_user(client, "wrong_owner@test.com")
    intended = await create_test_user(client, "intended_user@test.com")
    wrong_user = await create_test_user(client, "wrong_user@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Wrong User Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Send invite to intended user
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "intended_user@test.com"},
        headers=owner["headers"],
    )
    token = invite_response.json()["token"]

    # Try to accept as wrong user
    accept_response = await client.post(
        f"/v0/invites/{token}/accept",
        headers=wrong_user["headers"],
    )
    assert accept_response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_expired_invite_cannot_be_accepted(client: AsyncClient, dbsession):
    """Test that expired invites cannot be accepted."""
    owner = await create_test_user(client, "expired_owner@test.com")
    invitee = await create_test_user(client, "expired_invitee@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Expired Invite Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create invite directly with past expiry
    invite_dao = OrganizationInviteDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    member_role = role_dao.get_by_name("Member", organization_id=None)

    invite = invite_dao.create(
        organization_id=org_id,
        invitee_email="expired_invitee@test.com",
        invited_by_user_id=owner["id"],
        role_id=member_role.id,
        expires_in_days=-1,  # Already expired
    )
    dbsession.commit()

    # Try to accept expired invite
    accept_response = await client.post(
        f"/v0/invites/{invite.token}/accept",
        headers=invitee["headers"],
    )
    assert accept_response.status_code == status.HTTP_400_BAD_REQUEST
    assert "expired" in accept_response.json()["detail"].lower()


@pytest.mark.anyio
async def test_list_my_pending_invites(client: AsyncClient, dbsession):
    """Test listing pending invites for current user."""
    owner1 = await create_test_user(client, "my_invites_owner1@test.com")
    owner2 = await create_test_user(client, "my_invites_owner2@test.com")
    invitee = await create_test_user(client, "my_invites_user@test.com")

    # Create two organizations
    org1_response = await client.post(
        "/v0/organizations",
        json={"name": "My Invites Org 1"},
        headers=owner1["headers"],
    )
    org1_id = org1_response.json()["id"]

    org2_response = await client.post(
        "/v0/organizations",
        json={"name": "My Invites Org 2"},
        headers=owner2["headers"],
    )
    org2_id = org2_response.json()["id"]

    # Send invites from both orgs to the same user
    await client.post(
        f"/v0/organizations/{org1_id}/invites",
        json={"email": "my_invites_user@test.com"},
        headers=owner1["headers"],
    )
    await client.post(
        f"/v0/organizations/{org2_id}/invites",
        json={"email": "my_invites_user@test.com"},
        headers=owner2["headers"],
    )

    # List pending invites as invitee
    list_response = await client.get(
        "/v0/invites/pending",
        headers=invitee["headers"],
    )
    assert list_response.status_code == status.HTTP_200_OK
    invites = list_response.json()["invites"]
    assert len(invites) == 2


@pytest.mark.anyio
async def test_cleanup_expired_invites(client: AsyncClient, dbsession):
    """Test cleanup of expired invites via admin endpoint."""
    owner = await create_test_user(client, "cleanup_owner@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Cleanup Test Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Create expired invites directly via DAO
    invite_dao = OrganizationInviteDAO(dbsession)
    role_dao = RoleDAO(dbsession)
    member_role = role_dao.get_by_name("Member", organization_id=None)

    # Create 3 expired invites directly in DB
    for i in range(3):
        invite_dao.create(
            organization_id=org_id,
            invitee_email=f"expired{i}@test.com",
            invited_by_user_id=owner["id"],
            role_id=member_role.id,
            expires_in_days=-1,
        )
    dbsession.commit()

    # Create 1 non-expired invite via API endpoint
    valid_invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "valid_cleanup@test.com"},
        headers=owner["headers"],
    )
    assert valid_invite_response.status_code == status.HTTP_201_CREATED

    # Call cleanup endpoint (requires admin auth)
    cleanup_response = await client.post(
        "/v0/admin/cleanup/expired-invites",
        headers=ADMIN_HEADERS,
    )
    assert cleanup_response.status_code == status.HTTP_200_OK
    result = cleanup_response.json()
    assert result["deleted_count"] == 3

    # Verify only valid invite remains
    list_response = await client.get(
        f"/v0/organizations/{org_id}/invites",
        headers=owner["headers"],
    )
    assert list_response.status_code == status.HTTP_200_OK
    invites = list_response.json()["invites"]
    assert len(invites) == 1
    assert invites[0]["invitee_email"] == "valid_cleanup@test.com"


@pytest.mark.anyio
async def test_non_member_cannot_invite(client: AsyncClient, dbsession):
    """Test that non-members cannot send invites."""
    owner = await create_test_user(client, "non_member_owner@test.com")
    outsider = await create_test_user(client, "non_member_outsider@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Non Member Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Try to invite as outsider
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "someone@test.com"},
        headers=outsider["headers"],
    )
    assert invite_response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.anyio
async def test_accept_already_accepted_invite(client: AsyncClient, dbsession):
    """Test that accepting an already accepted invite returns 404 (invite deleted)."""
    owner = await create_test_user(client, "double_accept_owner@test.com")
    invitee = await create_test_user(client, "double_accept_invitee@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": "Double Accept Org"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Send invite
    invite_response = await client.post(
        f"/v0/organizations/{org_id}/invites",
        json={"email": "double_accept_invitee@test.com"},
        headers=owner["headers"],
    )
    token = invite_response.json()["token"]

    # Accept first time
    accept1 = await client.post(
        f"/v0/invites/{token}/accept",
        headers=invitee["headers"],
    )
    assert accept1.status_code == status.HTTP_200_OK

    # Try to accept again - invite should be deleted, so 404
    accept2 = await client.post(
        f"/v0/invites/{token}/accept",
        headers=invitee["headers"],
    )
    # Should fail because invite was deleted after acceptance
    assert accept2.status_code == status.HTTP_404_NOT_FOUND
