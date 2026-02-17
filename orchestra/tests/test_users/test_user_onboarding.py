"""Tests for onboarding status tracking."""

import pytest
from httpx import AsyncClient

from orchestra.db.dao.onboarding_status_dao import OnboardingStatusDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.tests.utils import create_test_user


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
        assert status.current_step == "account_setup"  # Initial step
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
            current_step="billing_setup",
            step_data={"selected_type": "business", "organization_id": "org_123"},
        )
        dbsession.commit()

        assert status.current_step == "billing_setup"
        assert status.step_data["selected_type"] == "business"
        assert status.step_data["organization_id"] == "org_123"

    @pytest.mark.anyio
    async def test_get_by_user_id(self, client: AsyncClient, dbsession):
        """Test retrieving onboarding status by user ID."""
        user_dao = UserDAO(dbsession)
        user_dao.create(email="test_onboarding3@example.com", name="Test User 3")
        dbsession.commit()

        user = user_dao.filter(email="test_onboarding3@example.com")[0][0]

        dao = OnboardingStatusDAO(dbsession)
        dao.create(user_id=user.id, current_step="billing_setup")
        dbsession.commit()

        # Retrieve
        status = dao.get_by_user_id(user.id)
        assert status is not None
        assert status.current_step == "billing_setup"

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
        assert status.current_step == "account_setup"

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
        status = dao.get_or_create(user.id, current_step="account_setup")
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

        # Update - user completed account setup, moving to billing
        status = dao.update(
            user_id=user.id,
            current_step="billing_setup",
            step_data={"selected_type": "business", "organization_name": "Test Org"},
        )
        dbsession.commit()

        assert status.current_step == "billing_setup"
        assert status.step_data["selected_type"] == "business"
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
            current_step="account_setup",
            step_data={"some_field": "value"},
        )
        dbsession.commit()

        # Update only step
        status = dao.update(user_id=user.id, current_step="billing_setup")
        dbsession.commit()

        assert status.current_step == "billing_setup"
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
            step_data={"selected_type": "business", "organization_name": "Acme"},
        )
        dbsession.commit()

        # Update single field - add billing info
        status = dao.update_step_data_field(user.id, "payment_method_added", True)
        dbsession.commit()

        assert status.step_data["selected_type"] == "business"  # Unchanged
        assert status.step_data["organization_name"] == "Acme"  # Unchanged
        assert status.step_data["payment_method_added"] is True  # Added

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

        assert status.current_step == "account_setup"
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
        assert status.current_step == "account_setup"


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
        assert data["current_step"] == "account_setup"  # Initial state

    @pytest.mark.anyio
    async def test_update_after_account_setup(self, client: AsyncClient):
        """Test updating after completing account setup."""
        test_user = await create_test_user(client, "onboarding_api3@example.com")

        # First get to create
        await client.get("/v0/user/onboarding", headers=test_user["headers"])

        # User completed account setup, moving to billing
        response = await client.put(
            "/v0/user/onboarding",
            headers=test_user["headers"],
            json={
                "current_step": "billing_setup",
                "step_data": {
                    "selected_type": "business",
                    "organization_id": "org_123",
                    "organization_name": "Test Corp",
                },
            },
        )
        assert response.status_code == 200

        data = response.json()
        assert data["current_step"] == "billing_setup"
        assert data["step_data"]["selected_type"] == "business"
        assert data["step_data"]["organization_id"] == "org_123"

    @pytest.mark.anyio
    async def test_update_to_completed_sets_onboarded_flag(self, client: AsyncClient):
        """Test that completing onboarding sets the legacy onboarded flag."""
        test_user = await create_test_user(client, "onboarding_api4@example.com")

        # Update to completed
        response = await client.put(
            "/v0/user/onboarding",
            headers=test_user["headers"],
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "personal",
                    "billing_skipped": True,
                },
            },
        )
        assert response.status_code == 200

        # Check legacy endpoint
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
        assert data["current_step"] == "account_setup"

        # Check legacy flag is also reset
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
    async def test_accumulated_step_data(self, client: AsyncClient):
        """Test that step_data accumulates as user progresses."""
        test_user = await create_test_user(client, "onboarding_api7@example.com")

        # Complete account setup
        response = await client.put(
            "/v0/user/onboarding",
            headers=test_user["headers"],
            json={
                "current_step": "billing_setup",
                "step_data": {
                    "selected_type": "business",
                    "organization_id": "org_456",
                    "organization_name": "Acme Inc",
                    "business_name": "Acme Incorporated",
                },
            },
        )
        assert response.status_code == 200

        # Complete billing setup
        response = await client.put(
            "/v0/user/onboarding",
            headers=test_user["headers"],
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "business",
                    "organization_id": "org_456",
                    "organization_name": "Acme Inc",
                    "business_name": "Acme Incorporated",
                    "payment_method_added": True,
                    "billing_skipped": False,
                },
            },
        )
        assert response.status_code == 200

        data = response.json()
        # All accumulated data should be present
        assert data["step_data"]["selected_type"] == "business"
        assert data["step_data"]["organization_id"] == "org_456"
        assert data["step_data"]["payment_method_added"] is True
        assert data["step_data"]["billing_skipped"] is False


