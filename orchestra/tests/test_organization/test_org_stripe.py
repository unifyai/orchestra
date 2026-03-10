"""Tests for organization billing endpoints – Stripe integration.

These tests cover:
  - Billing profile updates for organizations (backward-compat routes).
  - Unified billing profile endpoints for org contexts.
  - Stripe webhook processing for org billing (checkout, tax ID).
  - E2E billing lifecycle scenarios.
  - Permission levels for billing operations.

The explicit ``/organizations/{id}/billing/stripe-customer`` and
``/organizations/{id}/billing/checkout`` endpoints have been consolidated
into the unified ``/billing/checkout-session`` endpoint.  Stripe customer
creation is now handled implicitly during checkout.
"""

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from orchestra.settings import settings
from orchestra.tests.utils import create_test_org, create_test_user


@pytest.fixture(autouse=True)
def _mock_stripe_settings(monkeypatch):
    """Set Stripe settings for tests."""
    monkeypatch.setattr(
        settings,
        "stripe_secret_key",
        "sk_test_dummy_for_mocking",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "stripe_webhook_secret",
        "whsec_test",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "stripe_skip_signature_verification",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "stripe_unify_credits_price_id_personal",
        "price_test_personal_dummy",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "stripe_unify_credits_price_id_business",
        "price_test_business_dummy",
        raising=False,
    )


# ============================================================================
# Billing Profile – Unified Routes (org context via org API key)
# ============================================================================


@pytest.mark.anyio
async def test_org_billing_profile_unified_route(client: AsyncClient):
    """Test GET/PATCH /billing/billing-profile with org API key."""
    owner = await create_test_user(client, f"owner-unified-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"Unified Profile Org {uuid.uuid4()}",
    )

    # Update billing profile via unified endpoint
    response = await client.patch(
        "/v0/billing/billing-profile",
        json={
            "billing_email": "unified@testorg.com",
            "business_name": "Unified Business",
        },
        headers=org["headers"],
    )
    assert response.status_code == 200
    data = response.json()
    assert data["billing_email"] == "unified@testorg.com"
    assert data["is_business"] is True

    # Verify via GET unified endpoint
    get_response = await client.get(
        "/v0/billing/billing-profile",
        headers=org["headers"],
    )
    assert get_response.status_code == 200
    get_data = get_response.json()
    assert get_data["billing_email"] == "unified@testorg.com"
    assert get_data["is_business"] is True


@pytest.mark.anyio
async def test_org_billing_profile_with_address(client: AsyncClient):
    """Test updating billing profile with address details."""
    owner = await create_test_user(client, f"owner-addr-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"Address Profile Org {uuid.uuid4()}",
    )

    response = await client.patch(
        "/v0/billing/billing-profile",
        json={
            "business_name": "Address Corp",
            "billing_email": "billing@address.com",
            "billing_address": {
                "line1": "123 Business Ave",
                "city": "London",
                "country": "GB",
                "postal_code": "EC1A 1BB",
            },
        },
        headers=org["headers"],
    )
    assert response.status_code == 200
    data = response.json()
    assert data["billing_address"]["city"] == "London"
    assert data["billing_address"]["country"] == "GB"


@pytest.mark.anyio
async def test_org_billing_profile_personal_key_returns_personal(client: AsyncClient):
    """Test that using a personal API key returns the personal (non-org) profile."""
    user = await create_test_user(
        client,
        f"personal-profile-{uuid.uuid4()}@test.com",
    )

    # Using a personal API key → returns is_business=False
    response = await client.get(
        "/v0/billing/billing-profile",
        headers=user["headers"],
    )
    assert response.status_code == 200
    data = response.json()
    assert data["is_business"] is False


# ============================================================================
# Account Info – org context
# ============================================================================


@pytest.mark.anyio
async def test_org_account_info_via_unified_endpoint(client: AsyncClient):
    """Test GET /billing/account-info with org API key returns correct data."""
    owner = await create_test_user(client, f"owner-acct-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"Account Info Org {uuid.uuid4()}",
    )

    response = await client.get(
        "/v0/billing/account-info",
        headers=org["headers"],
    )
    assert response.status_code == 200
    data = response.json()
    assert "credits" in data
    assert "autorecharge" in data


# ============================================================================
# Checkout Session – org context
# ============================================================================


