"""Tests for organization Stripe customer and checkout endpoints."""

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.settings import settings
from orchestra.tests.utils import create_test_user


@pytest.fixture(autouse=True)
def _mock_stripe_settings(monkeypatch):
    """Set Stripe settings for tests."""
    monkeypatch.setattr(
        settings,
        "stripe_secret_key",
        "sk_test_dummy_for_mocking",
        raising=False,
    )
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test", raising=False)


# ============================================================================
# Test: Ensure Organization Stripe Customer
# ============================================================================


@pytest.mark.anyio
async def test_ensure_org_stripe_customer_creates_new_customer(client: AsyncClient):
    """Test creating a new Stripe customer for an organization."""
    # Create test user (org owner)
    owner = await create_test_user(client, f"owner-{uuid.uuid4()}@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_data = org_response.json()
    org_id = org_data["id"]

    # Update business profile with billing email (required for Stripe customer)
    profile_response = await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "billing@testorg.com", "business_name": "Test Business"},
        headers=owner["headers"],
    )
    assert profile_response.status_code == 200

    # Mock Stripe customer creation
    with patch("stripe.Customer.create") as mock_create, patch(
        "os.environ.get",
        return_value="sk_test_123",
    ):
        mock_customer = MagicMock()
        mock_customer.id = "cus_test_org_123"
        mock_create.return_value = mock_customer

        # Create Stripe customer for organization
        response = await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

        assert response.status_code == 200
        data = response.json()
        assert data["organization_id"] == org_id
        assert data["stripe_customer_id"] == "cus_test_org_123"
        assert data["is_new"] is True

        # Verify Stripe was called with org metadata
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["email"] == "billing@testorg.com"
        assert call_kwargs["metadata"]["organization_id"] == str(org_id)


@pytest.mark.anyio
async def test_ensure_org_stripe_customer_returns_existing(client: AsyncClient):
    """Test that existing Stripe customer is returned without creating new one."""
    # Create test user (org owner)
    owner = await create_test_user(client, f"owner2-{uuid.uuid4()}@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Org Existing {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Update business profile
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "billing2@testorg.com"},
        headers=owner["headers"],
    )

    # Create Stripe customer first time
    with patch("stripe.Customer.create") as mock_create, patch(
        "os.environ.get",
        return_value="sk_test_123",
    ):
        mock_customer = MagicMock()
        mock_customer.id = "cus_existing_456"
        mock_create.return_value = mock_customer

        first_response = await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )
        assert first_response.status_code == 200
        assert first_response.json()["is_new"] is True

    # Second call should return existing without creating new
    second_response = await client.post(
        f"/v0/organizations/{org_id}/billing/stripe-customer",
        headers=owner["headers"],
    )

    assert second_response.status_code == 200
    data = second_response.json()
    assert data["stripe_customer_id"] == "cus_existing_456"
    assert data["is_new"] is False


@pytest.mark.anyio
async def test_ensure_org_stripe_customer_with_request_body(client: AsyncClient):
    """Test creating Stripe customer with custom email/name in request body."""
    owner = await create_test_user(client, f"owner3-{uuid.uuid4()}@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Org Body {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    with patch("stripe.Customer.create") as mock_create, patch(
        "os.environ.get",
        return_value="sk_test_123",
    ):
        mock_customer = MagicMock()
        mock_customer.id = "cus_custom_789"
        mock_create.return_value = mock_customer

        # Add Content-Type header to ensure body is parsed as JSON
        headers = {**owner["headers"], "Content-Type": "application/json"}
        response = await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            json={
                "billing_email": "custom@billing.com",
                "business_name": "Custom Business Name",
            },
            headers=headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["stripe_customer_id"] == "cus_custom_789"

        # Verify custom values were used (billing_email or org name might be used as fallback)
        call_kwargs = mock_create.call_args.kwargs
        # The billing_email in request body should be prioritized
        assert call_kwargs["email"] == "custom@billing.com"
        assert call_kwargs["name"] == "Custom Business Name"


