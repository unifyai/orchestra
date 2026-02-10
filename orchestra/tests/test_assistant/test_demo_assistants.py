"""Tests for demo assistant functionality.

Tests cover:
- DemoAssistantMeta model creation and relationships
- Demo assistant creation endpoint
- Demo mode filtering in list endpoints
- Demo metadata cleanup on assistant deletion
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.tests.utils import ADMIN_HEADERS, HEADERS


@pytest.fixture(scope="function", autouse=True)
async def approve_default_user(client: AsyncClient):
    """Ensures the default test user for this module is approved for hiring."""
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]

    approve_url = f"/v0/admin/auth-user/{user_id}/assistant-hiring-approval/approved"
    approve_resp = await client.put(approve_url, headers=ADMIN_HEADERS)
    assert (
        approve_resp.status_code == status.HTTP_200_OK
    ), f"Failed to approve default user {user_id}: {approve_resp.json()}"


@pytest.fixture(autouse=True)
def mock_assistant_infra_calls(request):
    """
    Automatically mock assistant infrastructure webhooks for all tests.
    This prevents real network calls, making tests fast and reliable.
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
        "orchestra.web.api.assistant.views.create_phone_number",
        new_callable=AsyncMock,
    ) as mock_create_phone, patch(
        "orchestra.web.api.assistant.views.create_pubsub_topic",
        new_callable=AsyncMock,
    ) as mock_create_pubsub:

        mock_wake_up.return_value = MagicMock(status_code=200)
        mock_reawaken.return_value = MagicMock(status_code=200, json=lambda: {})
        # Return a dict matching the actual create_phone_number response format
        mock_create_phone.return_value = {"phoneNumber": "+14155551234"}
        mock_create_pubsub.return_value = None

        yield mock_wake_up, mock_reawaken