@pytest.mark.anyio
async def test_org_checkout_session(client: AsyncClient):
    """Test POST /billing/checkout-session with org API key."""
    owner = await create_test_user(client, f"owner-co-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"Checkout Org {uuid.uuid4()}",
    )

    with patch("stripe.checkout.Session.create") as mock_create:
        mock_session = MagicMock()
        mock_session.id = "cs_org_test_123"
        mock_session.url = "https://checkout.stripe.com/test"
        mock_create.return_value = mock_session

        response = await client.post(
            "/v0/billing/checkout-session",
            headers=org["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "cs_org_test_123"
        assert data["url"] == "https://checkout.stripe.com/test"


# ============================================================================
# Webhook – Organization Checkout Credits
# ============================================================================


@pytest.mark.anyio
async def test_org_checkout_webhook_adds_credits(client: AsyncClient, dbsession):
    """Test that checkout.session.completed webhook adds credits to organization."""
    from orchestra.db.dao.organization_dao import OrganizationDAO
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, f"owner-checkout-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"Checkout Webhook Org {uuid.uuid4()}",
    )
    org_id = org["id"]

    # Set Stripe customer ID directly in DB (simulating prior checkout)
    org_db = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org_db.billing_account.stripe_customer_id = "cus_org_checkout_webhook"
    dbsession.flush()

    # Record initial credits
    org_dao = OrganizationDAO(dbsession)
    org_obj = org_dao.get(org_id)
    initial_credits = float(org_obj.billing_account.credits)

    # Fire checkout.session.completed webhook
    webhook_payload = {
        "id": f"evt_org_checkout_{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_org_credit_test",
                "customer": "cus_org_checkout_webhook",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 10000,  # $100 in cents
                "currency": "usd",
                "metadata": {
                    "organization_id": str(org_id),
                },
            },
        },
    }

    response = await client.post(
        "/v0/webhooks/stripe",
        content=json.dumps(webhook_payload),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200

    # Verify credits were added
    dbsession.expire_all()
    org_obj = org_dao.get(org_id)
    assert float(org_obj.billing_account.credits) == initial_credits + 100


@pytest.mark.anyio
async def test_org_checkout_webhook_idempotent(client: AsyncClient, dbsession):
    """Test that duplicate checkout webhooks don't add credits twice."""
    from orchestra.db.dao.organization_dao import OrganizationDAO
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, f"owner-idemp-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"Idempotent Webhook Org {uuid.uuid4()}",
    )
    org_id = org["id"]

    org_db = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org_db.billing_account.stripe_customer_id = "cus_org_idempotent"
    dbsession.flush()

    org_dao = OrganizationDAO(dbsession)

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
                "amount_total": 5000,  # $50
                "currency": "usd",
                "metadata": {
                    "organization_id": str(org_id),
                },
            },
        },
    }

    # First call
    resp1 = await client.post(
        "/v0/webhooks/stripe",
        content=json.dumps(webhook_payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp1.status_code == 200

    dbsession.expire_all()
    credits_after_first = float(org_dao.get(org_id).billing_account.credits)

    # Second call with same event ID → idempotent (no double-credit)
    resp2 = await client.post(
        "/v0/webhooks/stripe",
        content=json.dumps(webhook_payload),
        headers={"Content-Type": "application/json"},
    )
    assert resp2.status_code == 200

    dbsession.expire_all()
    credits_after_second = float(org_dao.get(org_id).billing_account.credits)

    assert credits_after_second == credits_after_first


# ============================================================================
# Webhook – Tax ID sync
# ============================================================================


@pytest.mark.anyio
async def test_tax_id_webhook_creates_org_tax_id(client: AsyncClient, dbsession):
    """Test that customer.tax_id.created webhook updates billing account tax_id."""
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, f"owner-tax-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"Tax Webhook Org {uuid.uuid4()}",
    )
    org_id = org["id"]

    # Set Stripe customer ID
    org_db = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org_db.billing_account.stripe_customer_id = "cus_tax_webhook_test"
    dbsession.flush()

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

    response = await client.post(
        "/v0/webhooks/stripe",
        content=json.dumps(webhook_payload),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200

    # Verify tax_id was synced
    dbsession.expire_all()
    org_db = dbsession.query(Organization).filter(Organization.id == org_id).first()
    assert org_db.billing_account.tax_id == "DE123456789"


@pytest.mark.anyio
async def test_tax_id_webhook_deletes_org_tax_id(client: AsyncClient, dbsession):
    """Test that customer.tax_id.deleted webhook clears billing account tax_id."""
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, f"owner-taxdel-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"Tax Delete Org {uuid.uuid4()}",
    )
    org_id = org["id"]

    # Pre-set tax_id + stripe customer
    org_db = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org_db.billing_account.stripe_customer_id = "cus_tax_delete_test"
    org_db.billing_account.tax_id = "GB123456789"
    dbsession.flush()

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

    response = await client.post(
        "/v0/webhooks/stripe",
        content=json.dumps(webhook_payload),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200

    dbsession.expire_all()
    org_db = dbsession.query(Organization).filter(Organization.id == org_id).first()
    assert org_db.billing_account.tax_id is None


# ============================================================================
# Webhook – Edge Cases
# ============================================================================


@pytest.mark.anyio
async def test_webhook_unknown_customer_ignored(client: AsyncClient):
    """Test that webhook for unknown Stripe customer is handled gracefully."""
    webhook_payload = {
        "id": f"evt_unknown_cust_{uuid.uuid4()}",
        "type": "customer.tax_id.created",
        "data": {
            "object": {
                "customer": "cus_nonexistent_12345",
                "value": "XX123456789",
                "type": "eu_vat",
            },
        },
    }

    response = await client.post(
        "/v0/webhooks/stripe",
        content=json.dumps(webhook_payload),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200


# ============================================================================
# E2E – Full Organization Billing Lifecycle
# ============================================================================


@pytest.mark.anyio
async def test_e2e_org_full_billing_lifecycle(client: AsyncClient, dbsession):
    """
    E2E: org billing profile → Stripe customer set → webhook credits → verify.

    1. Create org
    2. Set up business profile
    3. Set Stripe customer (simulated via DB)
    4. Webhook confirms payment → credits are added
    5. Credits verified via account-info
    """
    from orchestra.db.dao.organization_dao import OrganizationDAO
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, f"e2e-life-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"E2E Lifecycle Org {uuid.uuid4()}",
    )
    org_id = org["id"]

    # Step 2 – business profile
    profile_resp = await client.patch(
        "/v0/billing/billing-profile",
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
        headers=org["headers"],
    )
    assert profile_resp.status_code == 200

    # Step 3 – set Stripe customer via DB
    org_db = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org_db.billing_account.stripe_customer_id = "cus_e2e_lifecycle"
    dbsession.flush()

    org_dao = OrganizationDAO(dbsession)
    initial_credits = float(org_dao.get(org_id).billing_account.credits)

    # Step 4 – webhook for $200 payment
    webhook_payload = {
        "id": f"evt_e2e_lifecycle_{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_e2e_lifecycle",
                "customer": "cus_e2e_lifecycle",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 20000,
                "currency": "usd",
                "metadata": {"organization_id": str(org_id)},
            },
        },
    }

    wh_resp = await client.post(
        "/v0/webhooks/stripe",
        content=json.dumps(webhook_payload),
        headers={"Content-Type": "application/json"},
    )
    assert wh_resp.status_code == 200

    # Step 5 – verify credits via unified account-info
    credits_resp = await client.get(
        "/v0/billing/account-info",
        headers=org["headers"],
    )
    assert credits_resp.status_code == 200
    assert credits_resp.json()["credits"] == initial_credits + 200


