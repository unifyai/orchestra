"""
Tests for credit grant links.

Credit grant links provide a way to give users initial credits.
Links can be single-use (max_claims=1, the default) or multi-use
(max_claims>1) so they can be shared publicly.

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


# ===========================================================================
# Single-use link tests (backward compat with max_claims=1)
# ===========================================================================


@pytest.mark.anyio
async def test_single_use_credit_grant_links_flow(client: AsyncClient):
    """Test the default single-use credit grant link flow."""
    test_user_link = await create_test_user(client, "link_user@example.com")
    user_id = test_user_link["id"]
    user_headers = test_user_link["headers"]
    initial_credits = await get_credits(client, user_headers=user_headers)

    # Admin creates a single-use link (default max_claims=1)
    response_create_link = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1},
        headers=ADMIN_HEADERS,
    )
    assert (
        response_create_link.status_code == status.HTTP_201_CREATED
    ), response_create_link.json()
    link_data = response_create_link.json()
    assert "token" in link_data
    assert "credit_amount" in link_data
    assert link_data["credit_amount"] == float(settings.assistant_creation_cost)
    assert link_data["max_claims"] == 1
    assert link_data["claim_count"] == 0
    one_time_token = link_data["token"]
    link_id_db = link_data["id"]

    # User claims the link
    response_claim = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": one_time_token},
        headers=user_headers,
    )
    assert response_claim.status_code == status.HTTP_200_OK, response_claim.json()
    claim_data = response_claim.json()
    assert "Link successfully claimed" in claim_data["message"]
    assert "credits awarded" in claim_data["message"]
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
        json={"token": one_time_token},
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

    # Admin lists links, check claim info
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
    assert claimed_link_in_list["claim_count"] == 1
    assert claimed_link_in_list["max_claims"] == 1
    assert len(claimed_link_in_list["claims"]) == 1
    assert claimed_link_in_list["claims"][0]["user_id"] == user_id
    assert claimed_link_in_list["claims"][0]["claimed_at"] is not None

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

    custom_amount = 25.0
    response_create_link = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": custom_amount},
        headers=ADMIN_HEADERS,
    )
    assert response_create_link.status_code == status.HTTP_201_CREATED
    link_data = response_create_link.json()
    assert link_data["credit_amount"] == custom_amount

    response_claim = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link_data["token"]},
        headers=user_headers,
    )
    assert response_claim.status_code == status.HTTP_200_OK
    claim_data = response_claim.json()
    assert claim_data["credits_granted"] == custom_amount

    credits_after = await get_credits(client, user_headers=user_headers)
    assert credits_after == initial_credits + custom_amount


@pytest.mark.anyio
async def test_single_use_link_single_benefit_only(client: AsyncClient):
    """
    Test that users can only benefit from one credit grant link ever.

    - First claim grants credits
    - Subsequent claims (same or different link) do NOT grant credits
    - Links are not consumed when user has already benefited
    """
    user1 = await create_test_user(client, "link_benefiter@example.com")
    user1_id = user1["id"]
    user1_headers = user1["headers"]

    initial_credits_u1 = await get_credits(client, user_headers=user1_headers)
    user1_details_before = await client.get(
        f"/v0/admin/user/by-user-id?user_id={user1_id}",
        headers=ADMIN_HEADERS,
    )
    assert user1_details_before.json()["has_claimed_credit_grant_link"] is False

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
    assert l2_from_list["claim_count"] == 0


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

    # Create a single-use link and have user A claim it; user B should be blocked
    user_A = await create_test_user(client, "user_A_claims@example.com")
    user_B = await create_test_user(client, "user_B_tries@example.com")

    create_link_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1},
        headers=ADMIN_HEADERS,
    )
    link_token_for_A = create_link_resp.json()["token"]

    user_B_initial_credits = await get_credits(client, user_headers=user_B["headers"])

    # User A claims the link
    await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link_token_for_A},
        headers=user_A["headers"],
    )

    # User B tries to claim the same single-use token → fully redeemed
    response_user_B_claim = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link_token_for_A},
        headers=user_B["headers"],
    )
    assert response_user_B_claim.status_code == status.HTTP_400_BAD_REQUEST
    assert "redemption limit" in response_user_B_claim.json()["detail"]

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

    org_row = dbsession.query(Organization).filter_by(id=org["org_id"]).first()
    org_credits_before = float(org_row.billing_account.credits)

    personal_credits_before = await get_credits(client, user_headers=owner["headers"])

    link_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": 50.0},
        headers=ADMIN_HEADERS,
    )
    assert link_resp.status_code == status.HTTP_201_CREATED
    token = link_resp.json()["token"]

    claim_resp = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=org["org_headers"],
    )
    assert claim_resp.status_code == 200
    claim_data = claim_resp.json()
    assert claim_data["credits_granted"] == 50.0
    assert claim_data["credited_to"] == "CreditTestOrg"

    dbsession.expire_all()
    org_row = dbsession.query(Organization).filter_by(id=org["org_id"]).first()
    org_credits_after = float(org_row.billing_account.credits)
    assert org_credits_after == org_credits_before + 50.0

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

    claim1 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link1_resp.json()["token"]},
        headers=owner["headers"],
    )
    assert claim1.status_code == 200
    assert claim1.json()["credits_granted"] == 10.0

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
    """
    owner = await create_test_user(client, "org_guard_owner@example.com")
    org = await _create_org(client, owner["headers"], "OrgGuardTest")

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

    claim1 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link1_resp.json()["token"]},
        headers=org["org_headers"],
    )
    assert claim1.status_code == 200
    assert claim1.json()["credits_granted"] == 20.0

    claim2 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link2_resp.json()["token"]},
        headers=org["org_headers"],
    )
    assert claim2.status_code == 200
    assert "already benefited" in claim2.json()["message"]
    assert claim2.json()["credits_granted"] is None

    # A DIFFERENT user/owner claiming for a NEW org should work
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

    await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=org["org_headers"],
    )

    list_resp = await client.get("/v0/admin/credit-grant-link", headers=ADMIN_HEADERS)
    assert list_resp.status_code == 200
    found = next((l for l in list_resp.json() if l["id"] == link_id), None)
    assert found is not None
    assert found["claim_count"] == 1
    assert len(found["claims"]) == 1
    assert found["claims"][0]["organization_id"] == org["org_id"]
    assert found["claims"][0]["claimed_for_org"] == "ListOrgInfo"
    assert found["claims"][0]["user_id"] is not None