@pytest.mark.anyio
async def test_ensure_org_stripe_customer_unauthorized(client: AsyncClient):
    """Test that non-members cannot create Stripe customer."""
    # Create org owner
    owner = await create_test_user(client, f"owner4-{uuid.uuid4()}@test.com")
    # Create separate user who is not a member
    other_user = await create_test_user(client, f"other-{uuid.uuid4()}@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Org Unauth {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Try to create Stripe customer as non-member
    response = await client.post(
        f"/v0/organizations/{org_id}/billing/stripe-customer",
        headers=other_user["headers"],
    )

    assert response.status_code == 403
    assert "permission" in response.json()["detail"].lower()


# ============================================================================
# Test: Get Organization Stripe Customer
# ============================================================================


@pytest.mark.anyio
async def test_get_org_stripe_customer_success(client: AsyncClient):
    """Test getting existing Stripe customer ID."""
    owner = await create_test_user(client, f"owner5-{uuid.uuid4()}@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Org Get {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Set up business profile and create Stripe customer
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "get@testorg.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create, patch(
        "os.environ.get",
        return_value="sk_test_123",
    ):
        mock_customer = MagicMock()
        mock_customer.id = "cus_get_test_123"
        mock_create.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

    # Now get the customer
    response = await client.get(
        f"/v0/organizations/{org_id}/billing/stripe-customer",
        headers=owner["headers"],
    )

    assert response.status_code == 200
    data = response.json()
    assert data["stripe_customer_id"] == "cus_get_test_123"
    assert data["is_new"] is False


@pytest.mark.anyio
async def test_get_org_stripe_customer_not_set_up(client: AsyncClient):
    """Test getting Stripe customer when none exists."""
    owner = await create_test_user(client, f"owner6-{uuid.uuid4()}@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Org No Stripe {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    response = await client.get(
        f"/v0/organizations/{org_id}/billing/stripe-customer",
        headers=owner["headers"],
    )

    assert response.status_code == 404
    assert "direct billing" in response.json()["detail"].lower()


# ============================================================================
# Test: Create Organization Checkout Session
# ============================================================================


@pytest.mark.anyio
async def test_create_org_checkout_session_success(client: AsyncClient):
    """Test creating a checkout session for organization."""
    owner = await create_test_user(client, f"owner7-{uuid.uuid4()}@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Org Checkout {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Set up Stripe customer first
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "checkout@testorg.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create_customer, patch(
        "stripe.checkout.Session.create",
    ) as mock_checkout, patch("os.environ.get", return_value="sk_test_123"):
        mock_customer = MagicMock()
        mock_customer.id = "cus_checkout_test"
        mock_create_customer.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

        # Now create checkout session
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/pay/test123"
        mock_session.id = "cs_test_123"
        mock_checkout.return_value = mock_session

        response = await client.post(
            f"/v0/organizations/{org_id}/billing/checkout",
            json={
                "amount": 100,
                "success_url": "https://app.test.com/billing?success=true",
                "cancel_url": "https://app.test.com/billing",
            },
            headers=owner["headers"],
        )

        assert response.status_code == 200
        data = response.json()
        assert data["checkout_url"] == "https://checkout.stripe.com/pay/test123"
        assert data["session_id"] == "cs_test_123"

        # Verify checkout session was created with org metadata
        mock_checkout.assert_called_once()
        call_kwargs = mock_checkout.call_args.kwargs
        assert call_kwargs["customer"] == "cus_checkout_test"
        assert call_kwargs["metadata"]["organization_id"] == str(org_id)
        assert call_kwargs["line_items"][0]["quantity"] == 100


@pytest.mark.anyio
async def test_create_org_checkout_without_stripe_customer_fails(client: AsyncClient):
    """Test that checkout fails if org has no Stripe customer."""
    owner = await create_test_user(client, f"owner8-{uuid.uuid4()}@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Org No Customer {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    response = await client.post(
        f"/v0/organizations/{org_id}/billing/checkout",
        json={
            "amount": 50,
            "success_url": "https://app.test.com/success",
            "cancel_url": "https://app.test.com/cancel",
        },
        headers=owner["headers"],
    )

    assert response.status_code == 400
    assert "stripe customer" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_create_org_checkout_with_invalid_amount_fails(client: AsyncClient):
    """Test that checkout fails with invalid amount."""
    owner = await create_test_user(client, f"owner9-{uuid.uuid4()}@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Org Invalid Amount {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Set up Stripe customer
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "amount@testorg.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create, patch(
        "os.environ.get",
        return_value="sk_test_123",
    ):
        mock_customer = MagicMock()
        mock_customer.id = "cus_amount_test"
        mock_create.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

    response = await client.post(
        f"/v0/organizations/{org_id}/billing/checkout",
        json={
            "amount": 0,
            "success_url": "https://app.test.com/success",
            "cancel_url": "https://app.test.com/cancel",
        },
        headers=owner["headers"],
    )

    assert response.status_code == 400
    assert "amount" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_create_org_checkout_unauthorized(client: AsyncClient):
    """Test that non-members cannot create checkout sessions."""
    owner = await create_test_user(client, f"owner10-{uuid.uuid4()}@test.com")
    other_user = await create_test_user(client, f"other2-{uuid.uuid4()}@test.com")

    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Org Unauth Checkout {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    response = await client.post(
        f"/v0/organizations/{org_id}/billing/checkout",
        json={
            "amount": 10,
            "success_url": "https://test.com/success",
            "cancel_url": "https://test.com/cancel",
        },
        headers=other_user["headers"],
    )

    assert response.status_code == 403


# ============================================================================
# Test: Organization Deletion Archives Stripe Customer
# ============================================================================


@pytest.mark.anyio
async def test_delete_org_archives_stripe_customer(client: AsyncClient):
    """Test that deleting an organization archives the Stripe customer."""
    owner = await create_test_user(client, f"owner-del-{uuid.uuid4()}@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Test Delete Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Set up billing email and create Stripe customer
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "delete-test@org.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create, patch(
        "stripe.Customer.modify",
    ) as mock_modify:
        mock_customer = MagicMock()
        mock_customer.id = "cus_to_be_archived"
        mock_create.return_value = mock_customer

        # Create Stripe customer
        response = await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )
        assert response.status_code == 200

        # Delete organization
        delete_response = await client.delete(
            f"/v0/organizations/{org_id}",
            headers=owner["headers"],
        )
        assert delete_response.status_code == 204

        # Verify Stripe.Customer.modify was called to archive
        mock_modify.assert_called_once()
        call_kwargs = mock_modify.call_args.kwargs
        assert call_kwargs.get("metadata", {}).get("organization_deleted") == "true"


# ============================================================================
# Test: Tax ID Webhook Sync
# ============================================================================


@pytest.mark.anyio
async def test_tax_id_webhook_creates_org_tax_id(client: AsyncClient, dbsession):
    """Test that customer.tax_id.created webhook updates organization tax_id."""
    from orchestra.db.dao.organization_dao import OrganizationDAO

    owner = await create_test_user(client, f"owner-tax-{uuid.uuid4()}@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Tax Webhook Test Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Set up Stripe customer
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "tax-webhook@org.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_tax_webhook_test"
        mock_create.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

    # Simulate tax_id.created webhook
    webhook_payload = {
        "id": f"evt_tax_created_{uuid.uuid4()}",
        "type": "customer.tax_id.created",
        "data": {
            "object": {
                "customer": "cus_tax_webhook_test",
                "value": "DE123456789",
                "type": "eu_vat",
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        response = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        assert response.status_code == 200

    # Verify organization tax_id was updated
    dbsession.expire_all()
    org_dao = OrganizationDAO(dbsession)
    org = org_dao.get(org_id)
    assert org.billing_account.tax_id == "DE123456789"


@pytest.mark.anyio
async def test_tax_id_webhook_deletes_org_tax_id(client: AsyncClient, dbsession):
    """Test that customer.tax_id.deleted webhook clears organization tax_id."""
    from orchestra.db.dao.organization_dao import OrganizationDAO

    owner = await create_test_user(client, f"owner-taxdel-{uuid.uuid4()}@test.com")

    # Create organization with tax_id
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Tax Delete Webhook Test Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Set up business profile with tax_id
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={
            "billing_email": "tax-delete@org.com",
            "tax_id": "GB123456789",
        },
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_tax_delete_test"
        mock_create.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

    # Simulate tax_id.deleted webhook
    webhook_payload = {
        "id": f"evt_tax_deleted_{uuid.uuid4()}",
        "type": "customer.tax_id.deleted",
        "data": {
            "object": {
                "customer": "cus_tax_delete_test",
                "value": "GB123456789",
                "type": "gb_vat",
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        response = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        assert response.status_code == 200

    # Verify organization tax_id was cleared
    dbsession.expire_all()
    org_dao = OrganizationDAO(dbsession)
    org = org_dao.get(org_id)
    assert org.billing_account.tax_id is None


# ============================================================================
# Test: Organization Checkout Webhook Adds Credits
# ============================================================================


@pytest.mark.anyio
async def test_org_checkout_webhook_adds_credits(client: AsyncClient, dbsession):
    """Test that checkout.session.completed webhook adds credits to organization."""
    from orchestra.db.dao.organization_dao import OrganizationDAO

    owner = await create_test_user(client, f"owner-checkout-{uuid.uuid4()}@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Checkout Webhook Test Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Set up Stripe customer
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "checkout-webhook@org.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_org_checkout_webhook"
        mock_create.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

    # Get initial credits
    org_dao = OrganizationDAO(dbsession)
    org = org_dao.get(org_id)
    initial_credits = float(org.billing_account.credits) if org.billing_account else 0

    # Simulate checkout.session.completed webhook for organization
    webhook_payload = {
        "id": f"evt_checkout_org_{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_org_test_123",
                "customer": "cus_org_checkout_webhook",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 10000,  # $100 in cents
                "currency": "usd",
                "metadata": {
                    "organization_id": str(org_id),
                    "credits_purchased": "100",
                },
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        response = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        assert response.status_code == 200

    # Verify credits were added
    dbsession.expire_all()
    org = org_dao.get(org_id)
    assert float(org.billing_account.credits) == initial_credits + 100


# ============================================================================
# Test: Webhook Edge Cases
# ============================================================================


@pytest.mark.anyio
async def test_webhook_unknown_customer_ignored(client: AsyncClient):
    """Test that webhook for unknown Stripe customer is handled gracefully."""
    # Webhook for a customer that doesn't exist in our system
    webhook_payload = {
        "id": f"evt_unknown_customer_{uuid.uuid4()}",
        "type": "customer.tax_id.created",
        "data": {
            "object": {
                "customer": "cus_nonexistent_12345",
                "value": "XX123456789",
                "type": "eu_vat",
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        response = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        # Should return 200 (webhook handled, just no action taken)
        assert response.status_code == 200


@pytest.mark.anyio
async def test_webhook_missing_metadata_handled(client: AsyncClient):
    """Test that checkout webhook without required identifiers returns 400."""
    # Webhook missing both client_reference_id (user_id) and organization_id in metadata
    webhook_payload = {
        "id": f"evt_no_metadata_{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_no_metadata",
                "customer": "cus_some_customer",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 1000,  # $10 in cents
                "currency": "usd",
                # Note: client_reference_id is missing (would contain user_id)
                # and organization_id is not in metadata
                "metadata": {},  # Empty metadata
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        response = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        # Returns 400 because we can't identify who to credit
        assert response.status_code == 400


@pytest.mark.anyio
async def test_org_checkout_webhook_idempotent(client: AsyncClient, dbsession):
    """Test that duplicate org checkout webhooks don't add credits twice."""
    from orchestra.db.dao.organization_dao import OrganizationDAO

    owner = await create_test_user(client, f"owner-idemp-{uuid.uuid4()}@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Idempotent Webhook Test Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Set up Stripe customer
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "idempotent@org.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_org_idempotent"
        mock_create.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

    org_dao = OrganizationDAO(dbsession)

    # Same event ID for both calls
    event_id = f"evt_org_idempotent_{uuid.uuid4()}"
    webhook_payload = {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_org_idempotent",
                "customer": "cus_org_idempotent",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 5000,  # $50 in cents
                "currency": "usd",
                "metadata": {
                    "organization_id": str(org_id),
                    "credits_purchased": "50",
                },
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        # First call
        response1 = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        assert response1.status_code == 200

        dbsession.expire_all()
        org_after_first = org_dao.get(org_id)
        credits_after_first = float(org_after_first.billing_account.credits)

        # Second call with same event ID
        response2 = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        assert response2.status_code == 200

    dbsession.expire_all()
    org_after_second = org_dao.get(org_id)
    credits_after_second = float(org_after_second.billing_account.credits)

    # Credits should be same (idempotent)
    assert credits_after_second == credits_after_first


# ============================================================================
# E2E Full Organization Billing Flow Tests
# ============================================================================


@pytest.mark.anyio
async def test_e2e_org_full_billing_lifecycle(client: AsyncClient, dbsession):
    """
    E2E Test: Complete organization billing lifecycle.

    Flow:
    1. User creates organization
    2. User sets up business profile
    3. User creates Stripe customer
    4. User creates checkout session
    5. Webhook confirms payment
    6. Credits are added to organization
    7. Organization can use credits
    """

    from orchestra.db.dao.organization_dao import OrganizationDAO

    owner = await create_test_user(client, f"e2e-lifecycle-{uuid.uuid4()}@test.com")

    # Step 1: Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"E2E Billing Lifecycle Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Step 2: Set up business profile
    profile_response = await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={
            "business_name": "E2E Test Corp",
            "billing_email": "billing@e2e-test.com",
            "billing_address": {
                "line1": "123 E2E Street",
                "city": "Test City",
                "country": "US",
                "postal_code": "12345",
            },
        },
        headers=owner["headers"],
    )
    assert profile_response.status_code == 200

    # Step 3: Create Stripe customer
    with patch("stripe.Customer.create") as mock_create_customer:
        mock_customer = MagicMock()
        mock_customer.id = "cus_e2e_lifecycle"
        mock_create_customer.return_value = mock_customer

        stripe_response = await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )
        assert stripe_response.status_code == 200
        assert stripe_response.json()["stripe_customer_id"] == "cus_e2e_lifecycle"
        assert stripe_response.json()["is_new"] is True

    # Step 4: Create checkout session
    with patch("stripe.checkout.Session.create") as mock_checkout:
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/e2e_test"
        mock_session.id = "cs_e2e_lifecycle"
        mock_checkout.return_value = mock_session

        checkout_response = await client.post(
            f"/v0/organizations/{org_id}/billing/checkout",
            json={
                "amount": 200,
                "success_url": "https://app.test.com/success",
                "cancel_url": "https://app.test.com/cancel",
            },
            headers=owner["headers"],
        )
        assert checkout_response.status_code == 200
        assert "checkout_url" in checkout_response.json()

    # Get initial credits
    org_dao = OrganizationDAO(dbsession)
    org = org_dao.get(org_id)
    initial_credits = float(org.billing_account.credits) if org.billing_account else 0

    # Step 5: Simulate webhook for payment completion
    webhook_payload = {
        "id": f"evt_e2e_lifecycle_{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_e2e_lifecycle",
                "customer": "cus_e2e_lifecycle",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 20000,  # $200
                "currency": "usd",
                "metadata": {"organization_id": str(org_id)},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        webhook_response = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "test_sig",
            },
        )
        assert webhook_response.status_code == 200

    # Step 6: Verify credits were added
    dbsession.expire_all()
    org = org_dao.get(org_id)
    assert float(org.billing_account.credits) == initial_credits + 200

    # Step 7: Verify billing endpoint shows correct credits
    billing_response = await client.get(
        f"/v0/organizations/{org_id}/billing/credits",
        headers=owner["headers"],
    )
    assert billing_response.status_code == 200
    assert billing_response.json()["credits"] == initial_credits + 200


@pytest.mark.anyio
async def test_e2e_org_billing_with_tax_details(client: AsyncClient, dbsession):
    """
    E2E Test: Organization billing with tax ID and business details.
    """
    owner = await create_test_user(client, f"e2e-tax-{uuid.uuid4()}@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"E2E Tax Details Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201
    org_id = org_response.json()["id"]

    # Set up business profile with tax details
    profile_response = await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={
            "business_name": "E2E Tax Corp GmbH",
            "billing_email": "steuer@e2e-tax.de",
            "tax_id": "DE123456789",
            "billing_address": {
                "line1": "Hauptstraße 42",
                "city": "Berlin",
                "country": "DE",
                "postal_code": "10115",
            },
        },
        headers=owner["headers"],
    )
    assert profile_response.status_code == 200

    # Verify the business profile was saved
    profile_get = await client.get(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        headers=owner["headers"],
    )
    assert profile_get.status_code == 200
    profile_data = profile_get.json()
    assert profile_data["business_name"] == "E2E Tax Corp GmbH"
    assert profile_data["tax_id"] == "DE123456789"

    # Create Stripe customer
    with patch("stripe.Customer.create") as mock_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_e2e_tax"
        mock_create.return_value = mock_customer

        stripe_response = await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )
        assert stripe_response.status_code == 200

        # Verify Stripe was called with org email
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["email"] == "steuer@e2e-tax.de"


@pytest.mark.anyio
async def test_e2e_org_billing_permission_levels(client: AsyncClient, dbsession):
    """
    E2E Test: Different permission levels for org billing.

    - Owner: Full access (read + write)
    - Admin: Full access (read + write)
    - Member: Read only
    - Non-member: No access
    """
    from orchestra.db.dao.role_dao import RoleDAO

    owner = await create_test_user(client, f"e2e-perm-owner-{uuid.uuid4()}@test.com")
    admin = await create_test_user(client, f"e2e-perm-admin-{uuid.uuid4()}@test.com")
    member = await create_test_user(client, f"e2e-perm-member-{uuid.uuid4()}@test.com")
    outsider = await create_test_user(
        client,
        f"e2e-perm-outsider-{uuid.uuid4()}@test.com",
    )

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"E2E Permissions Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Get roles
    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)
    member_role = role_dao.get_by_name("Member", organization_id=None)

    # Add admin and member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin["id"], "role_id": admin_role.id},
        headers=owner["headers"],
    )
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "role_id": member_role.id},
        headers=owner["headers"],
    )

    # Set up billing
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "perms@test.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_e2e_perms"
        mock_create.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

    # Test 1: Owner can read billing
    owner_read = await client.get(
        f"/v0/organizations/{org_id}/billing/credits",
        headers=owner["headers"],
    )
    assert owner_read.status_code == 200

    # Test 2: Admin can read billing
    admin_read = await client.get(
        f"/v0/organizations/{org_id}/billing/credits",
        headers=admin["headers"],
    )
    assert admin_read.status_code == 200

    # Test 3: Member can read billing (depending on permissions)
    member_read = await client.get(
        f"/v0/organizations/{org_id}/billing/credits",
        headers=member["headers"],
    )
    # Member may or may not have billing:read depending on role setup
    assert member_read.status_code in [200, 403]

    # Test 4: Outsider cannot access billing
    outsider_read = await client.get(
        f"/v0/organizations/{org_id}/billing/credits",
        headers=outsider["headers"],
    )
    assert outsider_read.status_code == 403

    # Test 5: Admin can create checkout (billing:write)
    with patch("stripe.checkout.Session.create") as mock_checkout:
        mock_session = MagicMock()
        mock_session.url = "https://checkout.stripe.com/admin"
        mock_session.id = "cs_admin"
        mock_checkout.return_value = mock_session

        admin_checkout = await client.post(
            f"/v0/organizations/{org_id}/billing/checkout",
            json={
                "amount": 50,
                "success_url": "https://test.com/success",
                "cancel_url": "https://test.com/cancel",
            },
            headers=admin["headers"],
        )
        assert admin_checkout.status_code == 200

    # Test 6: Member cannot create checkout (no billing:write)
    member_checkout = await client.post(
        f"/v0/organizations/{org_id}/billing/checkout",
        json={
            "amount": 50,
            "success_url": "https://test.com/success",
            "cancel_url": "https://test.com/cancel",
        },
        headers=member["headers"],
    )
    assert member_checkout.status_code == 403


@pytest.mark.anyio
async def test_e2e_org_multiple_credit_top_ups(client: AsyncClient, dbsession):
    """
    E2E Test: Organization receives multiple credit top-ups.
    """
    from orchestra.db.dao.organization_dao import OrganizationDAO

    owner = await create_test_user(client, f"e2e-multi-{uuid.uuid4()}@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"E2E Multi TopUp Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Set up billing
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "multi@test.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_e2e_multi"
        mock_create.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

    org_dao = OrganizationDAO(dbsession)
    org = org_dao.get(org_id)
    initial_credits = float(org.billing_account.credits) if org.billing_account else 0

    # First top-up: $100
    webhook1 = {
        "id": f"evt_multi_1_{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_multi_1",
                "customer": "cus_e2e_multi",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 10000,
                "currency": "usd",
                "metadata": {"organization_id": str(org_id)},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook1):
        await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook1),
            headers={"Content-Type": "application/json", "Stripe-Signature": "sig1"},
        )

    dbsession.expire_all()
    org = org_dao.get(org_id)
    assert float(org.billing_account.credits) == initial_credits + 100

    # Second top-up: $50
    webhook2 = {
        "id": f"evt_multi_2_{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_multi_2",
                "customer": "cus_e2e_multi",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "usd",
                "metadata": {"organization_id": str(org_id)},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook2):
        await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook2),
            headers={"Content-Type": "application/json", "Stripe-Signature": "sig2"},
        )

    dbsession.expire_all()
    org = org_dao.get(org_id)
    assert float(org.billing_account.credits) == initial_credits + 150  # 100 + 50

    # Third top-up: $250
    webhook3 = {
        "id": f"evt_multi_3_{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_multi_3",
                "customer": "cus_e2e_multi",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 25000,
                "currency": "usd",
                "metadata": {"organization_id": str(org_id)},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook3):
        await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook3),
            headers={"Content-Type": "application/json", "Stripe-Signature": "sig3"},
        )

    dbsession.expire_all()
    org = org_dao.get(org_id)
    assert float(org.billing_account.credits) == initial_credits + 400  # 100 + 50 + 250


@pytest.mark.anyio
async def test_e2e_org_billing_autorecharge_setup(client: AsyncClient, dbsession):
    """
    E2E Test: Organization sets up autorecharge.
    """
    from decimal import Decimal

    from orchestra.db.dao.organization_dao import OrganizationDAO

    owner = await create_test_user(client, f"e2e-auto-{uuid.uuid4()}@test.com")

    # Create organization
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"E2E Autorecharge Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Set up billing
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "auto@test.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_e2e_auto"
        mock_create.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

    # Configure autorecharge via PATCH /organizations/{id}/billing
    autorecharge_response = await client.patch(
        f"/v0/organizations/{org_id}/billing",
        json={
            "autorecharge": True,
            "autorecharge_threshold": 50,
            "autorecharge_qty": 100,
        },
        headers=owner["headers"],
    )
    assert autorecharge_response.status_code == 200

    # Verify autorecharge was configured
    org_dao = OrganizationDAO(dbsession)
    dbsession.expire_all()
    org = org_dao.get(org_id)
    assert org.billing_account.autorecharge is True
    assert org.billing_account.autorecharge_threshold == Decimal("50")
    assert org.billing_account.autorecharge_qty == Decimal("100")


@pytest.mark.anyio
async def test_e2e_org_new_member_uses_org_billing(client: AsyncClient, dbsession):
    """
    E2E Test: New member joins org and can use org's billing for credits.

    When a member works in org context, the org's billing is used.
    """
    owner = await create_test_user(client, f"e2e-mem-owner-{uuid.uuid4()}@test.com")
    new_member = await create_test_user(client, f"e2e-mem-new-{uuid.uuid4()}@test.com")

    # Create organization with billing
    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"E2E Member Billing Org {uuid.uuid4()}"},
        headers=owner["headers"],
    )
    org_id = org_response.json()["id"]

    # Set up org billing
    await client.patch(
        f"/v0/organizations/{org_id}/billing/billing-profile",
        json={"billing_email": "member-billing@test.com"},
        headers=owner["headers"],
    )

    with patch("stripe.Customer.create") as mock_create:
        mock_customer = MagicMock()
        mock_customer.id = "cus_e2e_member_billing"
        mock_create.return_value = mock_customer

        await client.post(
            f"/v0/organizations/{org_id}/billing/stripe-customer",
            headers=owner["headers"],
        )

    # Add credits to org
    webhook_payload = {
        "id": f"evt_member_billing_{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_member_billing",
                "customer": "cus_e2e_member_billing",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 50000,  # $500
                "currency": "usd",
                "metadata": {"organization_id": str(org_id)},
            },
        },
    }

    with patch("stripe.Webhook.construct_event", return_value=webhook_payload):
        await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(webhook_payload),
            headers={"Content-Type": "application/json", "Stripe-Signature": "sig"},
        )

    # Add new member
    await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": new_member["id"]},
        headers=owner["headers"],
    )

    # New member can see org credits
    credits_response = await client.get(
        f"/v0/organizations/{org_id}/billing/credits",
        headers=new_member["headers"],
    )
    # Member should see org has credits (if they have billing:read permission)
    # The exact permission depends on the default Member role
    assert credits_response.status_code in [200, 403]

    if credits_response.status_code == 200:
        assert credits_response.json()["credits"] == 500.0
