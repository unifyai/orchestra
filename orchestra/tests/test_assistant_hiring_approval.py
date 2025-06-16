import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.settings import settings

from .utils import ADMIN_HEADERS, create_test_user, get_credits


@pytest.mark.anyio
async def test_user_request_hiring_approval(client: AsyncClient):
    test_user = await create_test_user(client, "approval_user1@example.com")

    # Initial request
    response = await client.post(
        "/v0/user/assistant-hiring-approval",
        headers=test_user["headers"],
    )
    assert response.status_code == status.HTTP_200_OK, response.json()
    data = response.json()
    assert (
        data["message"]
        == "Request for assistant hiring submitted. You've been added to the waitlist."
    )
    assert data["assistant_hiring_approval"] == "pending"

    # Verify status via admin endpoint (or a user GET endpoint if it shows this status)
    user_details = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={test_user['id']}",
        headers=ADMIN_HEADERS,
    )
    assert user_details.json()["assistant_hiring_approval"] == "pending"

    # Request again (should indicate it's already pending)
    response_again = await client.post(
        "/v0/user/assistant-hiring-approval",
        headers=test_user["headers"],
    )
    assert response_again.status_code == status.HTTP_200_OK, response_again.json()
    data_again = response_again.json()
    assert "already pending" in data_again["message"]
    assert data_again["assistant_hiring_approval"] == "pending"


async def test_admin_manage_hiring_approval(client: AsyncClient):
    test_user = await create_test_user(client, "approval_admin_user@example.com")
    user_id = test_user["id"]
    user_headers = test_user["headers"]

    # Endpoint for user to request approval
    user_request_url = "/v0/user/assistant-hiring-approval"

    # 1. Admin approves user
    approve_url = f"/v0/admin/auth-user/{user_id}/assistant-hiring-approval/approved"
    response_approve = await client.put(approve_url, headers=ADMIN_HEADERS)
    assert response_approve.status_code == status.HTTP_200_OK, response_approve.json()
    assert response_approve.json()["assistant_hiring_approval"] == "approved"

    # Verify status via admin GET
    user_details_resp = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user_id}",
        headers=ADMIN_HEADERS,
    )
    assert user_details_resp.status_code == status.HTTP_200_OK
    assert user_details_resp.json()["assistant_hiring_approval"] == "approved"

    # User requests again (should show approved)
    response_user_req_approved = await client.post(
        user_request_url,
        headers=user_headers,
    )
    assert response_user_req_approved.status_code == status.HTTP_200_OK
    assert "already approved" in response_user_req_approved.json()["message"]

    # 2. Admin rejects user
    reject_url = f"/v0/admin/auth-user/{user_id}/assistant-hiring-approval/rejected"
    response_reject = await client.put(reject_url, headers=ADMIN_HEADERS)
    assert response_reject.status_code == status.HTTP_200_OK, response_reject.json()
    assert response_reject.json()["assistant_hiring_approval"] == "rejected"

    # Verify status via admin GET
    user_details_resp_rejected = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user_id}",
        headers=ADMIN_HEADERS,
    )
    assert user_details_resp_rejected.status_code == status.HTTP_200_OK
    assert user_details_resp_rejected.json()["assistant_hiring_approval"] == "rejected"

    # User requests again (after rejection, user should be able to re-request and go to pending)
    response_user_req_after_reject = await client.post(
        user_request_url,
        headers=user_headers,
    )
    assert response_user_req_after_reject.status_code == status.HTTP_200_OK
    assert (
        "Request for assistant hiring submitted"
        in response_user_req_after_reject.json()["message"]
    )
    assert (
        response_user_req_after_reject.json()["assistant_hiring_approval"] == "pending"
    )

    # Verify status is now pending via admin GET
    user_details_resp_pending_after_reject = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user_id}",
        headers=ADMIN_HEADERS,
    )
    assert user_details_resp_pending_after_reject.status_code == status.HTTP_200_OK
    assert (
        user_details_resp_pending_after_reject.json()["assistant_hiring_approval"]
        == "pending"
    )

    # 3. Admin approves again (to test revoke from approved state)
    await client.put(approve_url, headers=ADMIN_HEADERS)  # Re-approve

    # 4. Admin revokes approval
    revoke_url = f"/v0/admin/auth-user/{user_id}/assistant-hiring-approval/revoked"
    response_revoke = await client.put(revoke_url, headers=ADMIN_HEADERS)
    assert response_revoke.status_code == status.HTTP_200_OK, response_revoke.json()
    assert response_revoke.json()["assistant_hiring_approval"] == "revoked"

    # Verify status via admin GET
    user_details_resp_revoked = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user_id}",
        headers=ADMIN_HEADERS,
    )
    assert user_details_resp_revoked.status_code == status.HTTP_200_OK
    assert user_details_resp_revoked.json()["assistant_hiring_approval"] == "revoked"

    # User requests again (after revocation, user should be able to re-request and go to pending)
    response_user_req_after_revoke = await client.post(
        user_request_url,
        headers=user_headers,
    )
    assert response_user_req_after_revoke.status_code == status.HTTP_200_OK
    assert (
        "Request for assistant hiring submitted"
        in response_user_req_after_revoke.json()["message"]
    )
    assert (
        response_user_req_after_revoke.json()["assistant_hiring_approval"] == "pending"
    )

    # 5. Admin sets to pending (final state for this test user if needed elsewhere)
    pending_url = f"/v0/admin/auth-user/{user_id}/assistant-hiring-approval/pending"
    response_set_pending = await client.put(pending_url, headers=ADMIN_HEADERS)
    assert response_set_pending.status_code == status.HTTP_200_OK
    assert response_set_pending.json()["assistant_hiring_approval"] == "pending"