# ===========================================================================
# Multi-claim link tests
# ===========================================================================


@pytest.mark.anyio
async def test_multi_claim_link_multiple_users_can_claim(client: AsyncClient):
    """
    A link with max_claims > 1 can be claimed by multiple distinct users.
    """
    user_a = await create_test_user(client, "multi_a@example.com")
    user_b = await create_test_user(client, "multi_b@example.com")
    user_c = await create_test_user(client, "multi_c@example.com")

    credits_a_before = await get_credits(client, user_headers=user_a["headers"])
    credits_b_before = await get_credits(client, user_headers=user_b["headers"])
    credits_c_before = await get_credits(client, user_headers=user_c["headers"])

    # Admin creates a multi-claim link
    link_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 7, "credit_amount": 15.0, "max_claims": 3},
        headers=ADMIN_HEADERS,
    )
    assert link_resp.status_code == status.HTTP_201_CREATED
    link_data = link_resp.json()
    assert link_data["max_claims"] == 3
    assert link_data["claim_count"] == 0
    token = link_data["token"]

    # User A claims
    resp_a = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user_a["headers"],
    )
    assert resp_a.status_code == 200
    assert resp_a.json()["credits_granted"] == 15.0

    # User B claims the same link
    resp_b = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user_b["headers"],
    )
    assert resp_b.status_code == 200
    assert resp_b.json()["credits_granted"] == 15.0

    # User C claims the same link
    resp_c = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user_c["headers"],
    )
    assert resp_c.status_code == 200
    assert resp_c.json()["credits_granted"] == 15.0

    # Verify credits for all three
    assert (
        await get_credits(client, user_headers=user_a["headers"])
        == credits_a_before + 15.0
    )
    assert (
        await get_credits(client, user_headers=user_b["headers"])
        == credits_b_before + 15.0
    )
    assert (
        await get_credits(client, user_headers=user_c["headers"])
        == credits_c_before + 15.0
    )

    # Admin list should show 3 claims
    list_resp = await client.get("/v0/admin/credit-grant-link", headers=ADMIN_HEADERS)
    found = next((l for l in list_resp.json() if l["token"] == token), None)
    assert found is not None
    assert found["claim_count"] == 3
    assert found["max_claims"] == 3
    assert len(found["claims"]) == 3


@pytest.mark.anyio
async def test_multi_claim_link_blocks_after_budget_exhausted(client: AsyncClient):
    """
    Once a multi-claim link reaches max_claims, further claims are rejected.
    """
    user_a = await create_test_user(client, "budget_a@example.com")
    user_b = await create_test_user(client, "budget_b@example.com")
    user_c = await create_test_user(client, "budget_c@example.com")

    link_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 7, "credit_amount": 10.0, "max_claims": 2},
        headers=ADMIN_HEADERS,
    )
    token = link_resp.json()["token"]

    # User A & B claim successfully
    await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user_a["headers"],
    )
    await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user_b["headers"],
    )

    # User C should be rejected — redemption limit reached
    credits_c_before = await get_credits(client, user_headers=user_c["headers"])
    resp_c = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user_c["headers"],
    )
    assert resp_c.status_code == status.HTTP_400_BAD_REQUEST
    assert "redemption limit" in resp_c.json()["detail"]
    assert await get_credits(client, user_headers=user_c["headers"]) == credits_c_before


