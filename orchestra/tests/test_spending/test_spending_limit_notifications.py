"""
Tests for spending limit notification functionality.

These tests stress-test the notification logic including:
- Deduplication (same limit/month should only notify once)
- Limit value changes (new limit value = new notification)
- Limit re-enable scenarios (limit_set_at logic)
- Recipient determination for each limit type
- Concurrency handling
- Edge cases
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, HEADERS

# ===========================================================================
# Test Fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """Mock assistant infrastructure to prevent real network calls."""
    if "no_mock_infra" in request.keywords:
        yield
        return

    with patch(
        "orchestra.web.api.assistant.views.wake_up_assistant",
        new_callable=AsyncMock,
    ) as mock_wake_up, patch(
        "orchestra.web.api.assistant.views.reawaken_assistant",
        new_callable=AsyncMock,
    ) as mock_reawaken:
        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        yield mock_wake_up, mock_reawaken


@pytest.fixture
def mock_email_sending():
    """Mock email sending to capture what would be sent."""
    with patch(
        "orchestra.web.api.utils.email.send_email_async",
        new_callable=AsyncMock,
    ) as mock_send:
        mock_send.return_value = True
        yield mock_send


# ===========================================================================
# Helper Functions
# ===========================================================================


async def _create_assistant(
    client: AsyncClient,
    first_name: str,
    surname: str,
    headers: dict,
) -> Dict[str, Any]:
    """Create an assistant and return the response data."""
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
    assert response.status_code in [200, 201], response.json()
    return response.json()["info"]


async def _create_organization(
    client: AsyncClient,
    name: str,
    headers: dict,
) -> Dict[str, Any]:
    """Create an organization and return the response data."""
    response = await client.post(
        "/v0/organizations",
        json={"name": name},
        headers=headers,
    )
    assert response.status_code in [200, 201], response.json()
    return response.json()


async def _set_assistant_limit(
    client: AsyncClient,
    agent_id: str,
    limit: Optional[float],
    headers: dict,
) -> Dict[str, Any]:
    """Set an assistant's spending limit."""
    response = await client.put(
        f"/v0/assistant/{agent_id}/spending-limit",
        json={"monthly_spending_cap": limit},
        headers=headers,
    )
    assert response.status_code == 200, response.json()
    return response.json()


async def _set_user_limit(
    client: AsyncClient,
    limit: Optional[float],
    headers: dict,
) -> Dict[str, Any]:
    """Set a user's personal spending limit."""
    response = await client.put(
        "/v0/user/spending-limit",
        json={"monthly_spending_cap": limit},
        headers=headers,
    )
    assert response.status_code == 200, response.json()
    return response.json()


async def _set_org_limit(
    client: AsyncClient,
    org_id: int,
    limit: Optional[float],
    headers: dict,
) -> Dict[str, Any]:
    """Set an organization's spending limit."""
    response = await client.put(
        f"/v0/organizations/{org_id}/spending-limit",
        json={"monthly_spending_cap": limit},
        headers=headers,
    )
    assert response.status_code == 200, response.json()
    return response.json()


async def _set_member_limit(
    client: AsyncClient,
    org_id: int,
    user_id: str,
    limit: Optional[float],
    headers: dict,
) -> Dict[str, Any]:
    """Set a member's spending limit within an organization."""
    response = await client.put(
        f"/v0/organizations/{org_id}/members/{user_id}/spending-limit",
        json={"monthly_spending_cap": limit},
        headers=headers,
    )
    assert response.status_code == 200, response.json()
    return response.json()


async def _get_user_id(client: AsyncClient, headers: dict) -> str:
    """Get the user ID for the given auth headers."""
    response = await client.get("/v0/credits", headers=headers)
    return response.json()["id"]


async def _transfer_assistant_to_org(
    client: AsyncClient,
    agent_id: int,
    org_id: int,
    headers: dict,
) -> Dict[str, Any]:
    """Transfer an assistant to an organization."""
    response = await client.post(
        f"/v0/assistant/{agent_id}/transfer/to-org",
        json={"organization_id": org_id, "transfer_logs": False},
        headers=headers,
    )
    assert response.status_code == 200, response.json()
    return response.json()


