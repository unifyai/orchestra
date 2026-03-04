"""
Tests for one-time credit grant links.

Credit grant links provide a way to give users initial credits.
Links can be claimed for personal accounts (personal API key) or
for organization accounts (org API key).
"""

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.settings import settings
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user, get_credits


# ---------------------------------------------------------------------------
# Helper: create an org and return org headers + org_id
# ---------------------------------------------------------------------------
async def _create_org(
    client: AsyncClient,
    owner_headers: dict,
    org_name: str,
) -> dict:
    """Create an org and return { org_id, org_api_key, org_headers }."""
    resp = await client.post(
        "/v0/organizations",
        json={"name": org_name},
        headers=owner_headers,
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.json()
    data = resp.json()
    org_api_key = data["api_key"]
    return {
        "org_id": data["id"],
        "org_api_key": org_api_key,
        "org_headers": {
            "accept": "application/json",
            "Authorization": f"Bearer {org_api_key}",
        },
    }


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


# ===========================================================================
# Organization Credit Grant Tests
# ===========================================================================


@pytest.mark.anyio
async def test_claim_link_with_org_api_key_credits_org(client: AsyncClient, dbsession):
    """
    When a user claims a link using an org API key, credits go to the
    organization's billing account (not the user's personal account).
    """

    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, "org_claim_owner@example.com")
    org = await _create_org(client, owner["headers"], "CreditTestOrg")

    # Read org billing account credits directly from DB
    org_row = dbsession.query(Organization).filter_by(id=org["org_id"]).first()
    org_credits_before = float(org_row.billing_account.credits)

    # Get personal credits before
    personal_credits_before = await get_credits(client, user_headers=owner["headers"])

    # Admin creates link
    link_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": 50.0},
        headers=ADMIN_HEADERS,
    )
    assert link_resp.status_code == status.HTTP_201_CREATED
    token = link_resp.json()["token"]

    # Claim using ORG API key
    claim_resp = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=org["org_headers"],
    )
    assert claim_resp.status_code == 200
    claim_data = claim_resp.json()
    assert claim_data["credits_granted"] == 50.0
    assert claim_data["credited_to"] == "CreditTestOrg"

    # Org credits should have increased (re-read from DB)
    dbsession.expire_all()
    org_row = dbsession.query(Organization).filter_by(id=org["org_id"]).first()
    org_credits_after = float(org_row.billing_account.credits)
    assert org_credits_after == org_credits_before + 50.0

    # Personal credits should be unchanged
    personal_credits_after = await get_credits(client, user_headers=owner["headers"])
    assert personal_credits_after == personal_credits_before


@pytest.mark.anyio
async def test_claim_link_personal_credits_personal(client: AsyncClient):
    """
    When a user claims a link using a personal API key, credits go to
    their personal billing account and credited_to is 'personal'.
    """
    user = await create_test_user(client, "personal_claim_user@example.com")
    credits_before = await get_credits(client, user_headers=user["headers"])

    link_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": 30.0},
        headers=ADMIN_HEADERS,
    )
    token = link_resp.json()["token"]

    claim_resp = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user["headers"],
    )
    assert claim_resp.status_code == 200
    claim_data = claim_resp.json()
    assert claim_data["credits_granted"] == 30.0
    assert claim_data["credited_to"] == "personal"

    credits_after = await get_credits(client, user_headers=user["headers"])
    assert credits_after == credits_before + 30.0


@pytest.mark.anyio
async def test_per_user_guard_blocks_org_claim_after_personal(client: AsyncClient):
    """
    A user who already claimed a link (personal) cannot claim another
    link for their org — the per-user guard fires first.
    """
    owner = await create_test_user(client, "guard_user_then_org@example.com")
    org = await _create_org(client, owner["headers"], "GuardTestOrg")

    # Create two links
    link1_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": 10.0},
        headers=ADMIN_HEADERS,
    )
    link2_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": 10.0},
        headers=ADMIN_HEADERS,
    )

    # Claim link1 personally
    claim1 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link1_resp.json()["token"]},
        headers=owner["headers"],
    )
    assert claim1.status_code == 200
    assert claim1.json()["credits_granted"] == 10.0

    # Try to claim link2 with org key — blocked by per-user guard
    claim2 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link2_resp.json()["token"]},
        headers=org["org_headers"],
    )
    assert claim2.status_code == 200
    assert "already benefited" in claim2.json()["message"]
    assert claim2.json()["credits_granted"] is None


@pytest.mark.anyio
async def test_per_org_guard_blocks_second_org_claim(client: AsyncClient):
    """
    If an org already benefited from a link (claimed by member A),
    member B cannot claim another link for the same org.

    We use two separate owners — each creates their own org with the same
    name to simplify the test — but really what matters is: once owner
    claims for org, the *same* org cannot benefit twice, even via a
    different user.

    Since inviting a second member and getting their org key requires email
    infrastructure, we simplify: the same owner tries claiming a second link
    for the same org.
    """
    owner = await create_test_user(client, "org_guard_owner@example.com")
    org = await _create_org(client, owner["headers"], "OrgGuardTest")

    # Create two links
    link1_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": 20.0},
        headers=ADMIN_HEADERS,
    )
    link2_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": 20.0},
        headers=ADMIN_HEADERS,
    )

    # Owner claims link1 for org
    claim1 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link1_resp.json()["token"]},
        headers=org["org_headers"],
    )
    assert claim1.status_code == 200
    assert claim1.json()["credits_granted"] == 20.0

    # Same owner tries to claim link2 for the same org — blocked by both
    # per-user and per-org guards (per-user fires first)
    claim2 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link2_resp.json()["token"]},
        headers=org["org_headers"],
    )
    assert claim2.status_code == 200
    assert "already benefited" in claim2.json()["message"]
    assert claim2.json()["credits_granted"] is None

    # Also verify that a DIFFERENT user/owner trying to claim for a
    # NEW org works (the per-org guard should NOT block them)
    owner2 = await create_test_user(client, "org_guard_owner2@example.com")
    org2 = await _create_org(client, owner2["headers"], "OrgGuardTest2")
    claim3 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link2_resp.json()["token"]},
        headers=org2["org_headers"],
    )
    assert claim3.status_code == 200
    assert claim3.json()["credits_granted"] == 20.0
    assert claim3.json()["credited_to"] == "OrgGuardTest2"


@pytest.mark.anyio
async def test_list_links_shows_org_info(client: AsyncClient):
    """
    Admin list endpoint shows organization_id and claimed_for_org for
    links claimed with an org API key.
    """
    owner = await create_test_user(client, "list_org_owner@example.com")
    org = await _create_org(client, owner["headers"], "ListOrgInfo")

    link_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": 5.0},
        headers=ADMIN_HEADERS,
    )
    token = link_resp.json()["token"]
    link_id = link_resp.json()["id"]

    # Claim with org key
    await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=org["org_headers"],
    )

    # List and find our link
    list_resp = await client.get("/v0/admin/credit-grant-link", headers=ADMIN_HEADERS)
    assert list_resp.status_code == 200
    found = next((l for l in list_resp.json() if l["id"] == link_id), None)
    assert found is not None
    assert found["organization_id"] == org["org_id"]
    assert found["claimed_for_org"] == "ListOrgInfo"
    assert found["user_id"] is not None  # user who claimed it is always recorded