@pytest.mark.anyio
async def test_multi_claim_link_per_user_lifetime_guard(client: AsyncClient):
    """
    Even with a multi-claim link, the per-user lifetime guard still
    prevents a user who already benefited from any link from claiming again.
    """
    user = await create_test_user(client, "multi_lifetime@example.com")

    # User claims a single-use link first
    link1_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "credit_amount": 5.0},
        headers=ADMIN_HEADERS,
    )
    await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": link1_resp.json()["token"]},
        headers=user["headers"],
    )

    # Now create a multi-claim link
    link2_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 7, "credit_amount": 50.0, "max_claims": 100},
        headers=ADMIN_HEADERS,
    )
    token2 = link2_resp.json()["token"]

    # User tries the multi-claim link — blocked by per-user lifetime guard
    resp = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token2},
        headers=user["headers"],
    )
    assert resp.status_code == 200
    assert "already benefited" in resp.json()["message"]
    assert resp.json()["credits_granted"] is None


@pytest.mark.anyio
async def test_multi_claim_link_same_user_cannot_double_claim(client: AsyncClient):
    """
    A user cannot claim the same multi-claim link more than once.
    The per-user lifetime guard catches this before we even check the
    per-link budget.
    """
    user = await create_test_user(client, "multi_double@example.com")

    link_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 7, "credit_amount": 10.0, "max_claims": 5},
        headers=ADMIN_HEADERS,
    )
    token = link_resp.json()["token"]

    # First claim succeeds
    resp1 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user["headers"],
    )
    assert resp1.status_code == 200
    assert resp1.json()["credits_granted"] == 10.0

    # Second claim by same user — blocked by per-user lifetime guard
    resp2 = await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user["headers"],
    )
    assert resp2.status_code == 200
    assert "already benefited" in resp2.json()["message"]
    assert resp2.json()["credits_granted"] is None


@pytest.mark.anyio
async def test_create_link_validates_max_claims(client: AsyncClient):
    """max_claims must be at least 1."""
    resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 1, "max_claims": 0},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "max_claims" in resp.json()["detail"]


@pytest.mark.anyio
async def test_create_link_with_name(client: AsyncClient):
    """Links can have an optional admin-facing name for identification."""
    resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 7, "name": "Twitter campaign Q2"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    assert data["name"] == "Twitter campaign Q2"

    # Name appears in admin list
    list_resp = await client.get("/v0/admin/credit-grant-link", headers=ADMIN_HEADERS)
    found = next((l for l in list_resp.json() if l["id"] == data["id"]), None)
    assert found is not None
    assert found["name"] == "Twitter campaign Q2"

    # Links without a name default to null
    resp2 = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 7},
        headers=ADMIN_HEADERS,
    )
    assert resp2.status_code == status.HTTP_201_CREATED
    assert resp2.json()["name"] is None


@pytest.mark.anyio
async def test_multi_claim_link_admin_list_shows_all_claims(client: AsyncClient):
    """
    The admin list endpoint returns all individual claims with
    claimer details for multi-claim links.
    """
    user_a = await create_test_user(client, "list_multi_a@example.com")
    user_b = await create_test_user(client, "list_multi_b@example.com")

    link_resp = await client.post(
        "/v0/admin/credit-grant-link",
        json={"expires_in_days": 7, "credit_amount": 10.0, "max_claims": 5},
        headers=ADMIN_HEADERS,
    )
    token = link_resp.json()["token"]
    link_id = link_resp.json()["id"]

    await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user_a["headers"],
    )
    await client.post(
        "/v0/user/claim-credit-grant-link",
        json={"token": token},
        headers=user_b["headers"],
    )

    list_resp = await client.get("/v0/admin/credit-grant-link", headers=ADMIN_HEADERS)
    found = next((l for l in list_resp.json() if l["id"] == link_id), None)
    assert found is not None
    assert found["claim_count"] == 2
    assert found["max_claims"] == 5
    assert len(found["claims"]) == 2

    claim_emails = {c["claimed_by_email"] for c in found["claims"]}
    assert "list_multi_a@example.com" in claim_emails
    assert "list_multi_b@example.com" in claim_emails