class TestOnboardingStepProgression:
    """Tests for typical onboarding step progressions."""

    @pytest.mark.anyio
    async def test_personal_account_flow(self, client: AsyncClient):
        """Test complete personal account onboarding flow."""
        test_user = await create_test_user(client, "onboarding_flow1@example.com")
        headers = test_user["headers"]

        # Step 1: Get initial status
        response = await client.get("/v0/user/onboarding", headers=headers)
        assert response.json()["current_step"] == "account_setup"

        # Step 2: Complete account setup (personal)
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "billing_setup",
                "step_data": {"selected_type": "personal"},
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "billing_setup"

        # Step 3: Complete billing (skipped)
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "personal",
                    "billing_skipped": True,
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "completed"

    @pytest.mark.anyio
    async def test_business_account_flow(self, client: AsyncClient):
        """Test complete business account onboarding flow."""
        test_user = await create_test_user(client, "onboarding_flow2@example.com")
        headers = test_user["headers"]

        # Step 1: Complete account setup (business with org)
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "billing_setup",
                "step_data": {
                    "selected_type": "business",
                    "organization_id": "org_test_123",
                    "organization_name": "Test Corp",
                    "business_name": "Test Corporation LLC",
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "billing_setup"

        # Step 2: Complete billing setup
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "business",
                    "organization_id": "org_test_123",
                    "organization_name": "Test Corp",
                    "business_name": "Test Corporation LLC",
                    "payment_method_added": True,
                    "billing_skipped": False,
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "completed"

    @pytest.mark.anyio
    async def test_resume_onboarding(self, client: AsyncClient):
        """Test resuming onboarding from where user left off."""
        test_user = await create_test_user(client, "onboarding_flow3@example.com")
        headers = test_user["headers"]

        # User completed account setup but left before billing
        await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "billing_setup",
                "step_data": {
                    "selected_type": "business",
                    "organization_name": "Half Finished Corp",
                },
            },
        )

        # Later, user returns and gets their progress
        response = await client.get("/v0/user/onboarding", headers=headers)
        assert response.status_code == 200

        data = response.json()
        # Should resume at billing_setup
        assert data["current_step"] == "billing_setup"
        # Should have accumulated data from account setup
        assert data["step_data"]["selected_type"] == "business"
        assert data["step_data"]["organization_name"] == "Half Finished Corp"


# ============================================================================
# E2E Full User Onboarding Flows
# ============================================================================


