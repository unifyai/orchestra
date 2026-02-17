"""
Tests for one-time credit grant links.

Credit grant links provide a way to give users initial credits.
"""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.settings import settings
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user, get_credits


@pytest.mark.anyio
async def test_one_time_credit_grant_links_flow(client: AsyncClient):
    """Test the one-time credit grant link flow."""
    test_user_link = await create_test_user(client, "link_user@example.com")
    user_id = test_user_link["id"]
    user_headers = test_user_link["headers"]
    initial_credits = await get_credits(client, user_headers=user_headers)

    # Admin creates a one-time link with default credit amount
    create_link_payload = {"expires_in_days": 1}
    response_create_link = await client.post(
        "/v0/admin/credit-grant-link",
        json=create_link_payload,
        headers=ADMIN_HEADERS,
    )
    assert (
        response_create_link.status_code == status.HTTP_201_CREATED
    ), response_create_link.json()
    link_data = response_create_link.json()
    assert "token" in link_data
    assert "credit_amount" in link_data
    assert link_data["credit_amount"] == float(settings.assistant_creation_cost)
    one_time_token = link_data["token"]
    link_id_db = link_data["id"]

    # User claims the link
    claim_payload = {"token": one_time_token}
    response_claim = await client.post(
        "/v0/user/claim-credit-grant-link",
        json=claim_payload,
        headers=user_headers,
    )
    assert response_claim.status_code == status.HTTP_200_OK, response_claim.json()
    claim_data = response_claim.json()
    # New message format
    assert "Link successfully claimed" in claim_data["message"]
    assert "credits awarded" in claim_data["message"]
    # credits_granted is now returned
    assert claim_data["credits_granted"] == float(settings.assistant_creation_cost)

    # Verify credits were granted
    credits_after_first_claim = await get_credits(client, user_headers=user_headers)
    expected_credits_after_first_claim = initial_credits + float(
        settings.assistant_creation_cost,
    )
    assert (
        credits_after_first_claim == expected_credits_after_first_claim
    ), f"Credits mismatch: expected {expected_credits_after_first_claim}, got {credits_after_first_claim}"

    # Try to claim again (by same user) - should indicate already benefited
    response_claim_again = await client.post(
        "/v0/user/claim-credit-grant-link",
        json=claim_payload,
        headers=user_headers,
    )
    assert response_claim_again.status_code == status.HTTP_200_OK
    assert "already benefited" in response_claim_again.json()["message"]
    assert response_claim_again.json()["credits_granted"] is None

    # Verify credits did not change after second claim attempt
    credits_after_second_claim_attempt = await get_credits(
        client,
        user_headers=user_headers,
    )
    assert (
        credits_after_second_claim_attempt == credits_after_first_claim
    ), "Credits should not change on subsequent claims."

    # Admin lists links, check if claimed
    response_list_links = await client.get(
        "/v0/admin/credit-grant-link",
        headers=ADMIN_HEADERS,
    )
    assert response_list_links.status_code == status.HTTP_200_OK
    links = response_list_links.json()
    claimed_link_in_list = next(
        (link for link in links if link["token"] == one_time_token),
        None,
    )
    assert claimed_link_in_list is not None
    assert claimed_link_in_list["user_id"] == user_id
    assert claimed_link_in_list["claimed_at"] is not None
    assert claimed_link_in_list["credit_amount"] == float(
        settings.assistant_creation_cost,
    )

    # Admin deletes the link
    response_delete_link = await client.delete(
        f"/v0/admin/credit-grant-link/{link_id_db}",
        headers=ADMIN_HEADERS,
    )
    assert response_delete_link.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.anyio
async def test_custom_credit_amount_link(client: AsyncClient):
    """Test creating a link with a custom credit amount."""
    test_user = await create_test_user(client, "custom_credits_user@example.com")
    user_headers = test_user["headers"]
    initial_credits = await get_credits(client, user_headers=user_headers)

    # Admin creates a link with custom credit amount
    custom_amount = 25.0
    response_create_link = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": custom_amount},
        headers=ADMIN_HEADERS,
    )
    assert response_create_link.status_code == status.HTTP_201_CREATED
    link_data = response_create_link.json()
    assert link_data["credit_amount"] == custom_amount

    # User claims the link
    response_claim = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link_data["token"]},
        headers=user_headers,
    )
    assert response_claim.status_code == status.HTTP_200_OK
    claim_data = response_claim.json()
    assert claim_data["credits_granted"] == custom_amount

    # Verify credits increased by custom amount
    credits_after = await get_credits(client, user_headers=user_headers)
    assert credits_after == initial_credits + custom_amount


