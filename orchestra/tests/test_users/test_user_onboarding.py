"""Tests for onboarding status tracking."""

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.onboarding_status_dao import OnboardingStatusDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_PROMO,
    Organization,
    Recharge,
)
from orchestra.tests.utils import ADMIN_HEADERS, create_test_org, create_test_user


class TestOnboardingStatusDAO:
    """Tests for OnboardingStatusDAO."""

    @pytest.mark.anyio
    async def test_create_onboarding_status(self, client: AsyncClient, dbsession):
        """Test creating onboarding status for a user."""
        # Create test user
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding@example.com", name="Test User")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding@example.com")[0][0]

        # Create onboarding status
        dao = OnboardingStatusDAO(dbsession)
        status = dao.create(user_id=user.id)
        dbsession.commit()

        assert status is not None
        assert status.user_id == user.id
        assert status.current_step == "heard_about"  # Initial step
        assert status.step_data == {}

    @pytest.mark.anyio
    async def test_create_with_initial_step(self, client: AsyncClient, dbsession):
        """Test creating onboarding status with initial step and data."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding2@example.com", name="Test User 2")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding2@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        status = dao.create(
            user_id=user.id,
            current_step="completed",
            step_data={"selected_type": "organization", "organization_id": "org_123"},
        )
        dbsession.commit()

        assert status.current_step == "completed"
        assert status.step_data["selected_type"] == "organization"
        assert status.step_data["organization_id"] == "org_123"

    @pytest.mark.anyio
    async def test_get_by_user_id(self, client: AsyncClient, dbsession):
        """Test retrieving onboarding status by user ID."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding3@example.com", name="Test User 3")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding3@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        dao.create(user_id=user.id, current_step="completed")
        dbsession.commit()

        # Retrieve
        status = dao.get_by_user_id(user.id)
        assert status is not None
        assert status.current_step == "completed"

    @pytest.mark.anyio
    async def test_get_by_user_id_not_found(self, client: AsyncClient, dbsession):
        """Test retrieving onboarding status for non-existent user."""
        dao = OnboardingStatusDAO(dbsession)
        status = dao.get_by_user_id("non_existent_user_id")
        assert status is None

    @pytest.mark.anyio
    async def test_get_or_create_creates_new(self, client: AsyncClient, dbsession):
        """Test get_or_create creates new status if none exists."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding4@example.com", name="Test User 4")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding4@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        status = dao.get_or_create(user.id)
        dbsession.commit()

        assert status is not None
        assert status.user_id == user.id
        assert status.current_step == "heard_about"

    @pytest.mark.anyio
    async def test_get_or_create_returns_existing(self, client: AsyncClient, dbsession):
        """Test get_or_create returns existing status."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding5@example.com", name="Test User 5")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding5@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        # Create first
        dao.create(user_id=user.id, current_step="completed")
        dbsession.commit()

        # get_or_create should return existing
        status = dao.get_or_create(user.id, current_step="workspace_setup")
        assert status.current_step == "completed"  # Not overwritten

    @pytest.mark.anyio
    async def test_update_onboarding_status(self, client: AsyncClient, dbsession):
        """Test updating onboarding status."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding6@example.com", name="Test User 6")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding6@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        dao.create(user_id=user.id)
        dbsession.commit()

        # Update - user completed workspace setup
        status = dao.update(
            user_id=user.id,
            current_step="completed",
            step_data={
                "selected_type": "organization",
                "organization_name": "Test Org",
            },
        )
        dbsession.commit()

        assert status.current_step == "completed"
        assert status.step_data["selected_type"] == "organization"
        assert status.step_data["organization_name"] == "Test Org"

    @pytest.mark.anyio
    async def test_update_partial(self, client: AsyncClient, dbsession):
        """Test partial update (only current_step)."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding7@example.com", name="Test User 7")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding7@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        dao.create(
            user_id=user.id,
            current_step="workspace_setup",
            step_data={"some_field": "value"},
        )
        dbsession.commit()

        # Update only step
        status = dao.update(user_id=user.id, current_step="completed")
        dbsession.commit()

        assert status.current_step == "completed"
        assert status.step_data == {"some_field": "value"}  # Unchanged

    @pytest.mark.anyio
    async def test_update_step_data_field(self, client: AsyncClient, dbsession):
        """Test updating a single field in step_data."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding8@example.com", name="Test User 8")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding8@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        dao.create(
            user_id=user.id,
            step_data={"selected_type": "organization", "organization_name": "Acme"},
        )
        dbsession.commit()

        # Update single field
        status = dao.update_step_data_field(user.id, "organization_id", "org_789")
        dbsession.commit()

        assert status.step_data["selected_type"] == "organization"  # Unchanged
        assert status.step_data["organization_name"] == "Acme"  # Unchanged
        assert status.step_data["organization_id"] == "org_789"  # Added

    @pytest.mark.anyio
    async def test_mark_completed(self, client: AsyncClient, dbsession):
        """Test marking onboarding as completed."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding9@example.com", name="Test User 9")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding9@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        dao.create(user_id=user.id)
        dbsession.commit()

        status = dao.mark_completed(user.id)
        dbsession.commit()

        assert status.current_step == "completed"
        assert "completed_at" in status.step_data

    @pytest.mark.anyio
    async def test_delete(self, client: AsyncClient, dbsession):
        """Test deleting onboarding status."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding10@example.com", name="Test User 10")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding10@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        dao.create(user_id=user.id)
        dbsession.commit()

        # Delete
        result = dao.delete(user.id)
        dbsession.commit()

        assert result is True
        assert dao.get_by_user_id(user.id) is None

    @pytest.mark.anyio
    async def test_delete_not_found(self, client: AsyncClient, dbsession):
        """Test deleting non-existent onboarding status."""
        dao = OnboardingStatusDAO(dbsession)
        result = dao.delete("non_existent_user_id")
        assert result is False

    @pytest.mark.anyio
    async def test_reset(self, client: AsyncClient, dbsession):
        """Test resetting onboarding status."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding11@example.com", name="Test User 11")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding11@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        dao.create(
            user_id=user.id,
            current_step="completed",
            step_data={"completed_at": "2026-01-01", "selected_type": "personal"},
        )
        dbsession.commit()

        # Reset
        status = dao.reset(user.id)
        dbsession.commit()

        assert status.current_step == "heard_about"
        assert status.step_data == {}

    @pytest.mark.anyio
    async def test_reset_creates_if_not_exists(self, client: AsyncClient, dbsession):
        """Test reset creates new status if none exists."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding12@example.com", name="Test User 12")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding12@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        status = dao.reset(user.id)
        dbsession.commit()

        assert status is not None
        assert status.current_step == "heard_about"


class TestOnboardingSurvivesAccountFreeze:
    """A frozen account must still be able to finish onboarding.

    The console pins any user whose onboarding is incomplete to
    /login/onboarding. If the freeze gate also blocks the onboarding
    endpoints, such a user can neither finish signup nor reach the card
    page that would lift the freeze — a closed loop with no exit. The
    card-gate sweep put ~350 real accounts into exactly that state.
    """

    async def _freeze(self, client: AsyncClient, user_id: str) -> None:
        resp = await client.post(
            "/v0/admin/billing/freeze",
            params={"freeze": True, "user_id": user_id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text

    @pytest.mark.anyio
    async def test_frozen_account_is_really_frozen(self, client: AsyncClient):
        """Guard against the exemption tests passing vacuously.

        If the freeze never took effect in the test environment, the
        tests below would pass whether or not the exemption exists.
        """
        user = await create_test_user(client, "onboarding_frozen_ctl@example.com")
        await self._freeze(client, user["id"])

        # A non-exempt endpoint on the same router must be blocked.
        resp = await client.get("/v0/user/basic-info", headers=user["headers"])
        assert resp.status_code == 403, resp.text

    @pytest.mark.anyio
    async def test_frozen_account_can_read_onboarding(self, client: AsyncClient):
        user = await create_test_user(client, "onboarding_frozen_get@example.com")
        await self._freeze(client, user["id"])

        resp = await client.get("/v0/user/onboarding", headers=user["headers"])
        assert resp.status_code == 200, resp.text

    @pytest.mark.anyio
    async def test_frozen_account_can_advance_onboarding(self, client: AsyncClient):
        """The exact call that 403'd for every deadlocked user."""
        user = await create_test_user(client, "onboarding_frozen_put@example.com")
        await self._freeze(client, user["id"])

        resp = await client.put(
            "/v0/user/onboarding",
            headers=user["headers"],
            json={
                "current_step": "workspace_setup",
                "step_data": {"heard_about": "friend"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["current_step"] == "workspace_setup"

    @pytest.mark.anyio
    async def test_frozen_account_can_complete_onboarding(self, client: AsyncClient):
        """Completing onboarding is what releases the console redirect."""
        user = await create_test_user(client, "onboarding_frozen_done@example.com")
        await self._freeze(client, user["id"])

        resp = await client.put(
            "/v0/user/onboarding",
            headers=user["headers"],
            json={
                "current_step": "completed",
                "step_data": {"selected_type": "personal"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["current_step"] == "completed"

        # The gate still applies everywhere else, so the user lands on
        # the card-required prompt rather than free platform access.
        blocked = await client.get("/v0/user/basic-info", headers=user["headers"])
        assert blocked.status_code == 403, blocked.text


class TestOnboardingStatusAPI:
    """Tests for onboarding status API endpoints."""

    @pytest.mark.anyio
    async def test_get_onboarding_progress(self, client: AsyncClient):
        """Test getting onboarding progress."""
        test_user = await create_test_user(client, "onboarding_api1@example.com")

        response = await client.get(
            "/v0/user/onboarding",
            headers=test_user["headers"],
        )
        assert response.status_code == 200

        data = response.json()
        assert "user_id" in data
        assert "current_step" in data
        assert "step_data" in data
        assert "created_at" in data

    @pytest.mark.anyio
    async def test_get_onboarding_creates_if_not_exists(self, client: AsyncClient):
        """Test that GET creates onboarding status if none exists."""
        test_user = await create_test_user(client, "onboarding_api2@example.com")

        response = await client.get(
            "/v0/user/onboarding",
            headers=test_user["headers"],
        )
        assert response.status_code == 200

        data = response.json()
        assert data["current_step"] == "heard_about"  # Initial state

    @pytest.mark.anyio
    async def test_update_after_workspace_setup(self, client: AsyncClient):
        """Test updating after completing workspace setup."""
        test_user = await create_test_user(client, "onboarding_api3@example.com")

        # First get to create
        await client.get("/v0/user/onboarding", headers=test_user["headers"])

        # User completed workspace setup, choosing organization
        response = await client.put(
            "/v0/user/onboarding",
            headers=test_user["headers"],
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "organization",
                    "organization_id": "org_123",
                    "organization_name": "Test Corp",
                },
            },
        )
        assert response.status_code == 200

        data = response.json()
        assert data["current_step"] == "completed"
        assert data["step_data"]["selected_type"] == "organization"
        assert data["step_data"]["organization_id"] == "org_123"

    @pytest.mark.anyio
    async def test_update_to_completed_derives_onboarded(self, client: AsyncClient):
        """Test that completing onboarding is reflected in the legacy endpoint."""
        test_user = await create_test_user(client, "onboarding_api4@example.com")

        # Update to completed
        response = await client.put(
            "/v0/user/onboarding",
            headers=test_user["headers"],
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "personal",
                },
            },
        )
        assert response.status_code == 200

        # Check legacy endpoint derives onboarded from OnboardingStatus
        legacy_response = await client.get(
            "/v0/user/onboarding-status",
            headers=test_user["headers"],
        )
        assert legacy_response.status_code == 200
        assert legacy_response.json()["onboarded"] is True

    @pytest.mark.anyio
    async def test_reset_onboarding_progress(self, client: AsyncClient):
        """Test resetting onboarding progress."""
        test_user = await create_test_user(client, "onboarding_api5@example.com")

        # Set to completed first
        await client.put(
            "/v0/user/onboarding",
            headers=test_user["headers"],
            json={"current_step": "completed"},
        )

        # Reset
        response = await client.delete(
            "/v0/user/onboarding",
            headers=test_user["headers"],
        )
        assert response.status_code == 200

        data = response.json()
        assert data["current_step"] == "heard_about"

        # Check legacy endpoint derives onboarded from OnboardingStatus
        legacy_response = await client.get(
            "/v0/user/onboarding-status",
            headers=test_user["headers"],
        )
        assert legacy_response.json()["onboarded"] is False

    @pytest.mark.anyio
    async def test_invalid_step_rejected(self, client: AsyncClient):
        """Test that invalid steps are rejected."""
        test_user = await create_test_user(client, "onboarding_api6@example.com")

        response = await client.put(
            "/v0/user/onboarding",
            headers=test_user["headers"],
            json={"current_step": "invalid_step"},
        )
        assert response.status_code == 422  # Validation error

    @pytest.mark.anyio
    async def test_step_data_in_completed(self, client: AsyncClient):
        """Test that step_data is stored when completing onboarding."""
        test_user = await create_test_user(client, "onboarding_api7@example.com")

        # Complete workspace setup → completed
        response = await client.put(
            "/v0/user/onboarding",
            headers=test_user["headers"],
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "organization",
                    "organization_id": "org_456",
                    "organization_name": "Acme Inc",
                },
            },
        )
        assert response.status_code == 200

        data = response.json()
        assert data["step_data"]["selected_type"] == "organization"
        assert data["step_data"]["organization_id"] == "org_456"
        assert data["step_data"]["organization_name"] == "Acme Inc"