@pytest.mark.anyio
async def test_admin_list_users_by_hiring_status(client: AsyncClient):
    user_pending = await create_test_user(client, "list_pending@example.com")
    user_approved = await create_test_user(client, "list_approved@example.com")
    user_rejected = await create_test_user(client, "list_rejected@example.com")
    user_revoked = await create_test_user(client, "list_revoked@example.com")
    user_none = await create_test_user(client, "list_none@example.com")

    # Set statuses
    await client.put(
        f"/v0/admin/auth-user/{user_pending['id']}/assistant-hiring-approval/pending",
        headers=ADMIN_HEADERS,
    )
    await client.put(
        f"/v0/admin/auth-user/{user_approved['id']}/assistant-hiring-approval/approved",
        headers=ADMIN_HEADERS,
    )
    await client.put(
        f"/v0/admin/auth-user/{user_rejected['id']}/assistant-hiring-approval/rejected",
        headers=ADMIN_HEADERS,
    )
    await client.put(
        f"/v0/admin/auth-user/{user_revoked['id']}/assistant-hiring-approval/revoked",
        headers=ADMIN_HEADERS,
    )
    # user_none will have assistant_hiring_approval = None by default

    list_url_base = "/v0/admin/auth-user/assistant-hiring-approval"

    # List pending
    response_pending_list = await client.get(
        f"{list_url_base}?status_filter=pending",
        headers=ADMIN_HEADERS,
    )
    assert response_pending_list.status_code == status.HTTP_200_OK
    pending_users = response_pending_list.json()
    assert any(
        u["id"] == user_pending["id"] and u["assistant_hiring_approval"] == "pending"
        for u in pending_users
    )
    assert not any(u["id"] == user_approved["id"] for u in pending_users)
    assert not any(u["id"] == user_rejected["id"] for u in pending_users)
    assert not any(u["id"] == user_revoked["id"] for u in pending_users)
    assert not any(u["id"] == user_none["id"] for u in pending_users)

    # List approved
    response_approved_list = await client.get(
        f"{list_url_base}?status_filter=approved",
        headers=ADMIN_HEADERS,
    )
    assert response_approved_list.status_code == status.HTTP_200_OK
    approved_users = response_approved_list.json()
    assert any(
        u["id"] == user_approved["id"] and u["assistant_hiring_approval"] == "approved"
        for u in approved_users
    )
    assert not any(u["id"] == user_pending["id"] for u in approved_users)

    # List rejected
    response_rejected_list = await client.get(
        f"{list_url_base}?status_filter=rejected",
        headers=ADMIN_HEADERS,
    )
    assert response_rejected_list.status_code == status.HTTP_200_OK
    rejected_users = response_rejected_list.json()
    assert any(
        u["id"] == user_rejected["id"] and u["assistant_hiring_approval"] == "rejected"
        for u in rejected_users
    )
    assert not any(u["id"] == user_pending["id"] for u in rejected_users)

    # List revoked
    response_revoked_list = await client.get(
        f"{list_url_base}?status_filter=revoked",
        headers=ADMIN_HEADERS,
    )
    assert response_revoked_list.status_code == status.HTTP_200_OK
    revoked_users = response_revoked_list.json()
    assert any(
        u["id"] == user_revoked["id"] and u["assistant_hiring_approval"] == "revoked"
        for u in revoked_users
    )
    assert not any(u["id"] == user_pending["id"] for u in revoked_users)

    # List users with no status (None)
    response_none_list = await client.get(
        f"{list_url_base}?status_filter=none",
        headers=ADMIN_HEADERS,
    )
    assert response_none_list.status_code == status.HTTP_200_OK
    none_status_users = response_none_list.json()
    assert any(
        u["id"] == user_none["id"] and u["assistant_hiring_approval"] is None
        for u in none_status_users
    )
    assert not any(u["id"] == user_pending["id"] for u in none_status_users)

    # List all
    response_all_list = await client.get(
        f"{list_url_base}?status_filter=all",
        headers=ADMIN_HEADERS,
    )
    assert response_all_list.status_code == status.HTTP_200_OK
    all_users = response_all_list.json()
    user_ids_in_all = {u["id"] for u in all_users}
    assert user_pending["id"] in user_ids_in_all
    assert user_approved["id"] in user_ids_in_all
    assert user_rejected["id"] in user_ids_in_all
    assert user_revoked["id"] in user_ids_in_all
    assert user_none["id"] in user_ids_in_all