@pytest.mark.anyio
async def test_one_time_link_single_benefit_only(client: AsyncClient):
    """
    Test that users can only benefit from one credit grant link ever.

    This tests the core behavior:
    - First claim grants credits
    - Subsequent claims (same or different link) do NOT grant credits
    - Links are not consumed when user has already benefited
    """
    user1 = await create_test_user(client, "link_benefiter@example.com")
    user1_id = user1["id"]
    user1_headers = user1["headers"]

    # Get initial state for user1
    initial_credits_u1 = await get_credits(client, user_headers=user1_headers)
    user1_details_before = await client.get(
        f"/v0/admin/user/by-user-id?user_id={user1_id}",
        headers=ADMIN_HEADERS,
    )
    assert (
        user1_details_before.json()["has_claimed_credit_grant_link"] is False
    )  # derived from DB query

    # Admin creates Link L1
    resp_l1 = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1},
        headers=ADMIN_HEADERS,
    )
    assert resp_l1.status_code == status.HTTP_201_CREATED
    l1_token = resp_l1.json()["token"]

    # User 1 claims Link L1 - should get credits
    resp_u1_claim_l1 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": l1_token},
        headers=user1_headers,
    )
    assert resp_u1_claim_l1.status_code == status.HTTP_200_OK
    assert "credits awarded" in resp_u1_claim_l1.json()["message"]
    assert resp_u1_claim_l1.json()["credits_granted"] == float(
        settings.assistant_creation_cost,
    )

    # Verify User 1 received credits and claimed status is derived from DB
    credits_u1_after_l1 = await get_credits(client, user_headers=user1_headers)
    assert credits_u1_after_l1 == initial_credits_u1 + float(
        settings.assistant_creation_cost,
    )
    user1_details_after_l1 = await client.get(
        f"/v0/admin/user/by-user-id?user_id={user1_id}",
        headers=ADMIN_HEADERS,
    )
    assert user1_details_after_l1.json()["has_claimed_credit_grant_link"] is True

    # --- User 1: Tries to claim SAME Link L1 again ---
    resp_u1_reclaim_l1 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": l1_token},
        headers=user1_headers,
    )
    assert resp_u1_reclaim_l1.status_code == status.HTTP_200_OK
    assert "already benefited" in resp_u1_reclaim_l1.json()["message"]
    assert resp_u1_reclaim_l1.json()["credits_granted"] is None

    # Credits should not change
    credits_u1_after_reclaim = await get_credits(client, user_headers=user1_headers)
    assert credits_u1_after_reclaim == credits_u1_after_l1

    # --- User 1: Tries to claim a NEW Link L2 ---
    resp_l2 = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1},
        headers=ADMIN_HEADERS,
    )
    assert resp_l2.status_code == status.HTTP_201_CREATED
    l2_token = resp_l2.json()["token"]
    l2_id = resp_l2.json()["id"]

    resp_u1_claim_l2 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": l2_token},
        headers=user1_headers,
    )
    assert resp_u1_claim_l2.status_code == status.HTTP_200_OK
    assert "already benefited" in resp_u1_claim_l2.json()["message"]
    assert resp_u1_claim_l2.json()["credits_granted"] is None

    # Verify User 1 credits did not change
    credits_u1_after_l2_attempt = await get_credits(client, user_headers=user1_headers)
    assert credits_u1_after_l2_attempt == credits_u1_after_l1

    # Verify Link L2 is still unclaimed (not consumed)
    link_l2_details = await client.get(
        "/v0/admin/credit-grant-link",
        headers=ADMIN_HEADERS,
    )
    l2_from_list = next(
        (link for link in link_l2_details.json() if link["id"] == l2_id),
        None,
    )
    assert l2_from_list is not None
    assert l2_from_list["user_id"] is None  # Not claimed


@pytest.mark.anyio
async def test_claim_invalid_one_time_link(client: AsyncClient):
    """Test error handling for invalid link claims."""
    test_user_invalid = await create_test_user(client, "invalid_link_user@example.com")
    user_headers = test_user_invalid["headers"]

    # Try to claim a non-existent token
    response_non_existent = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": "this-token-does-not-exist"},
        headers=user_headers,
    )
    assert response_non_existent.status_code == status.HTTP_404_NOT_FOUND

    # Create a link and have another user claim it
    user_A = await create_test_user(client, "user_A_claims@example.com")
    user_B = await create_test_user(client, "user_B_tries@example.com")

    create_link_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1},
        headers=ADMIN_HEADERS,
    )
    link_token_for_A = create_link_resp.json()["token"]

    # Get User B's initial credits
    user_B_initial_credits = await get_credits(client, user_headers=user_B["headers"])

    # User A claims the link
    await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link_token_for_A},
        headers=user_A["headers"],
    )

    # User B tries to claim the same token
    response_user_B_claim = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link_token_for_A},
        headers=user_B["headers"],
    )
    assert response_user_B_claim.status_code == status.HTTP_400_BAD_REQUEST
    assert "already been claimed" in response_user_B_claim.json()["detail"]

    # Verify User B's credits did not change
    user_B_credits_after_failed_attempt = await get_credits(
        client,
        user_headers=user_B["headers"],
    )
    assert user_B_credits_after_failed_attempt == user_B_initial_credits
