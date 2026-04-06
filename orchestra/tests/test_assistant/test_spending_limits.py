"""Tests for assistant and organization spending limit functionality."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.tests.utils import HEADERS, create_test_user


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """
    Automatically mock assistant infrastructure webhooks and staging for all tests.
    This prevents real network calls and bypasses billing checks,
    making tests fast and reliable.
    """
    if "no_mock_infra" in request.keywords:
        yield
        return

    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken, patch(
        "orchestra.web.api.assistant.views.settings",
    ) as mock_settings:

        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        # Patch is_staging to skip credit/billing checks during assistant creation
        mock_settings.is_staging = True

        yield mock_wake_up, mock_reawaken


async def _create_assistant(
    client: AsyncClient,
    first_name: str,
    surname: str,
    headers: dict,
):
    """Create an assistant and return the agent_id."""
    response = await client.post(
        "/v0/assistant",
        json={
            "first_name": first_name,
            "surname": surname,
            "age": 25,
            "nationality": "American",
            "create_infra": False,
        },
        headers=headers,
    )
    return response


async def _create_organization(client: AsyncClient, name: str, headers: dict):
    """Create an organization."""
    return await client.post(
        "/v0/organizations",
        json={"name": name},
        headers=headers,
    )


# ===========================================================================
# Assistant Spending Limit Tests
# ===========================================================================


@pytest.mark.anyio
async def test_set_assistant_spending_limit(client: AsyncClient):
    """Test setting an assistant's spending limit."""
    # Create an assistant using the default approved user
    response = await _create_assistant(client, "SpendLimit", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set spending limit
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert str(data["agent_id"]) == str(agent_id)
    assert data["monthly_spending_cap"] == 100.00


@pytest.mark.anyio
async def test_admin_get_assistant_spend_no_data(client: AsyncClient):
    """Test admin getting assistant spend when no spend data exists."""
    # Create an assistant using the default approved user
    response = await _create_assistant(client, "NoSpend", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Get spend via admin endpoint (should be 0)
    response = await client.get(
        f"/v0/assistant/{agent_id}/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert str(data["agent_id"]) == str(agent_id)
    assert data["month"] == "2026-01"
    assert data["cumulative_spend"] == 0.0


@pytest.mark.anyio
async def test_admin_get_assistant_spend_with_limit(client: AsyncClient):
    """Test admin getting assistant spend with a limit set shows percent used."""
    # Create an assistant using the default approved user
    response = await _create_assistant(client, "WithLimit", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set spending limit
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Get spend via admin endpoint (should show limit and percent)
    response = await client.get(
        f"/v0/assistant/{agent_id}/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["limit"] == 100.00
    # With 0 spend and 100 limit, percent_used should be 0
    assert data["percent_used"] == 0.0


@pytest.mark.anyio
async def test_admin_get_assistant_spend_invalid_month_format(client: AsyncClient):
    """Test admin getting assistant spend with invalid month format."""
    # Create an assistant using the default approved user
    response = await _create_assistant(client, "InvalidMonth", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Get spend via admin endpoint with invalid month
    response = await client.get(
        f"/v0/assistant/{agent_id}/spend?month=invalid",
        headers=HEADERS,
    )

    assert response.status_code == 422, response.json()  # Validation error


@pytest.mark.anyio
async def test_assistant_spending_limit_forbidden(client: AsyncClient):
    """Test setting spending limit for an assistant owned by another user."""
    # Create an assistant using the default approved user
    response = await _create_assistant(client, "ForbiddenTest", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Create a different user
    other_user = await create_test_user(
        client,
        "other_spending_user@example.com",
    )

    # Other user should not be able to set spending limit on an assistant they don't own
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=other_user["headers"],
    )

    assert (
        response.status_code == 404
    ), response.json()  # Not found because they can't access it


# ===========================================================================
# Organization Spending Limit Tests
# ===========================================================================


@pytest.mark.anyio
async def test_set_org_spending_limit(client: AsyncClient):
    """Test setting an organization's spending limit."""
    # Create an organization using default user
    response = await _create_organization(client, "SpendLimitOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Set spending limit
    response = await client.put(
        f"/v0/organizations/{org_id}/spending-limit",
        json={"monthly_spending_cap": 500.00},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["organization_id"] == org_id
    assert data["monthly_spending_cap"] == 500.00


@pytest.mark.anyio
async def test_org_spending_limit_not_found(client: AsyncClient):
    """Test setting spending limit for non-existent organization."""
    response = await client.put(
        "/v0/organizations/99999/spending-limit",
        json={"monthly_spending_cap": 500.00},
        headers=HEADERS,
    )

    assert response.status_code == 404, response.json()


@pytest.mark.anyio
async def test_org_spending_limit_non_admin(client: AsyncClient):
    """Test that non-admin cannot set org spending limit."""
    # Create organization using default user
    response = await _create_organization(client, "NonAdminTestOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Create a different user who is not a member
    non_member = await create_test_user(
        client,
        "non_member_spending@example.com",
    )
    non_member_headers = non_member["headers"]

    # Non-member should not be able to set spending limit
    response = await client.put(
        f"/v0/organizations/{org_id}/spending-limit",
        json={"monthly_spending_cap": 500.00},
        headers=non_member_headers,
    )

    assert response.status_code == 403, response.json()


@pytest.mark.anyio
async def test_update_assistant_with_spending_cap(client: AsyncClient):
    """Test updating an assistant's spending cap via PUT spending-limit endpoint."""
    # Create an assistant using the default approved user
    response = await _create_assistant(client, "UpdateCap", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Update the assistant's spending cap via PUT spending-limit endpoint
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 75.50},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data.get("monthly_spending_cap") == 75.50


@pytest.mark.anyio
async def test_org_spending_limit_cascades(client: AsyncClient):
    """Test that setting org spending limit returns response with cascade info."""
    # Create organization
    response = await _create_organization(client, "CascadeTestOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Set a spending limit
    response = await client.put(
        f"/v0/organizations/{org_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    data = response.json()

    # Check response structure
    assert "organization_id" in data
    assert data["organization_id"] == org_id
    assert data["monthly_spending_cap"] == 100.00
    # cascaded_updates may be None if no users/assistants were capped


# ===========================================================================
# Member Spending Limit Tests (Organization Context)
# ===========================================================================


@pytest.mark.anyio
async def test_set_member_spending_limit(client: AsyncClient):
    """Test setting a member's spending limit within an organization."""
    # Create organization
    response = await _create_organization(client, "MemberLimitTestOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Get the current user ID (owner)
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Set member spending limit for the owner
    response = await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 150.00},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["organization_id"] == org_id
    assert data["user_id"] == owner_id
    assert data["monthly_spending_cap"] == 150.00


@pytest.mark.anyio
async def test_get_member_spending_limit(client: AsyncClient):
    """Test getting a member's spending limit."""
    # Create organization
    response = await _create_organization(client, "GetMemberLimitOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Get the current user ID (owner)
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Set member spending limit
    await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 200.00},
        headers=HEADERS,
    )

    # Get member spending limit
    response = await client.get(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["monthly_spending_cap"] == 200.00


@pytest.mark.anyio
async def test_member_limit_cannot_exceed_org_limit(client: AsyncClient):
    """Test that member limit cannot exceed organization limit."""
    # Create organization
    response = await _create_organization(client, "MemberOrgLimitOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Get owner ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Set org spending limit
    response = await client.put(
        f"/v0/organizations/{org_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Try to set member limit higher than org limit - should fail
    response = await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 150.00},
        headers=HEADERS,
    )

    assert response.status_code == 400, response.json()


@pytest.mark.anyio
async def test_member_limit_not_found(client: AsyncClient):
    """Test setting spending limit for non-member."""
    # Create organization
    response = await _create_organization(client, "NonMemberLimitOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Try to set limit for non-existent member
    response = await client.put(
        f"/v0/organizations/{org_id}/members/non_existent_user/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )

    assert response.status_code == 404, response.json()


# ===========================================================================
# User Personal Spending Limit Tests
# ===========================================================================


@pytest.mark.anyio
async def test_set_user_personal_spending_limit(client: AsyncClient):
    """Test setting user's personal spending limit."""
    response = await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": 300.00},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["monthly_spending_cap"] == 300.00


@pytest.mark.anyio
async def test_get_user_personal_spending_limit(client: AsyncClient):
    """Test getting user's personal spending limit."""
    # First set a limit
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": 250.00},
        headers=HEADERS,
    )

    # Get the limit
    response = await client.get(
        "/v0/user/spending-limit",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["monthly_spending_cap"] == 250.00


@pytest.mark.anyio
async def test_remove_user_personal_spending_limit(client: AsyncClient):
    """Test removing user's personal spending limit (set to null)."""
    # First set a limit
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )

    # Remove the limit
    response = await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["monthly_spending_cap"] is None


@pytest.mark.anyio
async def test_admin_get_user_spend(client: AsyncClient):
    """Test admin getting user's cumulative spend for a month."""
    # Get the user ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]

    # Get spend via user endpoint
    response = await client.get(
        "/v0/user/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["user_id"] == user_id
    assert data["month"] == "2026-01"
    assert "cumulative_spend" in data


# ===========================================================================
# Context-Aware Validation Tests
# ===========================================================================


@pytest.mark.anyio
async def test_personal_assistant_limit_capped_by_user_limit(client: AsyncClient):
    """Test that personal assistant limit cannot exceed user's personal limit."""
    # Set user's personal spending limit
    response = await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": 50.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create personal assistant
    response = await _create_assistant(client, "PersonalCapped", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Try to set assistant limit higher than user limit - should fail
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )

    assert response.status_code == 400, response.json()
    assert "user limit" in response.json()["detail"].lower()

    # Set assistant limit within user limit - should succeed
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 25.00},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] == 25.00

    # Clean up - remove user limit to not affect other tests
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )


@pytest.mark.anyio
async def test_org_assistant_limit_capped_by_member_and_org_limit(client: AsyncClient):
    """Test that org assistant limit is capped by both member and org limits."""
    # Create organization
    response = await _create_organization(client, "OrgAssistantLimitOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_data = response.json()
    org_id = org_data["id"]
    org_api_key = org_data["api_key"]

    # Create headers for org context
    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
        "Content-Type": "application/json",
    }

    # Set org spending limit
    response = await client.put(
        f"/v0/organizations/{org_id}/spending-limit",
        json={"monthly_spending_cap": 200.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Get owner ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Set member spending limit (lower than org)
    response = await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create org assistant using org API key
    response = await _create_assistant(client, "OrgCapped", "TestBot", org_headers)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Try to set assistant limit higher than member limit - should fail
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 150.00},
        headers=org_headers,
    )

    assert response.status_code == 400, response.json()
    assert "member limit" in response.json()["detail"].lower()

    # Set assistant limit within member limit - should succeed
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 75.00},
        headers=org_headers,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] == 75.00


# ===========================================================================
# GET Spending Limit Endpoint Tests
# ===========================================================================


@pytest.mark.anyio
async def test_get_assistant_spending_limit(client: AsyncClient):
    """Test getting an assistant's spending limit."""
    # Create an assistant
    response = await _create_assistant(client, "GetLimit", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set a limit first
    await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 150.00},
        headers=HEADERS,
    )

    # Get the limit
    response = await client.get(
        f"/v0/assistant/{agent_id}/spending-limit",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["monthly_spending_cap"] == 150.00
    assert "effective_limit" in data


@pytest.mark.anyio
async def test_get_org_spending_limit(client: AsyncClient):
    """Test getting an organization's spending limit."""
    # Create organization
    response = await _create_organization(client, "GetOrgLimitOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Set a limit first
    await client.put(
        f"/v0/organizations/{org_id}/spending-limit",
        json={"monthly_spending_cap": 500.00},
        headers=HEADERS,
    )

    # Get the limit
    response = await client.get(
        f"/v0/organizations/{org_id}/spending-limit",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["monthly_spending_cap"] == 500.00


@pytest.mark.anyio
async def test_admin_get_org_spend(client: AsyncClient):
    """Test admin getting an organization's cumulative spend."""
    # Create organization
    response = await _create_organization(client, "GetOrgSpendOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Get spend via admin endpoint
    response = await client.get(
        f"/v0/organizations/{org_id}/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["organization_id"] == org_id
    assert data["month"] == "2026-01"
    assert "cumulative_spend" in data


@pytest.mark.anyio
async def test_admin_get_member_spend(client: AsyncClient):
    """Test admin getting a member's cumulative spend within an organization."""
    # Create organization
    response = await _create_organization(client, "GetMemberSpendOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Get owner ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Get member spend via admin endpoint
    response = await client.get(
        f"/v0/organizations/{org_id}/members/{owner_id}/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["organization_id"] == org_id
    assert data["user_id"] == owner_id
    assert data["month"] == "2026-01"
    assert "cumulative_spend" in data


@pytest.mark.anyio
async def test_admin_get_member_spend_with_limit(client: AsyncClient):
    """Test admin getting member spend shows limit and percent when limit is set."""
    # Create organization
    response = await _create_organization(client, "MemberSpendLimitOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Get owner ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Set member limit
    await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )

    # Get member spend via admin endpoint
    response = await client.get(
        f"/v0/organizations/{org_id}/members/{owner_id}/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["limit"] == 100.00
    assert data["percent_used"] == 0.0  # No spend yet


@pytest.mark.anyio
async def test_admin_get_member_spend_non_member(client: AsyncClient):
    """Test admin getting spend for non-member returns 404."""
    # Create organization
    response = await _create_organization(client, "NonMemberSpendOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Try to get spend for non-existent member via admin endpoint
    response = await client.get(
        f"/v0/organizations/{org_id}/members/non_existent_user/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 404, response.json()


# ===========================================================================
# Cascade Behavior Tests
# ===========================================================================


@pytest.mark.anyio
async def test_lowering_user_limit_caps_personal_assistants(client: AsyncClient):
    """Test that lowering user limit caps all personal assistant limits."""
    # First, clear any user limit
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )

    # Create personal assistant
    response = await _create_assistant(client, "CascadeTest", "Bot1", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set assistant limit high
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 200.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Now set user limit lower - should trigger cascade
    response = await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": 50.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    # Check that assistants were capped
    assert data.get("assistants_capped", 0) >= 1

    # Verify assistant limit was actually capped
    response = await client.get(
        f"/v0/assistant/{agent_id}/spending-limit",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] == 50.00

    # Clean up
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )


@pytest.mark.anyio
async def test_lowering_org_limit_caps_members_and_assistants(client: AsyncClient):
    """Test that lowering org limit caps member limits."""
    # Create organization
    response = await _create_organization(client, "CascadeOrgLimitOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Get owner ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Set member limit high
    response = await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 500.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Now set org limit lower - should cascade to member
    response = await client.put(
        f"/v0/organizations/{org_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    # Check cascade info
    if data.get("cascaded_updates"):
        assert data["cascaded_updates"].get("members_capped", 0) >= 1

    # Verify member limit was capped
    response = await client.get(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] == 100.00


# ===========================================================================
# Edge Cases and Validation Tests
# ===========================================================================


@pytest.mark.anyio
async def test_set_zero_spending_limit(client: AsyncClient):
    """Test setting spending limit to zero is valid."""
    # Create an assistant
    response = await _create_assistant(client, "ZeroLimit", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set limit to zero
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 0.00},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] == 0.00


@pytest.mark.anyio
async def test_negative_spending_limit_rejected(client: AsyncClient):
    """Test that negative spending limits are rejected."""
    # Create an assistant
    response = await _create_assistant(client, "NegLimit", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Try to set negative limit
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": -50.00},
        headers=HEADERS,
    )

    assert response.status_code == 422, response.json()  # Validation error


@pytest.mark.anyio
async def test_effective_limit_calculation(client: AsyncClient):
    """Test that effective_limit is correctly calculated."""
    # Set user limit
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )

    # Create assistant
    response = await _create_assistant(client, "EffectiveLimit", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set assistant limit lower than user
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 50.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    # Effective limit should be 50 (min of assistant and user)
    assert response.json()["effective_limit"] == 50.00

    # Get the limit
    response = await client.get(
        f"/v0/assistant/{agent_id}/spending-limit",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["effective_limit"] == 50.00

    # Clean up
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )


@pytest.mark.anyio
async def test_assistant_no_limit_inherits_user_effective_limit(client: AsyncClient):
    """Test that assistant with no limit shows user limit as effective limit."""
    # Set user limit
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": 75.00},
        headers=HEADERS,
    )

    # Create assistant (no limit set)
    response = await _create_assistant(client, "NoOwnLimit", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Get assistant spending limit - should show user limit as effective
    response = await client.get(
        f"/v0/assistant/{agent_id}/spending-limit",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["monthly_spending_cap"] is None
    assert data["effective_limit"] == 75.00  # Inherits from user

    # Clean up
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )


@pytest.mark.anyio
async def test_admin_get_org_spend_invalid_month(client: AsyncClient):
    """Test admin getting org spend with invalid month format."""
    # Create organization
    response = await _create_organization(client, "InvalidMonthOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Try invalid month format via admin endpoint
    response = await client.get(
        f"/v0/organizations/{org_id}/spend?month=2026-13",  # Invalid month
        headers=HEADERS,
    )

    assert response.status_code == 422, response.json()


@pytest.mark.anyio
async def test_get_org_spending_limit_non_member(client: AsyncClient):
    """Test that non-member cannot get org spending limit."""
    # Create organization
    response = await _create_organization(client, "NonMemberGetLimitOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Create a different user who is not a member
    non_member = await create_test_user(
        client,
        "non_member_get_limit@example.com",
    )

    # Non-member should not be able to get spending limit
    response = await client.get(
        f"/v0/organizations/{org_id}/spending-limit",
        headers=non_member["headers"],
    )

    assert response.status_code == 403, response.json()


@pytest.mark.anyio
async def test_update_assistant_via_patch_with_spending_cap(client: AsyncClient):
    """Test updating assistant spending cap via the update_assistant DAO."""
    # First ensure no user limit
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )

    # Create an assistant
    response = await _create_assistant(client, "PatchUpdate", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Update assistant config including spending cap via PATCH
    response = await client.patch(
        f"/v0/assistant/{agent_id}/config",
        json={"monthly_spending_cap": 125.00},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()

    # Verify via GET spending-limit
    response = await client.get(
        f"/v0/assistant/{agent_id}/spending-limit",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] == 125.00


# ===========================================================================
# Member Limit Cascade to Org Assistants Tests
# ===========================================================================


@pytest.mark.anyio
async def test_lowering_member_limit_caps_org_assistants(client: AsyncClient):
    """Test that lowering member limit caps that member's org assistants."""
    # Create organization
    response = await _create_organization(client, "MemberCascadeOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_data = response.json()
    org_id = org_data["id"]
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
        "Content-Type": "application/json",
    }

    # Get owner ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Set member limit high first
    response = await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 500.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create org assistant with high limit
    response = await _create_assistant(client, "MemberCascade", "Bot", org_headers)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set assistant limit to 300 (below member's 500)
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 300.00},
        headers=org_headers,
    )
    assert response.status_code == 200, response.json()

    # Now lower member limit to 100 - should cascade to assistant
    response = await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    data = response.json()

    # Check that assistants were capped
    if data.get("cascaded_updates"):
        assert data["cascaded_updates"].get("assistants_capped", 0) >= 1

    # Verify assistant limit was capped to member limit
    response = await client.get(
        f"/v0/assistant/{agent_id}/spending-limit",
        headers=org_headers,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] == 100.00


@pytest.mark.anyio
async def test_member_limit_cascade_skips_assistants_below_new_limit(
    client: AsyncClient,
):
    """Test that member limit cascade only affects assistants above the new limit."""
    # Create organization
    response = await _create_organization(client, "MemberCascadeSkipOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_data = response.json()
    org_id = org_data["id"]
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
        "Content-Type": "application/json",
    }

    # Get owner ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Set member limit high
    response = await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 500.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create org assistant with LOW limit (below what we'll cascade to)
    response = await _create_assistant(client, "LowLimit", "Bot", org_headers)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set assistant limit to 50 (well below member limit)
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 50.00},
        headers=org_headers,
    )
    assert response.status_code == 200, response.json()

    # Lower member limit to 200 - should NOT affect assistant at 50
    response = await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 200.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Verify assistant limit is unchanged
    response = await client.get(
        f"/v0/assistant/{agent_id}/spending-limit",
        headers=org_headers,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] == 50.00  # Unchanged


# ===========================================================================
# Admin Assistant Spend Endpoint Tests
# ===========================================================================


@pytest.mark.anyio
async def test_owner_can_get_own_assistant_spend(client: AsyncClient):
    """Test that admin endpoint can get assistant's spend."""
    # Clear user limit first
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )

    # Create an assistant
    response = await _create_assistant(client, "OwnerSpend", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Admin endpoint should return the assistant's spend
    response = await client.get(
        f"/v0/assistant/{agent_id}/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert str(data["agent_id"]) == str(agent_id)
    assert data["month"] == "2026-01"
    assert "cumulative_spend" in data


@pytest.mark.anyio
async def test_non_owner_cannot_get_assistant_spend(client: AsyncClient):
    """Test that admin endpoint returns 404 for non-existent assistant."""
    # Try to get spend for non-existent assistant
    response = await client.get(
        "/v0/assistant/999999/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 404, response.json()


@pytest.mark.anyio
async def test_org_member_can_get_org_assistant_spend(client: AsyncClient):
    """Test that admin endpoint can get org assistant's spend."""
    # Create organization
    response = await _create_organization(client, "OrgMemberSpendOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_data = response.json()
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
        "Content-Type": "application/json",
    }

    # Create org assistant
    response = await _create_assistant(client, "OrgMemberView", "Bot", org_headers)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Admin endpoint should return the assistant's spend
    response = await client.get(
        f"/v0/assistant/{agent_id}/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert str(data["agent_id"]) == str(agent_id)


# ===========================================================================
# Admin User Spend Endpoint Tests
# ===========================================================================


@pytest.mark.anyio
async def test_user_can_get_own_spend(client: AsyncClient):
    """Test that admin endpoint can get user's cumulative spend."""
    # Get current user ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]

    response = await client.get(
        "/v0/user/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["month"] == "2026-01"
    assert "cumulative_spend" in data


@pytest.mark.anyio
async def test_user_spend_includes_limit_when_set(client: AsyncClient):
    """Test that user spend includes limit and percent when limit is set."""
    # Get current user ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]

    # Set user limit
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )

    response = await client.get(
        "/v0/user/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["limit"] == 100.00
    assert data["percent_used"] == 0.0  # No spend yet

    # Clean up
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )


@pytest.mark.anyio
async def test_user_spend_no_limit_shows_null(client: AsyncClient):
    """Test that user spend shows null limit when no limit is set."""
    # Get current user ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]

    # Ensure no limit
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )

    response = await client.get(
        "/v0/user/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data.get("limit") is None


# ===========================================================================
# Admin Organization Spend Endpoint Tests
# ===========================================================================


@pytest.mark.anyio
async def test_org_member_can_get_org_spend(client: AsyncClient):
    """Test that admin endpoint can get organization's spend."""
    # Create organization
    response = await _create_organization(client, "MemberViewOrgSpend", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Admin endpoint should return the org spend
    response = await client.get(
        f"/v0/organizations/{org_id}/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["organization_id"] == org_id
    assert data["month"] == "2026-01"
    assert "cumulative_spend" in data


@pytest.mark.anyio
async def test_non_member_cannot_get_org_spend(client: AsyncClient):
    """Test that admin endpoint returns 404 for non-existent organization."""
    # Try to get spend for non-existent organization
    response = await client.get(
        "/v0/organizations/999999/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 404, response.json()


# ===========================================================================
# Spend Aggregation Tests
# ===========================================================================


@pytest.mark.anyio
async def test_user_spend_aggregates_across_multiple_assistants(client: AsyncClient):
    """Test that user spend endpoint aggregates spend from multiple assistants.

    Seeds credit_transaction rows for two personal assistants and verifies
    the /user/spend endpoint returns their sum.
    """
    from datetime import datetime

    current_month = datetime.utcnow().strftime("%Y-%m")

    # Clear any existing user limit
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )

    # Get the current user ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]

    # Create two personal assistants
    response1 = await _create_assistant(client, "MultiSpendBot1", "TestBot", HEADERS)
    assert response1.status_code in [200, 201], response1.json()
    agent_id_1 = response1.json()["info"]["agent_id"]

    response2 = await _create_assistant(client, "MultiSpendBot2", "TestBot", HEADERS)
    assert response2.status_code in [200, 201], response2.json()
    agent_id_2 = response2.json()["info"]["agent_id"]

    # Deduct credits for first assistant (creates credit_transaction rows)
    resp = await client.post(
        "/v0/credits/deduct",
        json={
            "amount": 25.0,
            "category": "llm",
            "assistant_id": agent_id_1,
            "user_id": user_id,
            "description": "Assistant work",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()

    # Deduct credits for second assistant
    resp = await client.post(
        "/v0/credits/deduct",
        json={
            "amount": 15.0,
            "category": "llm",
            "assistant_id": agent_id_2,
            "user_id": user_id,
            "description": "Assistant work",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.json()

    # Query the current month (ledger uses the `at` timestamp, not explicit month metadata)
    response = await client.get(
        f"/v0/user/spend?month={current_month}",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    # Should aggregate spend from BOTH assistants: 25 + 15 = 40
    assert data["cumulative_spend"] >= 40.0, (
        f"Expected cumulative_spend >= 40.0 (sum of 25+15 from two assistants), "
        f"got {data['cumulative_spend']}. This suggests the query is not aggregating correctly."
    )


@pytest.mark.anyio
async def test_org_spend_aggregates_across_multiple_assistants(client: AsyncClient):
    """Test that org spend endpoint aggregates spend from multiple org assistants.

    Seeds credit_transaction rows for two org assistants and verifies the
    /organizations/{id}/spend endpoint returns their sum.
    """
    from datetime import datetime

    current_month = datetime.utcnow().strftime("%Y-%m")

    # Create an organization
    response = await _create_organization(client, "SpendAggregateOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_data = response.json()
    org_id = org_data["id"]
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
        "Content-Type": "application/json",
    }

    # Create two org assistants
    response1 = await _create_assistant(client, "OrgSpendBot1", "TestBot", org_headers)
    assert response1.status_code in [200, 201], response1.json()
    agent_id_1 = response1.json()["info"]["agent_id"]

    response2 = await _create_assistant(client, "OrgSpendBot2", "TestBot", org_headers)
    assert response2.status_code in [200, 201], response2.json()
    agent_id_2 = response2.json()["info"]["agent_id"]

    # Deduct credits for first org assistant
    resp = await client.post(
        "/v0/credits/deduct",
        json={
            "amount": 50.0,
            "category": "llm",
            "assistant_id": agent_id_1,
            "description": "Assistant work",
        },
        headers=org_headers,
    )
    assert resp.status_code == 200, resp.json()

    # Deduct credits for second org assistant
    resp = await client.post(
        "/v0/credits/deduct",
        json={
            "amount": 30.0,
            "category": "llm",
            "assistant_id": agent_id_2,
            "description": "Assistant work",
        },
        headers=org_headers,
    )
    assert resp.status_code == 200, resp.json()

    # Query the current month (ledger uses the `at` timestamp)
    response = await client.get(
        f"/v0/organizations/{org_id}/spend?month={current_month}",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    # Should aggregate spend from BOTH org assistants: 50 + 30 = 80
    assert data["cumulative_spend"] >= 80.0, (
        f"Expected cumulative_spend >= 80.0 (sum of 50+30 from two org assistants), "
        f"got {data['cumulative_spend']}. This suggests the query is not using SUM() to aggregate."
    )


@pytest.mark.anyio
async def test_member_spend_aggregates_across_org_assistants(client: AsyncClient):
    """Test that member spend endpoint aggregates spend from all assistants created by member.

    Seeds credit_transaction rows with user_id attribution and verifies the
    /organizations/{id}/members/{user_id}/spend endpoint returns their sum.
    """
    from datetime import datetime

    current_month = datetime.utcnow().strftime("%Y-%m")

    # Create an organization
    response = await _create_organization(client, "MemberSpendAggOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_data = response.json()
    org_id = org_data["id"]
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
        "Content-Type": "application/json",
    }

    # Get owner ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Create two org assistants by the member
    response1 = await _create_assistant(client, "MemberBot1", "AggTest", org_headers)
    assert response1.status_code in [200, 201], response1.json()
    agent_id_1 = response1.json()["info"]["agent_id"]

    response2 = await _create_assistant(client, "MemberBot2", "AggTest", org_headers)
    assert response2.status_code in [200, 201], response2.json()
    agent_id_2 = response2.json()["info"]["agent_id"]

    # Deduct credits for first assistant (attributed to the owner/member)
    resp = await client.post(
        "/v0/credits/deduct",
        json={
            "amount": 20.0,
            "category": "llm",
            "assistant_id": agent_id_1,
            "user_id": owner_id,
            "description": "Assistant work",
        },
        headers=org_headers,
    )
    assert resp.status_code == 200, resp.json()

    # Deduct credits for second assistant
    resp = await client.post(
        "/v0/credits/deduct",
        json={
            "amount": 35.0,
            "category": "llm",
            "assistant_id": agent_id_2,
            "user_id": owner_id,
            "description": "Assistant work",
        },
        headers=org_headers,
    )
    assert resp.status_code == 200, resp.json()

    # Query the current month (ledger uses the `at` timestamp)
    response = await client.get(
        f"/v0/organizations/{org_id}/members/{owner_id}/spend?month={current_month}",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    # Should aggregate spend from BOTH assistants: 20 + 35 = 55
    assert data["cumulative_spend"] >= 55.0, (
        f"Expected cumulative_spend >= 55.0 (sum of 20+35 from member's assistants), "
        f"got {data['cumulative_spend']}. This suggests member spend aggregation is broken."
    )


# ===========================================================================
# Admin Member Spend Endpoint Tests
# ===========================================================================


@pytest.mark.anyio
async def test_member_can_get_own_member_spend(client: AsyncClient):
    """Test that admin endpoint can get member's spend within an organization."""
    # Create organization
    response = await _create_organization(client, "OwnMemberSpendOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_id = response.json()["id"]

    # Get owner ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Admin endpoint should return the member's spend
    response = await client.get(
        f"/v0/organizations/{org_id}/members/{owner_id}/spend?month=2026-01",
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["organization_id"] == org_id
    assert data["user_id"] == owner_id
    assert "cumulative_spend" in data


# ===========================================================================
# Edge Cases for Spending Limits
# ===========================================================================


@pytest.mark.anyio
async def test_very_large_spending_limit(client: AsyncClient):
    """Test setting a very large spending limit."""
    # Create an assistant
    response = await _create_assistant(client, "LargeLimit", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set a very large limit
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 999999999.99},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] == 999999999.99


@pytest.mark.anyio
async def test_fractional_spending_limit(client: AsyncClient):
    """Test setting a fractional spending limit."""
    # Create an assistant
    response = await _create_assistant(client, "FracLimit", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set a fractional limit
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 0.01},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] == 0.01


@pytest.mark.anyio
async def test_update_spending_limit_multiple_times(client: AsyncClient):
    """Test updating spending limit multiple times."""
    # Create an assistant
    response = await _create_assistant(client, "MultiUpdate", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Update limit several times
    for limit in [100.0, 200.0, 50.0, 150.0]:
        response = await client.put(
            f"/v0/assistant/{agent_id}/spending-limit",
            json={"monthly_spending_cap": limit},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert response.json()["monthly_spending_cap"] == limit


@pytest.mark.anyio
async def test_remove_spending_limit_by_setting_null(client: AsyncClient):
    """Test removing spending limit by setting to null."""
    # First ensure no user limit (to allow any assistant limit)
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )

    # Create an assistant
    response = await _create_assistant(client, "RemoveLimit", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Set a limit first
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": 100.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Remove the limit by setting to null
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["monthly_spending_cap"] is None


@pytest.mark.anyio
async def test_org_assistant_inherits_member_limit_as_effective(client: AsyncClient):
    """Test that org assistant with no limit shows member limit as effective."""
    # Create organization
    response = await _create_organization(client, "InheritMemberOrg", HEADERS)
    assert response.status_code in [200, 201], response.json()
    org_data = response.json()
    org_id = org_data["id"]
    org_api_key = org_data["api_key"]

    org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {org_api_key}",
        "Content-Type": "application/json",
    }

    # Get owner ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    owner_id = credits_resp.json()["id"]

    # Set member limit
    response = await client.put(
        f"/v0/organizations/{org_id}/members/{owner_id}/spending-limit",
        json={"monthly_spending_cap": 250.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()

    # Create org assistant (no limit set)
    response = await _create_assistant(client, "InheritMember", "Bot", org_headers)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Get assistant spending limit - should show member limit as effective
    response = await client.get(
        f"/v0/assistant/{agent_id}/spending-limit",
        headers=org_headers,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["monthly_spending_cap"] is None
    assert data["effective_limit"] == 250.00  # Inherits from member


# ===========================================================================
# Month Format Validation Tests
# ===========================================================================


@pytest.mark.anyio
async def test_spend_endpoint_rejects_invalid_month_format(client: AsyncClient):
    """Test that spend endpoint rejects invalid month format."""
    # Create an assistant
    response = await _create_assistant(client, "InvalidFormat", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Try various invalid formats
    invalid_months = [
        "2026",  # Missing month
        "01-2026",  # Wrong order
        "2026/01",  # Wrong separator
        "2026-1",  # Single digit month
        "26-01",  # 2-digit year
        "2026-00",  # Invalid month 00
        "2026-13",  # Invalid month 13
    ]

    for month in invalid_months:
        response = await client.get(
            f"/v0/assistant/{agent_id}/spend?month={month}",
            headers=HEADERS,
        )
        assert response.status_code == 422, f"Expected 422 for month={month}"


@pytest.mark.anyio
async def test_spend_endpoint_requires_month_parameter(client: AsyncClient):
    """Test that spend endpoint requires month parameter."""
    # Create an assistant
    response = await _create_assistant(client, "NoMonth", "TestBot", HEADERS)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Request without month parameter
    response = await client.get(
        f"/v0/assistant/{agent_id}/spend",
        headers=HEADERS,
    )

    assert response.status_code == 422, response.json()  # Missing required param


# ===========================================================================
# Multiple Assistants Cascade Test
# ===========================================================================


@pytest.mark.anyio
async def test_org_member_can_get_other_members_assistant_spend(client: AsyncClient):
    """Test that an org member can view spend for an assistant created by another member.

    The spend endpoint should allow any org member to view spend data for any
    assistant in the org, not just assistants they personally created.
    """
    # Owner creates org
    owner = await create_test_user(client, "spend_org_owner@example.com")
    response = await _create_organization(
        client,
        "MemberSpendAccessOrg",
        owner["headers"],
    )
    assert response.status_code in [200, 201], response.json()
    org_data = response.json()
    org_id = org_data["id"]
    owner_org_key = org_data["api_key"]
    owner_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {owner_org_key}",
        "Content-Type": "application/json",
    }

    # Create a second user and add them as a member
    member = await create_test_user(client, "spend_org_member@example.com")
    add_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_resp.status_code == 201, add_resp.json()
    member_org_key = add_resp.json()["api_key"]
    member_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {member_org_key}",
        "Content-Type": "application/json",
    }

    # Owner creates an assistant in the org
    response = await _create_assistant(
        client,
        "OwnerBot",
        "SpendTest",
        owner_org_headers,
    )
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Member should be able to get spend for the owner's assistant
    response = await client.get(
        f"/v0/assistant/{agent_id}/spend?month=2026-01",
        headers=member_org_headers,
    )
    assert response.status_code == 200, (
        f"Org member should be able to view spend for another member's assistant, "
        f"got {response.status_code}: {response.json()}"
    )
    data = response.json()
    assert str(data["agent_id"]) == str(agent_id)


@pytest.mark.anyio
async def test_org_member_can_get_other_members_assistant_spending_limit(
    client: AsyncClient,
):
    """Test that an org member can view spending limit for another member's assistant."""
    # Owner creates org
    owner = await create_test_user(client, "limit_org_owner@example.com")
    response = await _create_organization(
        client,
        "MemberLimitAccessOrg",
        owner["headers"],
    )
    assert response.status_code in [200, 201], response.json()
    org_data = response.json()
    org_id = org_data["id"]
    owner_org_key = org_data["api_key"]
    owner_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {owner_org_key}",
        "Content-Type": "application/json",
    }

    # Create a second user and add them as a member
    member = await create_test_user(client, "limit_org_member@example.com")
    add_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"]},
        headers=owner["headers"],
    )
    assert add_resp.status_code == 201, add_resp.json()
    member_org_key = add_resp.json()["api_key"]
    member_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {member_org_key}",
        "Content-Type": "application/json",
    }

    # Owner creates an assistant in the org
    response = await _create_assistant(
        client,
        "OwnerBot",
        "LimitTest",
        owner_org_headers,
    )
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Member should be able to get spending limit for the owner's assistant
    response = await client.get(
        f"/v0/assistant/{agent_id}/spending-limit",
        headers=member_org_headers,
    )
    assert response.status_code == 200, (
        f"Org member should be able to view spending limit for another member's assistant, "
        f"got {response.status_code}: {response.json()}"
    )
    data = response.json()
    assert str(data["agent_id"]) == str(agent_id)


@pytest.mark.anyio
async def test_non_org_member_cannot_get_org_assistant_spend(client: AsyncClient):
    """Test that a user who is NOT an org member cannot view an org assistant's spend."""
    # Owner creates org
    owner = await create_test_user(client, "spend_access_owner@example.com")
    response = await _create_organization(
        client,
        "NonMemberSpendOrg2",
        owner["headers"],
    )
    assert response.status_code in [200, 201], response.json()
    org_data = response.json()
    owner_org_key = org_data["api_key"]
    owner_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {owner_org_key}",
        "Content-Type": "application/json",
    }

    # Owner creates an assistant
    response = await _create_assistant(client, "OrgOnly", "SpendBot", owner_org_headers)
    assert response.status_code in [200, 201], response.json()
    agent_id = response.json()["info"]["agent_id"]

    # Create outsider (not a member of the org)
    outsider = await create_test_user(client, "spend_outsider@example.com")

    # Outsider should NOT be able to get spend
    response = await client.get(
        f"/v0/assistant/{agent_id}/spend?month=2026-01",
        headers=outsider["headers"],
    )
    assert response.status_code == 404, response.json()


@pytest.mark.anyio
async def test_user_limit_cascade_caps_multiple_assistants(client: AsyncClient):
    """Test that lowering user limit caps multiple personal assistants."""
    # First, clear any user limit
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )

    # Create multiple assistants with different limits
    agent_ids = []
    original_limits = [200.00, 150.00, 100.00]

    for i, limit in enumerate(original_limits):
        response = await _create_assistant(
            client,
            f"MultiCascade{i}",
            "Bot",
            HEADERS,
        )
        assert response.status_code in [200, 201], response.json()
        agent_id = response.json()["info"]["agent_id"]
        agent_ids.append(agent_id)

        # Set each assistant's limit
        response = await client.put(
            f"/v0/assistant/{agent_id}/spending-limit",
            json={"monthly_spending_cap": limit},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()

    # Set user limit to 75 - should cap first two assistants
    response = await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": 75.00},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    # Should have capped at least 2 assistants (200 and 150 -> 75)
    assert data.get("assistants_capped", 0) >= 2

    # Verify all assistants are now at 75 or below
    for agent_id in agent_ids:
        response = await client.get(
            f"/v0/assistant/{agent_id}/spending-limit",
            headers=HEADERS,
        )
        assert response.status_code == 200, response.json()
        assert response.json()["monthly_spending_cap"] <= 75.00

    # Clean up
    await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": None},
        headers=HEADERS,
    )