@pytest.mark.anyio
async def test_one_time_approval_links_flow(client: AsyncClient):
    test_user_link = await create_test_user(client, "link_user@example.com")
    user_id = test_user_link["id"]
    user_headers = test_user_link["headers"]
    initial_credits = await get_credits(client, user_headers=user_headers)

    # Admin creates a one-time link
    create_link_payload = {"expires_in_days": 1}
    response_create_link = await client.post(
        "/v0/admin/assistant-hiring-one-time-link",
        json=create_link_payload,
        headers=ADMIN_HEADERS,
    )
    assert (
        response_create_link.status_code == status.HTTP_201_CREATED
    ), response_create_link.json()
    link_data = response_create_link.json()
    assert "token" in link_data
    one_time_token = link_data["token"]
    link_id_db = link_data["id"]

    # User claims the link
    claim_payload = {"token": one_time_token}
    response_claim = await client.post(
        "/v0/user/claim-assistant-hiring-one-time-link",
        json=claim_payload,
        headers=user_headers,
    )
    assert response_claim.status_code == status.HTTP_200_OK, response_claim.json()
    claim_data = response_claim.json()
    assert "Approval link successfully claimed" in claim_data["message"]
    assert claim_data["assistant_hiring_approval"] == "approved"

    # Verify user status is approved
    user_details = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user_id}",
        headers=ADMIN_HEADERS,
    )
    assert user_details.json()["assistant_hiring_approval"] == "approved"

    # Verify credits were granted
    credits_after_first_claim = await get_credits(client, user_headers=user_headers)
    expected_credits_after_first_claim = initial_credits + float(
        settings.assistant_creation_cost,
    )
    assert (
        credits_after_first_claim == expected_credits_after_first_claim
    ), f"Credits mismatch: expected {expected_credits_after_first_claim}, got {credits_after_first_claim}"

    # Try to claim again (by same user) - should indicate already used or success
    response_claim_again = await client.post(
        "/v0/user/claim-assistant-hiring-one-time-link",
        json=claim_payload,
        headers=user_headers,
    )
    assert response_claim_again.status_code == status.HTTP_200_OK
    assert "already benefited" in response_claim_again.json()["message"]
    assert response_claim_again.json()["assistant_hiring_approval"] == "approved"

    # Verify credits did not change after second claim attempt
    credits_after_second_claim_attempt = await get_credits(
        client,
        user_headers=user_headers,
    )
    assert (
        credits_after_second_claim_attempt == credits_after_first_claim
    ), "Credits should not change on subsequent claims of the same link by the same user."

    # Admin lists links, check if claimed
    response_list_links = await client.get(
        "/v0/admin/assistant-hiring-one-time-link",
        headers=ADMIN_HEADERS,
    )
    assert response_list_links.status_code == status.HTTP_200_OK
    links = response_list_links.json()
    claimed_link_in_list = next(
        (l for l in links if l["token"] == one_time_token),
        None,
    )
    assert claimed_link_in_list is not None
    assert claimed_link_in_list["user_id"] == user_id
    assert claimed_link_in_list["claimed_at"] is not None

    # Admin deletes the link
    response_delete_link = await client.delete(
        f"/v0/admin/assistant-hiring-one-time-link/{link_id_db}",
        headers=ADMIN_HEADERS,
    )
    assert response_delete_link.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.anyio