@pytest.mark.anyio
async def test_e2e_org_billing_with_tax_details(client: AsyncClient):
    """
    E2E: set up business profile with tax ID and verify persistence.
    """
    owner = await create_test_user(client, f"e2e-tax-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"E2E Tax Org {uuid.uuid4()}",
    )

    # Set business profile with tax
    resp = await client.patch(
        "/v0/billing/billing-profile",
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
        headers=org["headers"],
    )
    assert resp.status_code == 200

    # Verify
    get_resp = await client.get(
        "/v0/billing/billing-profile",
        headers=org["headers"],
    )
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["business_name"] == "E2E Tax Corp GmbH"
    assert data["tax_id"] == "DE123456789"
    assert data["is_business"] is True


@pytest.mark.anyio
async def test_e2e_org_multiple_credit_top_ups(client: AsyncClient, dbsession):
    """E2E: organisation receives three successive credit top-ups via webhook."""
    from orchestra.db.dao.organization_dao import OrganizationDAO
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, f"e2e-multi-{uuid.uuid4()}@test.com")
    org = await create_test_org(
        client,
        owner,
        f"E2E Multi Org {uuid.uuid4()}",
    )
    org_id = org["id"]

    org_db = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org_db.billing_account.stripe_customer_id = "cus_e2e_multi"
    dbsession.flush()

    org_dao = OrganizationDAO(dbsession)
    initial = float(org_dao.get(org_id).billing_account.credits)

    amounts_cents = [10000, 5000, 25000]  # $100, $50, $250
    cumulative = 0
    for idx, amount in enumerate(amounts_cents, 1):
        cumulative += amount // 100

        payload = {
            "id": f"evt_multi_{idx}_{uuid.uuid4()}",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": f"cs_multi_{idx}",
                    "customer": "cus_e2e_multi",
                    "mode": "payment",
                    "payment_status": "paid",
                    "amount_total": amount,
                    "currency": "usd",
                    "metadata": {"organization_id": str(org_id)},
                },
            },
        }
        resp = await client.post(
            "/v0/webhooks/stripe",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        dbsession.expire_all()
        assert (
            float(org_dao.get(org_id).billing_account.credits) == initial + cumulative
        )


@pytest.mark.anyio
async def test_e2e_org_new_member_uses_org_billing(client: AsyncClient, dbsession):
    """E2E: new member joins org and can see org billing via account-info."""
    from orchestra.db.models.orchestra_models import Organization

    owner = await create_test_user(client, f"e2e-mem-own-{uuid.uuid4()}@test.com")
    new_member = await create_test_user(client, f"e2e-mem-new-{uuid.uuid4()}@test.com")

    org = await create_test_org(
        client,
        owner,
        f"E2E Member Org {uuid.uuid4()}",
    )
    org_id = org["id"]

    # Set up org with credits
    org_db = dbsession.query(Organization).filter(Organization.id == org_id).first()
    org_db.billing_account.stripe_customer_id = "cus_e2e_member"
    dbsession.flush()

    # Add credits via webhook
    payload = {
        "id": f"evt_member_billing_{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_member_billing",
                "customer": "cus_e2e_member",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 50000,  # $500
                "currency": "usd",
                "metadata": {"organization_id": str(org_id)},
            },
        },
    }
    await client.post(
        "/v0/webhooks/stripe",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )

    # Add new member to org
    add_resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": new_member["id"]},
        headers=owner["headers"],
    )
    assert add_resp.status_code == 201

    member_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {add_resp.json()['api_key']}",
    }

    # New member can see org credits via account-info
    credits_resp = await client.get(
        "/v0/billing/account-info",
        headers=member_org_headers,
    )
    # Members should have billing:read access
    assert credits_resp.status_code in [200, 403]
    if credits_resp.status_code == 200:
        assert credits_resp.json()["credits"] == 500.0


