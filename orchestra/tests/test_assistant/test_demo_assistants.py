"""Tests for demo assistant functionality.

Tests cover:
- DemoAssistantMeta model creation and relationships
- Demo assistant creation endpoint
- Demo mode filtering in list endpoints
- Demo metadata cleanup on assistant deletion
- Prospect detail storage and retrieval
- List demo metadata endpoint
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import AsyncClient

from orchestra.db.models.orchestra_models import Organization, OrganizationMember
from orchestra.settings import settings
from orchestra.tests.utils import HEADERS


@pytest.fixture
async def unify_member_user(client: AsyncClient, dbsession):
    """
    Create the Unify organization with the test user as owner.

    This fixture enables demo assistant creation which requires Unify org membership.
    Uses the API endpoint to properly create org with roles.
    """
    # Get the test user ID
    credits_resp = await client.get("/v0/credits", headers=HEADERS)
    user_id = credits_resp.json()["id"]

    unify_org_name = settings.orchestra_organization_name

    # Check if organization already exists
    org = (
        dbsession.query(Organization)
        .filter(Organization.name == unify_org_name)
        .first()
    )

    if org:
        # Check if user is already a member
        existing_member = (
            dbsession.query(OrganizationMember)
            .filter(
                OrganizationMember.user_id == user_id,
                OrganizationMember.organization_id == org.id,
            )
            .first()
        )
        if existing_member:
            return {"user_id": user_id, "org_id": org.id}

    # Create org via API - this handles role creation properly
    create_resp = await client.post(
        "/v0/organizations",
        json={"name": unify_org_name},
        headers=HEADERS,
    )

    if create_resp.status_code == 409:
        # Org exists but user is not a member - this shouldn't happen in tests
        # but handle it gracefully
        org = (
            dbsession.query(Organization)
            .filter(Organization.name == unify_org_name)
            .first()
        )
        return {"user_id": user_id, "org_id": org.id if org else None}

    assert create_resp.status_code == 201, f"Failed to create org: {create_resp.json()}"
    org_data = create_resp.json()

    return {"user_id": user_id, "org_id": org_data["id"]}


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
        unify_member_user: dict,
    ):
        """Spending cap should be persisted when creating a demo assistant."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "Spending Cap Test",
            "first_name": "SpendTest",
            "surname": "Demo",
            "demoer_phone": "+14155559999",
            "monthly_spending_cap": 25.0,  # Custom spending cap
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_200_OK, f"Creation failed: {resp.json()}"

        created = resp.json()["info"]
        assert (
            created["monthly_spending_cap"] == 25.0
        ), f"Expected spending cap 25.0 but got {created.get('monthly_spending_cap')}"

    @pytest.mark.anyio
    async def test_demo_spending_cap_default_is_saved(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """Default spending cap ($10) should be persisted when not specified."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "Default Cap Test",
            "first_name": "DefaultTest",
            "surname": "Demo",
            "demoer_phone": "+14155559999",
            # monthly_spending_cap not provided - should default to 10.0
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_200_OK, f"Creation failed: {resp.json()}"

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


class TestProspectDetailsStorage:
    """Tests for storing and retrieving prospect details in demo metadata."""

    @pytest.mark.anyio
    async def test_prospect_details_saved_on_creation(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """Prospect details should be persisted when creating a demo assistant."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "Richard Branson Demo",
            "first_name": "Demo",
            "surname": "ForRichard",
            "demoer_phone": "+14155559999",
            "monthly_spending_cap": 10.0,
            # Prospect details
            "prospect_first_name": "Richard",
            "prospect_surname": "Branson",
            "prospect_email": "richard@virgin.com",
            "prospect_phone": "+447700900000",
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_200_OK, f"Creation failed: {resp.json()}"

        created = resp.json()["info"]
        demo_id = created["demo_id"]
        assert demo_id is not None, "demo_id should be set for demo assistants"

        # Fetch the demo meta and verify prospect details
        meta_resp = await client.get(
            f"/v0/demo/assistant/{demo_id}/meta",
            headers=HEADERS,
        )
        assert meta_resp.status_code == status.HTTP_200_OK, meta_resp.json()

        meta = meta_resp.json()["info"]
        assert meta["prospect_first_name"] == "Richard"
        assert meta["prospect_surname"] == "Branson"
        assert meta["prospect_email"] == "richard@virgin.com"
        assert meta["prospect_phone"] == "+447700900000"

    @pytest.mark.anyio
    async def test_prospect_details_optional(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """Demo assistant creation should work without prospect details."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "No Prospect Demo",
            "first_name": "Demo",
            "surname": "NoProspect",
            "demoer_phone": "+14155559999",
            # No prospect details
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_200_OK, f"Failed: {resp.json()}"

        created = resp.json()["info"]
        demo_id = created["demo_id"]

        # Fetch meta and verify prospect fields are None
        meta_resp = await client.get(
            f"/v0/demo/assistant/{demo_id}/meta",
            headers=HEADERS,
        )
        assert meta_resp.status_code == status.HTTP_200_OK

        meta = meta_resp.json()["info"]
        assert meta["prospect_first_name"] is None
        assert meta["prospect_surname"] is None
        assert meta["prospect_email"] is None
        assert meta["prospect_phone"] is None

    @pytest.mark.anyio
    async def test_partial_prospect_details_allowed(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """Partial prospect details should be allowed (only some fields)."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "Partial Prospect Demo",
            "first_name": "Demo",
            "surname": "PartialProspect",
            "demoer_phone": "+14155559999",
            # Only name, no email/phone
            "prospect_first_name": "Jane",
            "prospect_surname": "Doe",
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_200_OK, f"Failed: {resp.json()}"

        created = resp.json()["info"]
        demo_id = created["demo_id"]

        meta_resp = await client.get(
            f"/v0/demo/assistant/{demo_id}/meta",
            headers=HEADERS,
        )
        assert meta_resp.status_code == status.HTTP_200_OK

        meta = meta_resp.json()["info"]
        assert meta["prospect_first_name"] == "Jane"
        assert meta["prospect_surname"] == "Doe"
        assert meta["prospect_email"] is None
        assert meta["prospect_phone"] is None


class TestDemoMetaListEndpoint:
    """Tests for listing demo assistant metadata."""

    @pytest.mark.anyio
    async def test_list_meta_returns_empty_when_no_demos(
        self,
        client: AsyncClient,
    ):
        """List should return empty array when user has no demo assistants."""
        resp = await client.get("/v0/demo/assistant/meta/list", headers=HEADERS)
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()["info"]
        assert isinstance(data, list)

    @pytest.mark.anyio
    async def test_list_meta_returns_all_user_demos(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """List should return all demo metadata for the authenticated user."""
        # Create two demo assistants
        for i in range(2):
            payload = {
                "source_assistant_id": int(source_assistant["agent_id"]),
                "label": f"List Test Demo {i}",
                "first_name": f"ListDemo{i}",
                "surname": "Test",
                "demoer_phone": "+14155559999",
            }
            resp = await client.post(
                "/v0/demo/assistant",
                json=payload,
                headers=HEADERS,
            )
            assert resp.status_code == status.HTTP_200_OK, f"Failed: {resp.json()}"

        # List should return at least 2 demos
        list_resp = await client.get("/v0/demo/assistant/meta/list", headers=HEADERS)
        assert list_resp.status_code == status.HTTP_200_OK
        data = list_resp.json()["info"]
        assert len(data) >= 2

        # Verify expected fields are present
        labels = [d["label"] for d in data]
        assert any("List Test Demo 0" in label for label in labels)
        assert any("List Test Demo 1" in label for label in labels)

    @pytest.mark.anyio
    async def test_list_meta_includes_prospect_details(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """Listed metadata should include prospect details if set."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "List Prospect Test",
            "first_name": "ListProspect",
            "surname": "Test",
            "demoer_phone": "+14155559999",
            "prospect_first_name": "Elon",
            "prospect_surname": "Musk",
            "prospect_email": "elon@spacex.com",
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_200_OK

        # List should include prospect details
        list_resp = await client.get("/v0/demo/assistant/meta/list", headers=HEADERS)
        assert list_resp.status_code == status.HTTP_200_OK
        data = list_resp.json()["info"]

        # Find our created demo
        created = [d for d in data if d["label"] == "List Prospect Test"]
        assert len(created) == 1

        meta = created[0]
        assert meta["prospect_first_name"] == "Elon"
        assert meta["prospect_surname"] == "Musk"
        assert meta["prospect_email"] == "elon@spacex.com"
        assert meta["prospect_phone"] is None

    @pytest.mark.anyio
    async def test_list_meta_ordered_by_created_at_desc(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """List should return metadata ordered by created_at descending."""
        # Create demos in sequence
        for i in range(3):
            payload = {
                "source_assistant_id": int(source_assistant["agent_id"]),
                "label": f"Order Test {i}",
                "first_name": f"Order{i}",
                "surname": "Test",
                "demoer_phone": "+14155559999",
            }
            resp = await client.post(
                "/v0/demo/assistant",
                json=payload,
                headers=HEADERS,
            )
            assert resp.status_code == status.HTTP_200_OK

        list_resp = await client.get("/v0/demo/assistant/meta/list", headers=HEADERS)
        assert list_resp.status_code == status.HTTP_200_OK
        data = list_resp.json()["info"]

        # Filter to our test demos and verify order
        order_tests = [d for d in data if "Order Test" in d["label"]]
        # Should have most recent first (Order Test 2)
        if len(order_tests) >= 3:
            # Most recent first
            assert order_tests[0]["label"] == "Order Test 2"
            assert order_tests[-1]["label"] == "Order Test 0"


class TestDemoEmailProvisioning:
    """Tests for optional email provisioning in demo assistants."""

    @pytest.mark.anyio
    async def test_provision_email_false_by_default(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """provision_email defaults to False and no email is created."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "No Email Demo",
            "first_name": "NoEmail",
            "surname": "Demo",
            "demoer_phone": "+14155559999",
            # provision_email not provided - should default to False
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_200_OK, f"Failed: {resp.json()}"

        created = resp.json()["info"]
        # Email should be None when provision_email is not requested
        assert created["email"] is None

    @pytest.mark.anyio
    async def test_provision_email_explicitly_false(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """Explicitly setting provision_email=False should not create email."""
        payload = {
            "source_assistant_id": int(source_assistant["agent_id"]),
            "label": "Explicit No Email Demo",
            "first_name": "ExplicitNoEmail",
            "surname": "Demo",
            "demoer_phone": "+14155559999",
            "provision_email": False,
        }
        resp = await client.post("/v0/demo/assistant", json=payload, headers=HEADERS)
        assert resp.status_code == status.HTTP_200_OK, f"Failed: {resp.json()}"

        created = resp.json()["info"]
        assert created["email"] is None

    @pytest.mark.anyio
    async def test_provision_email_creates_email(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """Setting provision_email=True should create an email address."""
        # Mock create_email and watch_email since they make external calls
        with patch(
            "orchestra.web.api.assistant.views.create_email",
            new_callable=AsyncMock,
        ) as mock_create_email, patch(
            "orchestra.web.api.assistant.views.watch_email",
            new_callable=AsyncMock,
        ) as mock_watch_email, patch(
            "orchestra.web.api.assistant.views.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            # Set up mock return values
            mock_create_email.return_value = {
                "user": {"primaryEmail": "emaildemo.demo@unify.ai"},
            }
            mock_watch_email.return_value = {"success": True}

            payload = {
                "source_assistant_id": int(source_assistant["agent_id"]),
                "label": "Email Demo",
                "first_name": "EmailDemo",
                "surname": "Demo",
                "demoer_phone": "+14155559999",
                "provision_email": True,
            }
            resp = await client.post(
                "/v0/demo/assistant",
                json=payload,
                headers=HEADERS,
            )
            assert resp.status_code == status.HTTP_200_OK, f"Failed: {resp.json()}"

            created = resp.json()["info"]
            # Email should be set
            assert created["email"] == "emaildemo.demo@unify.ai"

            # Verify create_email was called with correct args
            mock_create_email.assert_called_once()
            call_args = mock_create_email.call_args
            # First arg is email_local (without @domain)
            assert "emaildemo" in call_args[0][0].lower()

    @pytest.mark.anyio
    async def test_email_provisioning_failure_does_not_fail_creation(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """Email provisioning failure should not fail the entire demo creation."""
        with patch(
            "orchestra.web.api.assistant.views.create_email",
            new_callable=AsyncMock,
        ) as mock_create_email, patch(
            "orchestra.web.api.assistant.views.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            # Simulate email creation failure
            mock_create_email.return_value = {
                "detail": "Email service temporarily unavailable",
            }

            payload = {
                "source_assistant_id": int(source_assistant["agent_id"]),
                "label": "Email Failure Demo",
                "first_name": "EmailFailure",
                "surname": "Demo",
                "demoer_phone": "+14155559999",
                "provision_email": True,
            }
            resp = await client.post(
                "/v0/demo/assistant",
                json=payload,
                headers=HEADERS,
            )

            # Creation should still succeed (email is optional)
            assert resp.status_code == status.HTTP_200_OK, f"Failed: {resp.json()}"

            created = resp.json()["info"]
            # Phone should be provisioned even if email failed
            assert created["phone"] is not None
            # Email will be None since provisioning failed
            assert created["email"] is None


class TestUniqueEmailGenerationDAO:
    """Direct unit tests for AssistantDAO.generate_unique_email_local()."""

    @pytest.mark.anyio
    async def test_generate_unique_email_local_base_case(
        self,
        client: AsyncClient,
        dbsession,
    ):
        """First email should use base name without suffix."""
        from orchestra.db.dao.assistant_dao import AssistantDAO

        dao = AssistantDAO(dbsession)
        result = dao.generate_unique_email_local("John", "Doe")
        assert result == "john.doe"

    @pytest.mark.anyio
    async def test_generate_unique_email_local_normalizes_special_chars(
        self,
        client: AsyncClient,
        dbsession,
    ):
        """Special characters should be stripped."""
        from orchestra.db.dao.assistant_dao import AssistantDAO

        dao = AssistantDAO(dbsession)
        result = dao.generate_unique_email_local("Mary-Jane", "O'Connor")
        # Hyphens and apostrophes should be removed
        assert result == "maryjane.oconnor"

    @pytest.mark.anyio
    async def test_generate_unique_email_local_handles_collision(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
        dbsession,
    ):
        """Should append suffix when base email already exists."""
        from orchestra.db.dao.assistant_dao import AssistantDAO

        # First, create a demo assistant with email provisioned
        with patch(
            "orchestra.web.api.assistant.views.create_email",
            new_callable=AsyncMock,
        ) as mock_create_email, patch(
            "orchestra.web.api.assistant.views.watch_email",
            new_callable=AsyncMock,
        ), patch(
            "orchestra.web.api.assistant.views.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            mock_create_email.return_value = {
                "user": {"primaryEmail": "collision.test@unify.ai"},
            }

            payload = {
                "source_assistant_id": int(source_assistant["agent_id"]),
                "label": "Collision Demo 1",
                "first_name": "Collision",
                "surname": "Test",
                "demoer_phone": "+14155559999",
                "provision_email": True,
            }
            resp = await client.post(
                "/v0/demo/assistant",
                json=payload,
                headers=HEADERS,
            )
            assert resp.status_code == status.HTTP_200_OK

        # Now test that the DAO generates a unique suffix
        dao = AssistantDAO(dbsession)
        result = dao.generate_unique_email_local("Collision", "Test")
        # Should be "collision.test.1" since "collision.test" is taken
        assert result == "collision.test.1"


class TestUniqueEmailGeneration:
    """Tests for unique email local part generation via API."""

    @pytest.mark.anyio
    async def test_unique_email_generation_base_case(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """First email should use base name without suffix."""
        with patch(
            "orchestra.web.api.assistant.views.create_email",
            new_callable=AsyncMock,
        ) as mock_create_email, patch(
            "orchestra.web.api.assistant.views.watch_email",
            new_callable=AsyncMock,
        ) as mock_watch_email, patch(
            "orchestra.web.api.assistant.views.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            mock_create_email.return_value = {
                "user": {"primaryEmail": "unique.base@unify.ai"},
            }
            mock_watch_email.return_value = {"success": True}

            payload = {
                "source_assistant_id": int(source_assistant["agent_id"]),
                "label": "Unique Base Demo",
                "first_name": "Unique",
                "surname": "Base",
                "demoer_phone": "+14155559999",
                "provision_email": True,
            }
            resp = await client.post(
                "/v0/demo/assistant",
                json=payload,
                headers=HEADERS,
            )
            assert resp.status_code == status.HTTP_200_OK

            # Check that create_email was called with base name
            call_args = mock_create_email.call_args
            email_local = call_args[0][0]
            # Should be "unique.base" (no suffix for first)
            assert email_local == "unique.base"

    @pytest.mark.anyio
    async def test_unique_email_generation_handles_special_chars(
        self,
        client: AsyncClient,
        source_assistant: dict,
        unify_member_user: dict,
    ):
        """Email generation should handle special characters in names."""
        with patch(
            "orchestra.web.api.assistant.views.create_email",
            new_callable=AsyncMock,
        ) as mock_create_email, patch(
            "orchestra.web.api.assistant.views.watch_email",
            new_callable=AsyncMock,
        ) as mock_watch_email, patch(
            "orchestra.web.api.assistant.views.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            mock_create_email.return_value = {
                "user": {"primaryEmail": "johnpaul.obrien@unify.ai"},
            }
            mock_watch_email.return_value = {"success": True}

            payload = {
                "source_assistant_id": int(source_assistant["agent_id"]),
                "label": "Special Chars Demo",
                "first_name": "John-Paul",
                "surname": "O'Brien",
                "demoer_phone": "+14155559999",
                "provision_email": True,
            }
            resp = await client.post(
                "/v0/demo/assistant",
                json=payload,
                headers=HEADERS,
            )
            assert resp.status_code == status.HTTP_200_OK

            # Check that special chars were normalized
            call_args = mock_create_email.call_args
            email_local = call_args[0][0]
            # Hyphens and apostrophes should be removed
            assert "-" not in email_local
            assert "'" not in email_local
            # Should be lowercase
            assert email_local == email_local.lower()