async def test_one_time_link_single_benefit_and_multiple_links(client: AsyncClient):
    # --- User 1: Claims first link, gets benefit ---
    user1 = await create_test_user(client, "link_benefiter@example.com")
    user1_id = user1["id"]
    user1_headers = user1["headers"]

    # Get initial state for user1
    initial_credits_u1 = await get_credits(client, user_headers=user1_headers)
    user1_details_before = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user1_id}",
        headers=ADMIN_HEADERS,
    )
    assert (
        user1_details_before.json()["assistant_hiring_approval"] is None
    )  # Or "pending" if they requested
    assert (
        user1_details_before.json()["has_claimed_approval_link"] is False
    )  # Assuming AuthUserResponse includes this

    # Admin creates Link L1
    resp_l1 = await client.post(
        "/v0/admin/assistant-hiring-one-time-link",
        json={"expires_in_days": 1},
        headers=ADMIN_HEADERS,
    )
    assert resp_l1.status_code == status.HTTP_201_CREATED
    l1_token = resp_l1.json()["token"]

    # User 1 claims Link L1
    resp_u1_claim_l1 = await client.post(
        "/v0/user/claim-assistant-hiring-one-time-link",
        json={"token": l1_token},
        headers=user1_headers,
    )
    assert resp_u1_claim_l1.status_code == status.HTTP_200_OK
    assert "credits awarded" in resp_u1_claim_l1.json()["message"]
    assert resp_u1_claim_l1.json()["assistant_hiring_approval"] == "approved"

    # Verify User 1 state after claiming L1
    credits_u1_after_l1 = await get_credits(client, user_headers=user1_headers)
    assert credits_u1_after_l1 == initial_credits_u1 + float(
        settings.assistant_creation_cost,
    )
    user1_details_after_l1 = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user1_id}",
        headers=ADMIN_HEADERS,
    )
    assert user1_details_after_l1.json()["assistant_hiring_approval"] == "approved"
    assert user1_details_after_l1.json()["has_claimed_approval_link"] is True

    # --- User 1: Tries to claim SAME Link L1 again ---
    resp_u1_reclaim_l1 = await client.post(
        "/v0/user/claim-assistant-hiring-one-time-link",
        json={"token": l1_token},
        headers=user1_headers,
    )
    assert resp_u1_reclaim_l1.status_code == status.HTTP_200_OK
    assert "already benefited" in resp_u1_reclaim_l1.json()["message"]
    assert resp_u1_reclaim_l1.json()["assistant_hiring_approval"] == "approved"

    credits_u1_after_reclaim_l1 = await get_credits(client, user_headers=user1_headers)
    assert credits_u1_after_reclaim_l1 == credits_u1_after_l1

    # --- User 1: Tries to claim a NEW, fresh Link L2 ---
    # Admin creates Link L2
    resp_l2 = await client.post(
        "/v0/admin/assistant-hiring-one-time-link",
        json={"expires_in_days": 1},
        headers=ADMIN_HEADERS,
    )
    assert resp_l2.status_code == status.HTTP_201_CREATED
    l2_token = resp_l2.json()["token"]
    l2_id = resp_l2.json()["id"]

    resp_u1_claim_l2 = await client.post(
        "/v0/user/claim-assistant-hiring-one-time-link",
        json={"token": l2_token},
        headers=user1_headers,
    )
    assert resp_u1_claim_l2.status_code == status.HTTP_200_OK
    assert "already benefited" in resp_u1_claim_l2.json()["message"]
    assert "This link was not consumed" in resp_u1_claim_l2.json()["message"]
    assert resp_u1_claim_l2.json()["assistant_hiring_approval"] == "approved"

    # Verify User 1 credits did not change, Link L2 is still unclaimed
    credits_u1_after_l2_attempt = await get_credits(client, user_headers=user1_headers)
    assert credits_u1_after_l2_attempt == credits_u1_after_l1

    link_l2_details = await client.get(
        f"/v0/admin/assistant-hiring-one-time-link",
        headers=ADMIN_HEADERS,
    )  # List all links
    l2_from_list = next(
        (link for link in link_l2_details.json() if link["id"] == l2_id),
        None,
    )
    assert l2_from_list is not None
    assert l2_from_list["user_id"] is None

    # --- Admin revokes User 1's approval ---
    await client.put(
        f"/v0/admin/auth-user/{user1_id}/assistant-hiring-approval/revoked",
        headers=ADMIN_HEADERS,
    )
    user1_details_revoked = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user1_id}",
        headers=ADMIN_HEADERS,
    )
    assert user1_details_revoked.json()["assistant_hiring_approval"] == "revoked"
    assert user1_details_revoked.json()["has_claimed_approval_link"] is True

    # --- User 1 (revoked, but has_claimed=True): Tries to claim NEW Link L2 again ---
    resp_u1_claim_l2_after_revoke = await client.post(
        "/v0/user/claim-assistant-hiring-one-time-link",
        json={"token": l2_token},
        headers=user1_headers,
    )
    assert resp_u1_claim_l2_after_revoke.status_code == status.HTTP_200_OK
    assert "has been re-activated" in resp_u1_claim_l2_after_revoke.json()["message"]
    assert (
        resp_u1_claim_l2_after_revoke.json()["assistant_hiring_approval"] == "approved"
    )

    # Verify User 1 credits did NOT change, Link L2 STILL unclaimed
    credits_u1_after_l2_revoke_attempt = await get_credits(
        client,
        user_headers=user1_headers,
    )
    assert credits_u1_after_l2_revoke_attempt == credits_u1_after_l1
    user1_details_after_l2_revoke_claim = await client.get(
        f"/v0/admin/auth-user/by-user-id?user_id={user1_id}",
        headers=ADMIN_HEADERS,
    )
    assert (
        user1_details_after_l2_revoke_claim.json()["assistant_hiring_approval"]
        == "approved"
    )

    link_l2_details_after = await client.get(
        f"/v0/admin/assistant-hiring-one-time-link",
        headers=ADMIN_HEADERS,
    )
    l2_from_list_after = next(
        (link for link in link_l2_details_after.json() if link["id"] == l2_id),
        None,
    )
    assert l2_from_list_after is not None
    assert l2_from_list_after["user_id"] is None