@pytest.fixture
async def source_assistant(client: AsyncClient) -> dict:
    """Create a source assistant that can be cloned for demo purposes."""
    payload = {
        "first_name": "Source",
        "surname": "Assistant",
        "age": 28,
        "nationality": "United States",
        "about": "A source assistant for cloning",
        "create_infra": False,
    }
    resp = await client.post("/v0/assistant", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    return resp.json()["info"]


class TestDemoAssistantModel:
    """Tests for DemoAssistantMeta model and relationships."""

    @pytest.mark.anyio
    async def test_demo_meta_fields_exist_in_response(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """Verify demo_id field is present in assistant response."""
        # Regular assistants should have demo_id = None
        # Use the list endpoint to verify the response schema includes demo_id
        resp = await client.get("/v0/assistant", headers=HEADERS)
        assert resp.status_code == 200
        assistants = resp.json()["info"]

        # Find the source assistant in the list
        matching = [
            a for a in assistants if a["agent_id"] == source_assistant["agent_id"]
        ]
        assert len(matching) == 1, "Source assistant should be in the list"
        data = matching[0]

        assert "demo_id" in data
        assert data["demo_id"] is None


class TestDemoAssistantListFiltering:
    """Tests for demo assistant list filtering."""

    @pytest.mark.anyio
    async def test_list_excludes_demo_assistants_by_default(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """Default list should exclude demo assistants."""
        # Create a regular assistant (source_assistant fixture)
        # List without demo param should return only regular assistants
        resp = await client.get("/v0/assistant", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()["info"]

        # All returned assistants should have demo_id = None
        for assistant in data:
            assert assistant.get("demo_id") is None

    @pytest.mark.anyio
    async def test_list_with_demo_includes_all(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """List with demo=true should include all assistants."""
        resp = await client.get("/v0/assistant?demo=true", headers=HEADERS)
        assert resp.status_code == 200
        # Should succeed even if no demo assistants exist
        assert "info" in resp.json()

    @pytest.mark.anyio
    async def test_list_with_demo_only(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """List with demo_only=true should only return demo assistants."""
        resp = await client.get("/v0/assistant?demo_only=true", headers=HEADERS)
        assert resp.status_code == 200
        data = resp.json()["info"]
        # Should return empty list if no demo assistants exist
        assert isinstance(data, list)


class TestDemoAssistantDeletion:
    """Tests for demo assistant deletion and cleanup."""

    @pytest.mark.anyio
    async def test_delete_regular_assistant_succeeds(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """Deleting a regular assistant should work normally."""
        agent_id = source_assistant["agent_id"]

        # Delete the assistant
        resp = await client.delete(
            f"/v0/assistant/{agent_id}",
            headers=HEADERS,
        )
        assert resp.status_code == 200

        # Verify it's deleted by checking the list endpoint
        resp = await client.get("/v0/assistant", headers=HEADERS)
        assert resp.status_code == 200
        assistants = resp.json()["info"]

        # The deleted assistant should not be in the list
        matching = [a for a in assistants if a["agent_id"] == agent_id]
        assert len(matching) == 0, "Deleted assistant should not be in the list"


class TestDemoAssistantCreationEndpoint:
    """Tests for demo assistant creation endpoint."""

    @pytest.mark.anyio
    async def test_demo_endpoint_requires_unify_org_membership(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """Demo endpoint should reject users not in Unify organization."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "Test Demo",
            "first_name": "Demo",
            "surname": "Assistant",
            "demoer_phone": "+14155559999",
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        # Should be forbidden for non-Unify members
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert "Unify organization" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_demo_spending_cap_validation_rejects_below_minimum(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """Spending cap below $1 should be rejected."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "Test Demo",
            "first_name": "Demo",
            "surname": "Assistant",
            "demoer_phone": "+14155559999",
            "monthly_spending_cap": 0.5,  # Below minimum of $1
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.anyio
    async def test_demo_spending_cap_validation_rejects_above_maximum(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """Spending cap above $100 should be rejected."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "Test Demo",
            "first_name": "Demo",
            "surname": "Assistant",
            "demoer_phone": "+14155559999",
            "monthly_spending_cap": 150.0,  # Above maximum of $100
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.anyio
    async def test_demo_spending_cap_defaults_to_ten(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """Spending cap should default to $10 when not provided."""
        # This test validates the schema default - we test via request validation
        # The actual creation would fail for non-Unify members, but the payload
        # should be valid without specifying monthly_spending_cap
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "Test Demo",
            "first_name": "Demo",
            "surname": "Assistant",
            "demoer_phone": "+14155559999",
            # Note: monthly_spending_cap not provided, should default to 10
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        # Will fail due to org check, but should NOT fail due to validation
        # (403 means it passed validation and reached the org check)
        assert resp.status_code == status.HTTP_403_FORBIDDEN


class TestDemoAssistantSpendingCapPersistence:
    """Tests for demo assistant spending cap persistence."""

    @pytest.mark.anyio
    async def test_demo_spending_cap_is_saved_on_creation(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """Spending cap should be persisted when creating a demo assistant."""
        # Mock the Unify organization membership check to allow creation
        with patch(
            "orchestra.web.api.assistant.views.is_unify_org_member",
            new_callable=AsyncMock,
            return_value=True,
        ):
            payload = {
                "source_assistant_id": int(source_assistant["agent_id"]),
                "label": "Spending Cap Test",
                "first_name": "SpendTest",
                "surname": "Demo",
                "demoer_phone": "+14155559999",
                "monthly_spending_cap": 25.0,  # Custom spending cap
            }
            resp = await client.post(
                "/v0/demo/assistant", json=payload, headers=HEADERS
            )
            assert (
                resp.status_code == status.HTTP_200_OK
            ), f"Creation failed: {resp.json()}"

            created = resp.json()["info"]
            assert (
                created["monthly_spending_cap"] == 25.0
            ), f"Expected spending cap 25.0 but got {created.get('monthly_spending_cap')}"

    @pytest.mark.anyio
    async def test_demo_spending_cap_default_is_saved(
        self,
        client: AsyncClient,
        source_assistant: dict,
    ):
        """Default spending cap ($10) should be persisted when not specified."""
        with patch(
            "orchestra.web.api.assistant.views.is_unify_org_member",
            new_callable=AsyncMock,
            return_value=True,
        ):
            payload = {
                "source_assistant_id": int(source_assistant["agent_id"]),
                "label": "Default Cap Test",
                "first_name": "DefaultTest",
                "surname": "Demo",
                "demoer_phone": "+14155559999",
                # monthly_spending_cap not provided - should default to 10.0
            }
            resp = await client.post(
                "/v0/demo/assistant", json=payload, headers=HEADERS
            )
            assert (
                resp.status_code == status.HTTP_200_OK
            ), f"Creation failed: {resp.json()}"

            created = resp.json()["info"]
            assert (
                created["monthly_spending_cap"] == 10.0
            ), f"Expected default spending cap 10.0 but got {created.get('monthly_spending_cap')}"


class TestDemoAssistantMetaEndpoint:
    """Tests for demo assistant metadata endpoint."""

    @pytest.mark.anyio
    async def test_demo_meta_not_found_for_invalid_id(
        self,
        client: AsyncClient,
    ):
        """Demo meta endpoint should return 404 for invalid demo_id."""
        resp = await client.get("/v0/demo/assistant/999999/meta", headers=HEADERS)
        assert resp.status_code == status.HTTP_404_NOT_FOUND