class TestE2EUserOnboardingFlows:
    """
    End-to-end tests for complete user onboarding flows.

    These tests simulate real user journeys through the onboarding process,
    covering both personal and business paths as defined in the design document.
    """

    @pytest.mark.anyio
    async def test_e2e_path_a_personal_direct_signup_with_billing(
        self,
        client: AsyncClient,
    ):
        """
        E2E Test: Path A - Direct signup as personal user with billing setup.

        Flow:
        1. User signs up (creates account)
        2. Selects "Personal" in account setup
        3. Adds payment method via checkout
        4. Completes onboarding
        """
        # Step 1: Create user (simulating OAuth signup)
        user = await create_test_user(client, "e2e_personal_billing@example.com")
        headers = user["headers"]

        # Verify initial onboarding state
        response = await client.get("/v0/user/onboarding", headers=headers)
        assert response.status_code == 200
        assert response.json()["current_step"] == "account_setup"

        # Step 2: Complete account setup - personal
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "billing_setup",
                "step_data": {"selected_type": "personal"},
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "billing_setup"

        # Step 3: User would add payment method via Stripe Checkout
        # (simulated - actual checkout happens in browser)

        # Step 4: Complete onboarding
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "personal",
                    "payment_method_added": True,
                    "billing_skipped": False,
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "completed"

        # Verify user is marked as onboarded
        legacy = await client.get("/v0/user/onboarding-status", headers=headers)
        assert legacy.json()["onboarded"] is True

    @pytest.mark.anyio
    async def test_e2e_path_a_personal_direct_signup_skip_billing(
        self,
        client: AsyncClient,
    ):
        """
        E2E Test: Path A - Direct signup as personal user, skip billing.

        Flow:
        1. User signs up
        2. Selects "Personal" in account setup
        3. Skips billing setup
        4. Completes onboarding (can hire assistants later)
        """
        user = await create_test_user(client, "e2e_personal_skip@example.com")
        headers = user["headers"]

        # Complete account setup
        await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "billing_setup",
                "step_data": {"selected_type": "personal"},
            },
        )

        # Skip billing
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "personal",
                    "billing_skipped": True,
                    "payment_method_added": False,
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["current_step"] == "completed"
        assert response.json()["step_data"]["billing_skipped"] is True

    @pytest.mark.anyio
    async def test_e2e_path_a_business_direct_signup_with_org(
        self,
        client: AsyncClient,
    ):
        """
        E2E Test: Path A - Direct signup as business user with organization.

        Flow:
        1. User signs up
        2. Selects "Business" → creates organization
        3. Optionally adds business details (tax ID, address)
        4. Adds payment method for organization
        5. Completes onboarding
        """
        user = await create_test_user(client, "e2e_business_full@example.com")
        headers = user["headers"]

        # Step 2a: Create organization
        org_response = await client.post(
            "/v0/organizations",
            headers=headers,
            json={"name": "E2E Business Corp"},
        )
        assert org_response.status_code == 201
        org_id = str(org_response.json()["id"])  # Convert to string

        # Step 2b: Update onboarding with business selection
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "billing_setup",
                "step_data": {
                    "selected_type": "business",
                    "organization_id": org_id,
                    "organization_name": "E2E Business Corp",
                },
            },
        )
        assert (
            response.status_code == 200
        ), f"Got {response.status_code}: {response.json()}"

        # Step 3: Add business details to organization is optional
        # In a real flow, this would be done via the billing/billing-profile endpoint
        # after Stripe customer is set up

        # Step 4: Complete onboarding
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "business",
                    "organization_id": org_id,
                    "organization_name": "E2E Business Corp",
                    "payment_method_added": True,
                    "billing_skipped": False,
                },
            },
        )
        assert response.status_code == 200

        # Verify organization was created and user is owner
        org = await client.get(f"/v0/organizations/{org_id}", headers=headers)
        assert org.status_code == 200
        assert org.json()["name"] == "E2E Business Corp"

    @pytest.mark.anyio
    async def test_e2e_interrupted_onboarding_resume(self, client: AsyncClient):
        """
        E2E Test: User interrupts onboarding and resumes later.

        Flow:
        1. User starts onboarding
        2. Completes account setup (selects business)
        3. Creates organization
        4. Leaves before billing setup
        5. Returns later and resumes from billing_setup
        6. Completes onboarding
        """
        user = await create_test_user(client, "e2e_interrupted@example.com")
        headers = user["headers"]

        # Create org and progress to billing setup
        org_response = await client.post(
            "/v0/organizations",
            headers=headers,
            json={"name": "Interrupted Corp"},
        )
        org_id = str(org_response.json()["id"])  # Convert to string

        # Update onboarding and verify it was saved
        update_response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "billing_setup",
                "step_data": {
                    "selected_type": "business",
                    "organization_id": org_id,
                },
            },
        )
        assert update_response.status_code == 200, f"Got: {update_response.json()}"
        assert update_response.json()["current_step"] == "billing_setup"

        # Simulate user leaving (no action, just checking state persists)

        # User returns - check they resume at billing_setup
        resume_response = await client.get("/v0/user/onboarding", headers=headers)
        assert resume_response.status_code == 200
        data = resume_response.json()
        assert data["current_step"] == "billing_setup"
        # step_data fields may be normalized by schema
        assert data["step_data"].get("selected_type") == "business"
        assert data["step_data"].get("organization_id") == org_id

        # Complete the flow
        response = await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "business",
                    "organization_id": org_id,
                    "payment_method_added": True,
                },
            },
        )
        assert response.status_code == 200

    @pytest.mark.anyio
    async def test_e2e_user_can_restart_onboarding(self, client: AsyncClient):
        """
        E2E Test: User can reset and restart their onboarding.

        Useful for users who made a mistake in account type selection.
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

        # User decides they want to switch to business
        # Reset onboarding
        reset_response = await client.delete("/v0/user/onboarding", headers=headers)
        assert reset_response.status_code == 200
        assert reset_response.json()["current_step"] == "account_setup"

        # Start fresh
        response = await client.get("/v0/user/onboarding", headers=headers)
        assert response.json()["current_step"] == "account_setup"
        # After reset, step_data should have cleared user-set values
        # (schema may add nullable fields with None values)
        step_data = response.json()["step_data"]
        assert step_data.get("selected_type") is None
        assert step_data.get("organization_id") is None

    @pytest.mark.anyio
    async def test_e2e_onboarding_state_consistency(self, client: AsyncClient):
        """
        E2E Test: Onboarding state remains consistent across API calls.
        """
        user = await create_test_user(client, "e2e_consistency@example.com")
        headers = user["headers"]

        # Set state
        await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "billing_setup",
                "step_data": {
                    "selected_type": "personal",
                    "custom_field": "test_value",
                },
            },
        )

        # Get state multiple times
        for _ in range(3):
            response = await client.get("/v0/user/onboarding", headers=headers)
            assert response.json()["current_step"] == "billing_setup"
            assert response.json()["step_data"]["custom_field"] == "test_value"

    @pytest.mark.anyio
    async def test_e2e_personal_user_later_creates_org(self, client: AsyncClient):
        """
        E2E Test: Personal user completes onboarding, then later creates org.

        Flow:
        1. User signs up as personal
        2. Completes onboarding
        3. Later creates an organization
        4. Organization gets its own billing entity
        """
        user = await create_test_user(client, "e2e_later_org@example.com")
        headers = user["headers"]

        # Complete as personal
        await client.put(
            "/v0/user/onboarding",
            headers=headers,
            json={
                "current_step": "completed",
                "step_data": {
                    "selected_type": "personal",
                    "billing_skipped": True,
                },
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