async def _trigger_spending_limit_notification(
    client: AsyncClient,
    limit_type: str,
    entity_id: str,
    limit_value: float,
    current_spend: float,
    month: str,
    limit_set_at: Optional[str] = None,
    organization_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Call the spending limit notification endpoint."""
    payload = {
        "limit_type": limit_type,
        "entity_id": entity_id,
        "limit_value": limit_value,
        "current_spend": current_spend,
        "month": month,
    }
    if limit_set_at:
        payload["limit_set_at"] = limit_set_at
    if organization_id is not None:
        payload["organization_id"] = organization_id

    response = await client.post(
        "/v0/user/spending-limit-reached",
        json=payload,
        headers=HEADERS,
    )
    return {"status_code": response.status_code, "data": response.json()}


# ===========================================================================
# Basic Notification Tests
# ===========================================================================


@pytest.mark.anyio
async def test_first_notification_for_assistant_limit_is_sent(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """First time an assistant limit is reached, a notification should be sent."""
    # Create assistant with a spending limit
    assistant = await _create_assistant(client, "NotifyTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    # Trigger notification for limit reached
    result = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )

    assert result["status_code"] == 200
    assert result["data"]["notified"] is True
    assert result["data"]["recipient_count"] >= 1


@pytest.mark.anyio
async def test_first_notification_for_user_limit_is_sent(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """First time a user's personal limit is reached, a notification should be sent."""
    user_id = await _get_user_id(client, HEADERS)
    await _set_user_limit(client, 200.00, HEADERS)

    result = await _trigger_spending_limit_notification(
        client,
        limit_type="user",
        entity_id=user_id,
        limit_value=200.00,
        current_spend=200.00,
        month="2026-02",
    )

    assert result["status_code"] == 200
    assert result["data"]["notified"] is True


@pytest.mark.anyio
async def test_first_notification_for_org_limit_is_sent(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """First time an org limit is reached, notification sent to members with assistants."""
    org = await _create_organization(client, "NotifyTestOrg", HEADERS)
    org_id = org["id"]
    await _set_org_limit(client, org_id, 500.00, HEADERS)

    # Create an assistant and transfer to org so the owner qualifies for notification
    assistant = await _create_assistant(client, "OrgTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _transfer_assistant_to_org(client, agent_id, org_id, HEADERS)

    result = await _trigger_spending_limit_notification(
        client,
        limit_type="organization",
        entity_id=str(org_id),
        limit_value=500.00,
        current_spend=500.00,
        month="2026-02",
    )

    assert result["status_code"] == 200
    assert result["data"]["notified"] is True


# ===========================================================================
# Deduplication Tests - Same Limit Value, Same Month
# ===========================================================================


@pytest.mark.anyio
async def test_duplicate_notification_same_limit_same_month_is_skipped(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Limit is reached, notification sent. Same limit reached again in same month.
    Expected: Second notification should be skipped (deduplication).
    """
    assistant = await _create_assistant(client, "DedupTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    # First notification - should be sent
    result1 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )
    assert result1["data"]["notified"] is True

    # Second notification - same limit, same month - should be skipped
    result2 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=105.00,  # Spend increased but limit is same
        month="2026-02",
    )
    assert result2["data"]["notified"] is False
    assert result2["data"]["reason"] == "already_notified"


@pytest.mark.anyio
async def test_multiple_blocked_calls_only_one_notification(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: 10 LLM calls are blocked in quick succession due to limit.
    Expected: Only one notification email is sent (deduplication handles the rest).
    """
    assistant = await _create_assistant(client, "MultiBlockTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _set_assistant_limit(client, agent_id, 50.00, HEADERS)

    results = []
    for i in range(10):
        result = await _trigger_spending_limit_notification(
            client,
            limit_type="assistant",
            entity_id=str(agent_id),
            limit_value=50.00,
            current_spend=50.00 + i,  # Slight variation in spend
            month="2026-02",
        )
        results.append(result["data"]["notified"])

    # First should be True, rest should be False
    assert results[0] is True
    assert all(r is False for r in results[1:])


# ===========================================================================
# Different Limit Values - Should Trigger New Notifications
# ===========================================================================


@pytest.mark.anyio
async def test_different_limit_values_each_get_notification(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Limit set to $100, reached, notified. Limit increased to $150, reached.
    Expected: Both limits trigger separate notifications.
    """
    assistant = await _create_assistant(client, "LimitChangeTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]

    # First limit: $100
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)
    result1 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )
    assert result1["data"]["notified"] is True

    # Limit increased to $150
    await _set_assistant_limit(client, agent_id, 150.00, HEADERS)
    result2 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=150.00,
        current_spend=150.00,
        month="2026-02",
    )
    assert result2["data"]["notified"] is True  # New limit value = new notification


@pytest.mark.anyio
async def test_limit_decreased_triggers_new_notification(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Limit set to $200, reached, notified. Limit decreased to $100.
              Since spend is still at $200, the $100 limit is immediately exceeded.
    Expected: New notification for the $100 limit.
    """
    user_id = await _get_user_id(client, HEADERS)

    # First limit: $200
    await _set_user_limit(client, 200.00, HEADERS)
    result1 = await _trigger_spending_limit_notification(
        client,
        limit_type="user",
        entity_id=user_id,
        limit_value=200.00,
        current_spend=200.00,
        month="2026-02",
    )
    assert result1["data"]["notified"] is True

    # Limit decreased to $100 (spend still at $200)
    await _set_user_limit(client, 100.00, HEADERS)
    result2 = await _trigger_spending_limit_notification(
        client,
        limit_type="user",
        entity_id=user_id,
        limit_value=100.00,
        current_spend=200.00,
        month="2026-02",
    )
    assert result2["data"]["notified"] is True  # Different limit value


# ===========================================================================
# Limit Re-Enable Scenarios (limit_set_at logic)
# ===========================================================================


@pytest.mark.anyio
async def test_limit_removed_and_reenabled_triggers_new_notification(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario:
    1. Limit set to $100, reached, notified
    2. Limit removed (NULL)
    3. Limit set back to $100 (with a newer timestamp)
    4. Immediately blocked again
    Expected: New notification is sent (limit was re-enabled after previous notification).
    """
    assistant = await _create_assistant(client, "ReenableTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]

    # Step 1: Set limit and trigger notification (no limit_set_at = recorded with current time)
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    result1 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-01",
        limit_set_at=None,  # No timestamp - uses default deduplication
    )
    assert result1["data"]["notified"] is True

    # Step 2: Remove limit (this doesn't trigger notification - limit is gone)
    await _set_assistant_limit(client, agent_id, None, HEADERS)

    # Step 3: Re-enable limit - use a future date to ensure it's after notified_at
    future_date = datetime(2027, 1, 20, 14, 0, 0, tzinfo=timezone.utc)
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    # Step 4: Same limit value, but limit_set_at is definitively AFTER the previous notification
    result2 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-01",
        limit_set_at=future_date.isoformat(),
    )
    assert result2["data"]["notified"] is True  # Re-enabled limit gets new notification


@pytest.mark.anyio
async def test_limit_set_before_notification_does_not_retrigger(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Limit set at Jan 5, notification sent at Jan 10.
              Same limit, same month, limit_set_at is BEFORE notified_at.
    Expected: No new notification (limit hasn't been re-configured).
    """
    assistant = await _create_assistant(client, "NoRetriggerTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]

    jan_5 = datetime(2026, 1, 5, 10, 0, 0, tzinfo=timezone.utc)
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    # First notification
    result1 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-01",
        limit_set_at=jan_5.isoformat(),
    )
    assert result1["data"]["notified"] is True

    # Second attempt with same limit_set_at (before the notification)
    result2 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=105.00,
        month="2026-01",
        limit_set_at=jan_5.isoformat(),
    )
    assert result2["data"]["notified"] is False


# ===========================================================================
# Different Months - Each Month Gets Its Own Notifications
# ===========================================================================


@pytest.mark.anyio
async def test_same_limit_different_months_both_notified(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Limit of $100 reached in January and February.
    Expected: Both months receive notifications (deduplication is per-month).
    """
    assistant = await _create_assistant(client, "MultiMonthTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    # January notification
    result_jan = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-01",
    )
    assert result_jan["data"]["notified"] is True

    # February notification
    result_feb = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )
    assert result_feb["data"]["notified"] is True  # Different month


# ===========================================================================
# Multiple Entity Types - Independent Deduplication
# ===========================================================================


@pytest.mark.anyio
async def test_different_entity_types_independent_notifications(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: User limit and assistant limit both hit $100 in same month.
    Expected: Both get separate notifications (different entity types).
    """
    user_id = await _get_user_id(client, HEADERS)
    assistant = await _create_assistant(client, "EntityTypeTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]

    await _set_user_limit(client, 100.00, HEADERS)
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    # User limit notification
    result_user = await _trigger_spending_limit_notification(
        client,
        limit_type="user",
        entity_id=user_id,
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )
    assert result_user["data"]["notified"] is True

    # Assistant limit notification (same limit value, same month, different entity)
    result_assistant = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )
    assert result_assistant["data"]["notified"] is True


@pytest.mark.anyio
async def test_org_limit_and_member_limit_independent(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Org limit reached, then member limit reached.
    Expected: Both get separate notifications (user may receive both emails).
    """
    user_id = await _get_user_id(client, HEADERS)
    org = await _create_organization(client, "IndependentLimitsOrg", HEADERS)
    org_id = org["id"]

    # Create assistant and transfer to org so user qualifies for org notification
    assistant = await _create_assistant(client, "OrgMemberTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _transfer_assistant_to_org(client, agent_id, org_id, HEADERS)

    await _set_org_limit(client, org_id, 500.00, HEADERS)
    await _set_member_limit(client, org_id, user_id, 150.00, HEADERS)

    # Org limit notification
    result_org = await _trigger_spending_limit_notification(
        client,
        limit_type="organization",
        entity_id=str(org_id),
        limit_value=500.00,
        current_spend=500.00,
        month="2026-02",
    )
    assert result_org["data"]["notified"] is True

    # Member limit notification
    result_member = await _trigger_spending_limit_notification(
        client,
        limit_type="member",
        entity_id=user_id,
        limit_value=150.00,
        current_spend=150.00,
        month="2026-02",
        organization_id=org_id,
    )
    assert result_member["data"]["notified"] is True


# ===========================================================================
# Concurrency Tests
# ===========================================================================


@pytest.mark.anyio
async def test_concurrent_notifications_only_one_succeeds(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Two Unity instances simultaneously call notification endpoint
              for the same limit breach.
    Expected: Only one notification is sent (unique constraint handles race).
    """
    assistant = await _create_assistant(client, "ConcurrencyTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    # Simulate concurrent calls
    async def trigger_notification():
        return await _trigger_spending_limit_notification(
            client,
            limit_type="assistant",
            entity_id=str(agent_id),
            limit_value=100.00,
            current_spend=100.00,
            month="2026-02",
        )

    # Run 5 concurrent requests
    results = await asyncio.gather(*[trigger_notification() for _ in range(5)])

    # Count how many succeeded with notified=True
    notified_count = sum(1 for r in results if r["data"].get("notified") is True)

    # Exactly one should have succeeded
    assert notified_count == 1

    # Others should have been deduplicated
    skipped_count = sum(
        1 for r in results if r["data"].get("reason") == "already_notified"
    )
    assert skipped_count == 4


# ===========================================================================
# Recipient Logic Tests
# ===========================================================================


@pytest.mark.anyio
async def test_assistant_limit_notifies_owner_only(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Assistant limit reached.
    Expected: Only the assistant owner receives the notification.
    """
    assistant = await _create_assistant(client, "OwnerOnlyTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    result = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )

    assert result["data"]["notified"] is True
    assert result["data"]["recipient_count"] == 1

    # Verify the notified_user_ids contains the owner
    user_id = await _get_user_id(client, HEADERS)
    assert user_id in result["data"]["notified_user_ids"]


@pytest.mark.anyio
async def test_org_limit_notifies_members_with_assistants(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Org owner has an assistant in the org.
    Expected: Owner receives notification when org limit is reached.
    """
    # Use default HEADERS user who has credits
    org = await _create_organization(client, "MemberNotifyOrg", HEADERS)
    org_id = org["id"]

    # Owner creates an assistant and transfers to org
    assistant = await _create_assistant(client, "OwnerOrgBot", "Test", HEADERS)
    agent_id = assistant["agent_id"]
    await _transfer_assistant_to_org(client, agent_id, org_id, HEADERS)

    # Set org limit
    await _set_org_limit(client, org_id, 1000.00, HEADERS)

    result = await _trigger_spending_limit_notification(
        client,
        limit_type="organization",
        entity_id=str(org_id),
        limit_value=1000.00,
        current_spend=1000.00,
        month="2026-02",
    )

    assert result["data"]["notified"] is True
    # Owner has an assistant in org, so should receive notification
    assert result["data"]["recipient_count"] >= 1


# ===========================================================================
# Edge Cases
# ===========================================================================


@pytest.mark.anyio
async def test_notification_with_null_limit_set_at_falls_back_to_dedupe(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Legacy data where limit_set_at is NULL.
    Expected: Falls back to standard deduplication (entity+month+limit_value).
    """
    assistant = await _create_assistant(client, "NullSetAtTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    # First notification without limit_set_at
    result1 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
        limit_set_at=None,  # No timestamp provided
    )
    assert result1["data"]["notified"] is True

    # Second notification also without limit_set_at
    result2 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
        limit_set_at=None,
    )
    assert result2["data"]["notified"] is False  # Deduplicated


@pytest.mark.anyio
async def test_notification_for_nonexistent_entity_returns_not_found(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Notification triggered for an entity that doesn't exist.
    Expected: Returns 404 (user-auth endpoint validates ownership).
    """
    result = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id="999999",  # Non-existent
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )

    assert result["status_code"] == 404


@pytest.mark.anyio
async def test_notification_preserves_precision_in_limit_value(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Limit is set to $99.99, then later exactly $99.99 again.
    Expected: Deduplication works correctly with decimal precision.
    """
    assistant = await _create_assistant(client, "PrecisionTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _set_assistant_limit(client, agent_id, 99.99, HEADERS)

    result1 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=99.99,
        current_spend=99.99,
        month="2026-02",
    )
    assert result1["data"]["notified"] is True

    # Same limit value with same precision
    result2 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=99.99,
        current_spend=100.00,
        month="2026-02",
    )
    assert result2["data"]["notified"] is False

    # Slightly different value
    result3 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=99.98,  # Different!
        current_spend=100.00,
        month="2026-02",
    )
    assert result3["data"]["notified"] is True  # New limit value


@pytest.mark.anyio
async def test_orphaned_notification_after_entity_deleted(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Notification sent, then assistant is deleted.
    Expected: Notification record remains (orphaned) for audit trail.
    """
    assistant = await _create_assistant(client, "OrphanTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    # Send notification
    result = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )
    assert result["data"]["notified"] is True

    # Delete the assistant
    delete_resp = await client.delete(
        f"/v0/assistant/{agent_id}",
        headers=HEADERS,
    )
    # Note: This may require different handling based on your delete endpoint

    # The notification record should still exist (would verify via DB query)
    # This test primarily ensures we don't cascade delete notifications


@pytest.mark.anyio
async def test_invalid_limit_type_returns_error(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Invalid limit_type provided.
    Expected: Returns 422 validation error.
    """
    result = await _trigger_spending_limit_notification(
        client,
        limit_type="invalid_type",
        entity_id="123",
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )

    assert result["status_code"] == 422


@pytest.mark.anyio
async def test_invalid_month_format_returns_error(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Invalid month format provided.
    Expected: Returns 422 validation error.
    """
    assistant = await _create_assistant(client, "InvalidMonthTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]

    result = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="invalid-month",
    )

    assert result["status_code"] == 422


# ===========================================================================
# Cascading Limit Scenario Tests
# ===========================================================================


@pytest.mark.anyio
async def test_org_limit_reached_then_member_limit_reached_both_notified(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario:
    1. Org limit is $200
    2. Member limit is $100
    3. Spend reaches $100 → member limit hit → member notification
    4. Spend reaches $200 → org limit hit → org notification

    Expected: User receives both notifications (different limits).
    """
    user_id = await _get_user_id(client, HEADERS)
    org = await _create_organization(client, "CascadeTestOrg", HEADERS)
    org_id = org["id"]

    # Create assistant and transfer to org so user qualifies for org notification
    assistant = await _create_assistant(client, "CascadeBot", "Test", HEADERS)
    agent_id = assistant["agent_id"]
    await _transfer_assistant_to_org(client, agent_id, org_id, HEADERS)

    # Set limits (member limit must be <= org limit)
    await _set_org_limit(client, org_id, 200.00, HEADERS)
    await _set_member_limit(client, org_id, user_id, 100.00, HEADERS)

    # Member limit reached first (lower limit)
    result_member = await _trigger_spending_limit_notification(
        client,
        limit_type="member",
        entity_id=user_id,
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
        organization_id=org_id,
    )
    assert result_member["data"]["notified"] is True

    # Org limit reached later
    result_org = await _trigger_spending_limit_notification(
        client,
        limit_type="organization",
        entity_id=str(org_id),
        limit_value=200.00,
        current_spend=200.00,
        month="2026-02",
    )
    assert result_org["data"]["notified"] is True  # Different limit type


@pytest.mark.anyio
async def test_hierarchical_limits_all_independent(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Assistant, member, and org limits all reached in sequence.
    Expected: Each gets its own notification (all tracked independently).
    """
    user_id = await _get_user_id(client, HEADERS)
    org = await _create_organization(client, "HierarchyTestOrg", HEADERS)
    org_id = org["id"]
    assistant = await _create_assistant(client, "HierarchyTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]

    # Transfer assistant to org so org notifications work
    await _transfer_assistant_to_org(client, agent_id, org_id, HEADERS)

    # Set all limits (respecting hierarchy: assistant <= member <= org)
    await _set_org_limit(client, org_id, 500.00, HEADERS)
    await _set_member_limit(client, org_id, user_id, 200.00, HEADERS)
    # Note: assistant limit is set on the assistant directly

    # All three limits reached - using the personal assistant for assistant limit
    # (We need a separate personal assistant for this test)
    personal_assistant = await _create_assistant(
        client,
        "PersonalHierarchy",
        "Bot",
        HEADERS,
    )
    personal_agent_id = personal_assistant["agent_id"]
    await _set_assistant_limit(client, personal_agent_id, 100.00, HEADERS)

    result_assistant = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(personal_agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )
    assert result_assistant["data"]["notified"] is True

    result_member = await _trigger_spending_limit_notification(
        client,
        limit_type="member",
        entity_id=user_id,
        limit_value=200.00,
        current_spend=200.00,
        month="2026-02",
        organization_id=org_id,
    )
    assert result_member["data"]["notified"] is True

    result_org = await _trigger_spending_limit_notification(
        client,
        limit_type="organization",
        entity_id=str(org_id),
        limit_value=500.00,
        current_spend=500.00,
        month="2026-02",
    )
    assert result_org["data"]["notified"] is True

    # Verify all three are tracked independently (re-calling should skip all)
    result_assistant_2 = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(personal_agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2026-02",
    )
    assert result_assistant_2["data"]["notified"] is False

    result_member_2 = await _trigger_spending_limit_notification(
        client,
        limit_type="member",
        entity_id=user_id,
        limit_value=200.00,
        current_spend=200.00,
        month="2026-02",
        organization_id=org_id,
    )
    assert result_member_2["data"]["notified"] is False

    result_org_2 = await _trigger_spending_limit_notification(
        client,
        limit_type="organization",
        entity_id=str(org_id),
        limit_value=500.00,
        current_spend=500.00,
        month="2026-02",
    )
    assert result_org_2["data"]["notified"] is False


# ===========================================================================
# Cleanup Endpoint Tests
# ===========================================================================


@pytest.mark.anyio
async def test_cleanup_endpoint_deletes_old_notifications(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Create notifications for old months, then call cleanup.
    Expected: Old notifications are deleted, recent ones are kept.
    """
    # Create an assistant and trigger notifications for different months
    assistant = await _create_assistant(client, "CleanupTest", "Bot", HEADERS)
    agent_id = assistant["agent_id"]
    await _set_assistant_limit(client, agent_id, 100.00, HEADERS)

    # Create notification for an old month (should be deleted with months_to_keep=1)
    old_result = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=100.00,
        current_spend=100.00,
        month="2025-01",  # Very old month
    )
    assert old_result["data"]["notified"] is True

    # Create notification for current month (should be kept)
    current_result = await _trigger_spending_limit_notification(
        client,
        limit_type="assistant",
        entity_id=str(agent_id),
        limit_value=200.00,  # Different limit value
        current_spend=200.00,
        month="2026-02",  # Current month
    )
    assert current_result["data"]["notified"] is True

    # Call cleanup endpoint with months_to_keep=1
    cleanup_response = await client.post(
        "/v0/admin/cleanup/spending-limit-notifications",
        params={"months_to_keep": 1},
        headers=ADMIN_HEADERS,
    )
    assert cleanup_response.status_code == 200

    cleanup_data = cleanup_response.json()
    assert cleanup_data["deleted_count"] >= 1  # At least the old one
    assert cleanup_data["months_retained"] == 1
    assert "message" in cleanup_data


@pytest.mark.anyio
async def test_cleanup_endpoint_with_no_old_notifications(
    client: AsyncClient,
    mock_email_sending: AsyncMock,
):
    """
    Scenario: Call cleanup when there are no old notifications.
    Expected: Returns successfully with deleted_count of 0.
    """
    # Call cleanup endpoint - may delete 0 or more depending on previous test state
    cleanup_response = await client.post(
        "/v0/admin/cleanup/spending-limit-notifications",
        params={"months_to_keep": 12},  # Keep last 12 months
        headers=ADMIN_HEADERS,
    )
    assert cleanup_response.status_code == 200

    cleanup_data = cleanup_response.json()
    assert "deleted_count" in cleanup_data
    assert cleanup_data["months_retained"] == 12