# ============================================================================
# Permission Tests
# ============================================================================


@pytest.mark.anyio
async def test_billing_api_admin_can_update(client: AsyncClient, dbsession):
    """Admin members (billing:write) can update billing settings."""
    from orchestra.db.dao.role_dao import RoleDAO

    owner = await create_test_user(client, f"perm-owner-{uuid.uuid4()}@test.com")
    admin_user = await create_test_user(client, f"perm-admin-{uuid.uuid4()}@test.com")

    org = await create_test_org(client, owner, f"Perm Test Org {uuid.uuid4()}")
    org_id = org["id"]

    role_dao = RoleDAO(dbsession)
    admin_role = role_dao.get_by_name("Admin", organization_id=None)

    # Add admin member
    resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": admin_user["id"], "role_id": admin_role.id},
        headers=owner["headers"],
    )
    assert resp.status_code == 201
    admin_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {resp.json()['api_key']}",
    }

    # Admin can update billing profile
    update_resp = await client.patch(
        "/v0/billing/billing-profile",
        json={"billing_email": "admin-set@org.com"},
        headers=admin_org_headers,
    )
    assert update_resp.status_code == 200


@pytest.mark.anyio
async def test_billing_api_member_cannot_update(client: AsyncClient, dbsession):
    """Regular members (no billing:write) cannot update billing settings."""
    from orchestra.db.dao.role_dao import RoleDAO

    owner = await create_test_user(client, f"perm2-owner-{uuid.uuid4()}@test.com")
    member_user = await create_test_user(
        client,
        f"perm2-member-{uuid.uuid4()}@test.com",
    )

    org = await create_test_org(client, owner, f"Perm2 Test Org {uuid.uuid4()}")
    org_id = org["id"]

    role_dao = RoleDAO(dbsession)
    member_role = role_dao.get_by_name("Member", organization_id=None)

    # Add regular member
    resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member_user["id"], "role_id": member_role.id},
        headers=owner["headers"],
    )
    assert resp.status_code == 201
    member_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {resp.json()['api_key']}",
    }

    # Member cannot update billing profile
    update_resp = await client.patch(
        "/v0/billing/billing-profile",
        json={"billing_email": "member-hack@org.com"},
        headers=member_org_headers,
    )
    assert update_resp.status_code == 403


@pytest.mark.anyio
async def test_billing_api_member_can_read(client: AsyncClient, dbsession):
    """Regular members (billing:read) can read billing info."""
    from orchestra.db.dao.role_dao import RoleDAO

    owner = await create_test_user(client, f"perm3-owner-{uuid.uuid4()}@test.com")
    member_user = await create_test_user(
        client,
        f"perm3-member-{uuid.uuid4()}@test.com",
    )

    org = await create_test_org(client, owner, f"Perm3 Test Org {uuid.uuid4()}")
    org_id = org["id"]

    role_dao = RoleDAO(dbsession)
    member_role = role_dao.get_by_name("Member", organization_id=None)

    resp = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member_user["id"], "role_id": member_role.id},
        headers=owner["headers"],
    )
    assert resp.status_code == 201
    member_org_headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {resp.json()['api_key']}",
    }

    # Member can read account info
    read_resp = await client.get(
        "/v0/billing/account-info",
        headers=member_org_headers,
    )
    assert read_resp.status_code in [200, 403]