class TestOnboardingStepProgression:
    """Tests for typical onboarding step progressions."""

    @pytest.mark.anyio
    async def test_personal_workspace_flow(self, client: AsyncClient):
        """Test complete personal workspace onboarding flow."""
        test_user = await create_test_user(client, "onboarding_flow1@example.com")
        headers = test_user["headers"]

        # Step 1: Get initial status
        response = await client.get("/v0/user/onboarding", headers=headers)
        assert response.json()["current_step"] == "heard_about"

        # Step 2: Complete acquisition survey → workspace_setup
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "workspace_setup",
                "step_data": {
                    "heard_about": "search",
                    "heard_about_detail": "Google",
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "workspace_setup"
        assert response.json()["step_data"]["heard_about"] == "search"

        # Step 3: Complete workspace setup (personal) → completed
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {"selected_type": "personal"},
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "completed"
        # Earlier survey fields are preserved across step updates.
        assert response.json()["step_data"]["heard_about"] == "search"
        assert response.json()["step_data"]["selected_type"] == "personal"

    @pytest.mark.anyio
    async def test_organization_workspace_flow(self, client: AsyncClient):
        """Test complete organization workspace onboarding flow."""
        test_user = await create_test_user(client, "onboarding_flow2@example.com")
        headers = test_user["headers"]

        # Step 1: Complete workspace setup (organization) → completed
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "organization",
                    "organization_id": "org_test_123",
                    "organization_name": "Test Corp",
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "completed"

    @pytest.mark.anyio
    async def test_resume_onboarding(self, client: AsyncClient):
        """Test resuming onboarding from heard_about step."""
        test_user = await create_test_user(client, "onboarding_flow3@example.com")
        headers = test_user["headers"]

        # User starts but doesn't complete — heard_about is the initial step
        response = await client.get("/v0/user/onboarding", headers=headers)
        assert response.status_code == 200

        data = response.json()
        # Should be at heard_about (initial state)
        assert data["current_step"] == "heard_about"


# ============================================================================
# E2E Full User Onboarding Flows
# ============================================================================


class TestE2EUserOnboardingFlows:
    """
    End-to-end tests for complete user onboarding flows.

    These tests simulate real user journeys through the simplified onboarding
    process (heard_about → workspace_setup → completed).
    """

    @pytest.mark.anyio
    async def test_e2e_personal_workspace_direct_signup(
        self,
        client: AsyncClient,
    ):
        """
        E2E Test: Direct signup choosing personal workspace.

        Flow:
        1. User signs up (creates account)
        2. Answers acquisition survey
        3. Selects "Personal" in workspace setup
        4. Completes onboarding
        """
        user = await create_test_user(client, "e2e_personal@example.com")
        headers = user["headers"]

        # Verify initial onboarding state
        response = await client.get("/v0/user/onboarding", headers=headers)
        assert response.status_code == 200
        assert response.json()["current_step"] == "heard_about"

        # Complete acquisition survey → workspace_setup
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "workspace_setup",
                "step_data": {"heard_about": "friend"},
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "workspace_setup"

        # Complete workspace setup → completed
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {"selected_type": "personal"},
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "completed"
        assert response.json()["step_data"]["heard_about"] == "friend"

        # Verify user is marked as onboarded
        legacy = await client.get("/v0/user/onboarding-status", headers=headers)
        assert legacy.json()["onboarded"] is True

    @pytest.mark.anyio
    async def test_e2e_organization_workspace_direct_signup(
        self,
        client: AsyncClient,
    ):
        """
        E2E Test: Direct signup choosing organization workspace.

        Flow:
        1. User signs up
        2. Selects "For my team" → creates organization
        3. Completes onboarding
        """
        user = await create_test_user(client, "e2e_org_full@example.com")
        headers = user["headers"]

        # Create organization
        org_response = await client.post(
            "/v0/organizations",
            headers=headers,
            json={"name": "E2E Org Corp"},
        )
        assert org_response.status_code == 201
        org_id = str(org_response.json()["id"])

        # Complete workspace setup with organization → completed
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "organization",
                    "organization_id": org_id,
                    "organization_name": "E2E Org Corp",
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "completed"

        # Verify organization was created and user is owner
        org = await client.get(f"/v0/organizations/{org_id}", headers=headers)
        assert org.status_code == 200
        assert org.json()["name"] == "E2E Org Corp"

        # Verify user is marked as onboarded
        legacy = await client.get("/v0/user/onboarding-status", headers=headers)
        assert legacy.json()["onboarded"] is True

    @pytest.mark.anyio
    async def test_e2e_user_can_restart_onboarding(self, client: AsyncClient):
        """
        E2E Test: User can reset and restart their onboarding.

        Useful for users who made a mistake in workspace type selection.
        """
        user = await create_test_user(client, "e2e_restart@example.com")
        headers = user["headers"]

        # Complete onboarding as personal
        await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {"selected_type": "personal"},
            },
        )

        # User decides they want to switch to organization
        # Reset onboarding
        reset_response = await client.delete("/v0/user/onboarding", headers=headers)
        assert reset_response.status_code == 200
        assert reset_response.json()["current_step"] == "heard_about"

        # Start fresh
        response = await client.get("/v0/user/onboarding", headers=headers)
        assert response.json()["current_step"] == "heard_about"
        # After reset, step_data should have cleared user-set values
        step_data = response.json()["step_data"]
        assert step_data.get("selected_type") is None
        assert step_data.get("organization_id") is None
        assert step_data.get("heard_about") is None

    @pytest.mark.anyio
    async def test_e2e_onboarding_state_consistency(self, client: AsyncClient):
        """
        E2E Test: Onboarding state remains consistent across API calls.
        """
        user = await create_test_user(client, "e2e_consistency@example.com")
        headers = user["headers"]

        # Set state to completed with custom data
        await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "personal",
                    "custom_field": "test_value",
                },
            },
        )

        # Get state multiple times — should be consistent
        for _ in range(3):
            response = await client.get("/v0/user/onboarding", headers=headers)
            assert response.json()["current_step"] == "completed"
            assert response.json()["step_data"]["custom_field"] == "test_value"

    @pytest.mark.anyio
    async def test_e2e_personal_user_later_creates_org(self, client: AsyncClient):
        """
        E2E Test: Personal user completes onboarding, then later creates org.

        Flow:
        1. User signs up as personal
        2. Completes onboarding
        3. Later creates an organization from settings
        """
        user = await create_test_user(client, "e2e_later_org@example.com")
        headers = user["headers"]

        # Complete as personal
        await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {"selected_type": "personal"},
            },
        )

        # Later, create organization
        org_response = await client.post(
            "/v0/organizations",
            headers=headers,
            json={"name": "Later Org"},
        )
        assert org_response.status_code == 201
        org_id = org_response.json()["id"]

        # Verify user is owner
        org = await client.get(f"/v0/organizations/{org_id}", headers=headers)
        assert org.json()["owner_id"] == user["id"]

        # User's onboarding status should remain completed
        onboarding = await client.get("/v0/user/onboarding", headers=headers)
        assert onboarding.json()["current_step"] == "completed"