@pytest.mark.anyio
async def test_claim_invalid_one_time_link(client: AsyncClient):
    test_user_invalid = await create_test_user(client, "invalid_link_user@example.com")
    user_headers = test_user_invalid["headers"]

    # Try to claim a non-existent token
    response_non_existent = await client.post(
        "/v0/user/claim-assistant-hiring-one-time-link",
        json={"token": "this-token-does-not-exist"},
        headers=user_headers,
    )
    assert response_non_existent.status_code == status.HTTP_404_NOT_FOUND

    # Create a link and have another user claim it
    user_A = await create_test_user(client, "user_A_claims@example.com")
    user_B = await create_test_user(client, "user_B_tries@example.com")

    create_link_resp = await client.post(
        "/v0/admin/assistant-hiring-one-time-link",
        json={"expires_in_days": 1},
        headers=ADMIN_HEADERS,
    )
    link_token_for_A = create_link_resp.json()["token"]

    # Get User B's initial credits before User A claims and User B attempts to claim
    user_B_initial_credits = await get_credits(client, user_headers=user_B["headers"])

    # User A claims it
    await client.post(
        "/v0/user/claim-assistant-hiring-one-time-link",
        json={"token": link_token_for_A},
        headers=user_A["headers"],
    )

    # User B tries to claim the same token
    response_user_B_claim = await client.post(
        "/v0/user/claim-assistant-hiring-one-time-link",
        json={"token": link_token_for_A},
        headers=user_B["headers"],
    )
    assert response_user_B_claim.status_code == status.HTTP_400_BAD_REQUEST
    assert (
        "already been claimed by another user" in response_user_B_claim.json()["detail"]
    )

    # Verify User B's credits did not change
    user_B_credits_after_failed_attempt = await get_credits(
        client,
        user_headers=user_B["headers"],
    )
    assert (
        user_B_credits_after_failed_attempt == user_B_initial_credits
    ), "User B's credits should not change after a failed claim attempt on an already claimed link."