# ============================================================================
# Signup Credit Grant
# ============================================================================


class TestSignupCreditGrant:
    """
    Tests that promo credits are available as soon as account records exist.
    """

    def _promo_recharges(
        self,
        dbsession: Session,
        billing_account_id: int,
    ) -> list[Recharge]:
        return (
            dbsession.query(Recharge)
            .filter_by(
                billing_account_id=billing_account_id,
                type=RECHARGE_TYPE_PROMO,
            )
            .all()
        )

    @pytest.mark.anyio
    async def test_user_creation_grants_signup_credits_to_user(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """A created user can spend credits before onboarding is completed."""
        from orchestra.settings import settings

        user = await create_test_user(client, "signup-credit-user@unify.ai")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])

        assert float(db_user.billing_account.credits) == settings.signup_credit_grant
        promos = self._promo_recharges(dbsession, db_user.billing_account_id)
        assert len(promos) == 1
        assert float(promos[0].quantity) == settings.signup_credit_grant
        assert float(promos[0].amount_usd) == 0

    @pytest.mark.anyio
    async def test_org_creation_does_not_grant_signup_credits(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """The signup promo is per person, so a new organization starts empty.

        Granting per billing account made the promo re-mintable without
        limit: create an organization, spend its balance, delete it, repeat.
        The owner keeps the one grant on their own account.
        """
        from orchestra.settings import settings

        user = await create_test_user(client, "signup-credit-org-owner@unify.ai")
        org = await create_test_org(client, user, "SignupCreditOrg")

        db_org = dbsession.query(Organization).filter_by(id=org["id"]).first()

        assert float(db_org.billing_account.credits) == 0
        assert self._promo_recharges(dbsession, db_org.billing_account_id) == []

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        assert float(db_user.billing_account.credits) == settings.signup_credit_grant

    @pytest.mark.anyio
    async def test_a_second_organization_is_refused(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """One organization per user, and the refusal mints nothing."""
        from orchestra.settings import settings

        user = await create_test_user(client, "org-loop-owner@unify.ai")

        org = await create_test_org(client, user, "LoopOrg0")
        db_org = dbsession.query(Organization).filter_by(id=org["id"]).first()
        assert float(db_org.billing_account.credits) == 0

        response = await client.post(
            "/v0/organizations",
            json={"name": "LoopOrg1"},
            headers=user["headers"],
        )
        assert response.status_code == 403

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        assert float(db_user.billing_account.credits) == settings.signup_credit_grant

    @pytest.mark.anyio
    async def test_unify_members_are_exempt_from_the_org_cap(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Staff provision customer orgs, so Unify members may own several."""
        from orchestra.services.personal_workspace_service import (
            UNIFY_ORGANIZATION_NAME,
        )

        staff = await create_test_user(client, "staff-org-cap@unify.ai")
        await create_test_org(client, staff, UNIFY_ORGANIZATION_NAME)

        second = await create_test_org(client, staff, "White Glove Client Org")
        assert second["id"]

    @pytest.mark.anyio
    async def test_personal_onboarding_does_not_change_signup_grant(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Choosing 'personal' records onboarding state only."""
        from orchestra.settings import settings

        user = await create_test_user(client, "signup-credit-personal@unify.ai")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        initial_billing_account_id = db_user.billing_account_id

        assert float(db_user.billing_account.credits) == settings.signup_credit_grant
        assert len(self._promo_recharges(dbsession, initial_billing_account_id)) == 1

        resp = await client.put(
            "/v0/user/onboarding",
            headers=user["headers"],
            json={
                "current_step": "completed",
                "step_data": {"selected_type": "personal"},
            },
        )
        assert resp.status_code == 200

        dbsession.expire_all()
        db_user = user_dao.get_user_with_id(user["id"])
        assert float(db_user.billing_account.credits) == settings.signup_credit_grant
        assert len(self._promo_recharges(dbsession, initial_billing_account_id)) == 1

    @pytest.mark.anyio
    async def test_org_onboarding_does_not_change_signup_grant(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Choosing 'organization' records onboarding state only."""
        from orchestra.settings import settings

        user = await create_test_user(client, "signup-credit-org@unify.ai")
        org = await create_test_org(client, user, "SignupCreditOrg")
        org_id = org["id"]

        db_org = dbsession.query(Organization).filter_by(id=org_id).first()
        org_ba_id = db_org.billing_account_id

        assert float(db_org.billing_account.credits) == settings.signup_credit_grant
        assert len(self._promo_recharges(dbsession, org_ba_id)) == 1

        resp = await client.put(
            "/v0/user/onboarding",
            headers=user["headers"],
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "organization",
                    "organization_id": str(org_id),
                    "organization_name": "SignupCreditOrg",
                },
            },
        )
        assert resp.status_code == 200

        dbsession.expire_all()
        db_org = dbsession.query(Organization).filter_by(id=org_id).first()
        assert float(db_org.billing_account.credits) == settings.signup_credit_grant
        assert len(self._promo_recharges(dbsession, org_ba_id)) == 1

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        assert float(db_user.billing_account.credits) == settings.signup_credit_grant

    @pytest.mark.anyio
    async def test_idempotent_onboarding_no_double_grant(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Completing onboarding twice does not mutate signup funding."""
        from orchestra.settings import settings

        user = await create_test_user(client, "signup-credit-idempotent@unify.ai")

        for _ in range(2):
            resp = await client.put(
                "/v0/user/onboarding",
                headers=user["headers"],
                json={
                    "current_step": "completed",
                    "step_data": {"selected_type": "personal"},
                },
            )
            assert resp.status_code == 200

        dbsession.expire_all()
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        assert float(db_user.billing_account.credits) == settings.signup_credit_grant
        assert len(self._promo_recharges(dbsession, db_user.billing_account_id)) == 1

    @pytest.mark.anyio
    async def test_org_onboarding_not_granted_per_member(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Members completing onboarding do not add org promo credits."""
        from orchestra.settings import settings

        owner = await create_test_user(client, "org-owner-credit@unify.ai")
        org = await create_test_org(client, owner, "MultiMemberCreditOrg")
        org_id = org["id"]

        await client.put(
            "/v0/user/onboarding",
            headers=owner["headers"],
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "organization",
                    "organization_id": str(org_id),
                },
            },
        )

        dbsession.expire_all()
        db_org = dbsession.query(Organization).filter_by(id=org_id).first()
        assert float(db_org.billing_account.credits) == settings.signup_credit_grant

        member = await create_test_user(client, "org-member-credit@unify.ai")
        await client.put(
            "/v0/user/onboarding",
            headers=member["headers"],
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "organization",
                    "organization_id": str(org_id),
                },
            },
        )

        dbsession.expire_all()
        db_org = dbsession.query(Organization).filter_by(id=org_id).first()
        assert float(db_org.billing_account.credits) == settings.signup_credit_grant
        assert len(self._promo_recharges(dbsession, db_org.billing_account_id)) == 1

    @pytest.mark.anyio
    async def test_completed_onboarding_without_step_data_keeps_signup_grant(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Completing onboarding without step_data does not mutate credits."""
        from orchestra.settings import settings

        user = await create_test_user(client, "signup-credit-nostep@unify.ai")

        resp = await client.put(
            "/v0/user/onboarding",
            headers=user["headers"],
            json={"current_step": "completed"},
        )
        assert resp.status_code == 200

        dbsession.expire_all()
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        assert float(db_user.billing_account.credits) == settings.signup_credit_grant
        assert len(self._promo_recharges(dbsession, db_user.billing_account_id)) == 1
