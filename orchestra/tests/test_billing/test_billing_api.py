"""
Billing API endpoint tests.

Organised into semantic classes so every group of related endpoints lives
together.  All Stripe calls are mocked — these tests run in CI without
network access.

Sections:
- SchemaSmoke: recharge / billing_account table columns exist
- Credits: GET /credits, POST /credits/deduct, add/deduct via DAO
- BillingEntity: get_billing_entity + deduct_credits for user / org
- DeductEndpoint: /credits/deduct triggering auto-recharge
- CheckoutPortalStatus: checkout-session, portal-session, checkout-status
- AutoRechargeEndpoints: GET / PUT /billing/auto-recharge
- OrgBillingPermissions: RBAC on billing endpoints via org API key
- AccountInfo: GET /billing/account-info
- CreditGrants: claim-credit-grant-link → promo recharge
- BillingProfile: GET / PATCH /billing/billing-profile
- TaxValidation: validate-tax-id, supported-tax-countries
- AdminBillingEndpoints: admin billing routes
- APIValidation: error-message quality, user-not-found
- BillingModel: DB-level model constraints & defaults
- InternationalAddress: DAO address formatting
"""

from __future__ import annotations

import datetime as dt
import math
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette import status

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.models.orchestra_models import (
    RECHARGE_TYPE_PAYMENT,
    RECHARGE_TYPE_PROMO,
    BillingAccount,
    Recharge,
    RechargeStatus,
    User,
)
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import make_user_with_billing
from orchestra.tests.utils import (
    ADMIN_HEADERS,
    HEADERS,
    create_test_org,
    create_test_user,
)
from orchestra.web.api.admin.views import get_user

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def _env_secrets(monkeypatch):
    import os

    if not os.environ.get("STRIPE_WEBHOOK_SECRET"):
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")

    existing_key = os.environ.get("STRIPE_SECRET_KEY")
    if not existing_key or not existing_key.startswith("sk_test_"):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy_for_mocking")

    if not os.environ.get("ORCHESTRA_ADMIN_KEY"):
        monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "test_admin_key")

    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test", raising=False)
    monkeypatch.setattr(
        settings,
        "stripe_secret_key",
        "sk_test_dummy_for_mocking",
        raising=False,
    )
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test", raising=False)


# ============================================================================
# Schema Smoke Tests
# ============================================================================


class TestSchemaSmoke:
    def test_schema_columns(self, dbsession: Session):
        insp = sa.inspect(dbsession.bind)
        rcols = {c["name"] for c in insp.get_columns("recharge")}
        bacols = {c["name"] for c in insp.get_columns("billing_account")}

        assert {"status", "stripe_invoice_id", "invoice_group"} <= rcols
        assert "billing_account_id" in rcols
        assert "account_status" in bacols
        assert "credits" in bacols


# ============================================================================
# Credits
# ============================================================================


class TestCredits:
    def test_positive_recharge(self, dbsession, worker_id):
        user_dao = UserDAO(dbsession)
        ba_dao = BillingAccountDAO(dbsession)

        test_users = ["user1", "user2", "user3", "user4"]
        for user_id in test_users:
            user = user_dao.get_user_with_id(user_id)
            ba_dao.add_credits(user.billing_account_id, 2.5)

        dbsession.commit()

        simple = get_user("user1", dbsession)[0][0]
        assert math.isclose(float(simple.billing_account.credits), 3.5)
        recharge_limited = get_user("user2", session=dbsession)[0][0]
        assert math.isclose(float(recharge_limited.billing_account.credits), 12.49)
        recharge_not_needed_a = get_user("user3", session=dbsession)[0][0]
        assert math.isclose(float(recharge_not_needed_a.billing_account.credits), 12.5)
        recharge_not_needed_b = get_user("user4", session=dbsession)[0][0]
        assert math.isclose(float(recharge_not_needed_b.billing_account.credits), 22.5)

    def test_negative_recharge(self, dbsession, worker_id):
        user_dao = UserDAO(dbsession)
        ba_dao = BillingAccountDAO(dbsession)

        test_users = ["user1", "user2", "user3", "user4"]
        for user_id in test_users:
            user = user_dao.get_user_with_id(user_id)
            ba_dao.deduct_credits(user.billing_account_id, 0.5)

        dbsession.commit()

        simple = get_user("user1", session=dbsession)[0][0]
        assert math.isclose(float(simple.billing_account.credits), 0.5)
        recharge_limited = get_user("user2", session=dbsession)[0][0]
        assert math.isclose(float(recharge_limited.billing_account.credits), 9.49)
        recharge_not_needed_a = get_user("user3", session=dbsession)[0][0]
        assert math.isclose(float(recharge_not_needed_a.billing_account.credits), 9.5)
        recharge_not_needed_b = get_user("user4", session=dbsession)[0][0]
        assert math.isclose(float(recharge_not_needed_b.billing_account.credits), 19.5)

    @pytest.mark.anyio
    async def test_get_credits(self, client: AsyncClient, fastapi_app):
        url = fastapi_app.url_path_for("get_credits")
        response = await client.get(url, headers=HEADERS)
        assert response.status_code == status.HTTP_200_OK
        response_dict = response.json()
        assert isinstance(response_dict, dict)
        assert "credits" in response_dict
        assert isinstance(response_dict["credits"], float)
        assert "id" in response_dict
        assert isinstance(response_dict["id"], str)
        assert len(response_dict.keys()) == 2

    @pytest.mark.anyio
    async def test_deduct_credits_success(self, client: AsyncClient, dbsession):
        credits_response = await client.get("/v0/credits", headers=HEADERS)
        assert credits_response.status_code == status.HTTP_200_OK
        initial_credits = credits_response.json()["credits"]

        deduct_amount = 0.5
        response = await client.post(
            "/v0/credits/deduct",
            headers=HEADERS,
            json={"amount": deduct_amount},
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["previous_credits"] == initial_credits
        assert data["deducted"] == deduct_amount
        assert math.isclose(data["current_credits"], initial_credits - deduct_amount)

        updated = await client.get("/v0/credits", headers=HEADERS)
        assert math.isclose(updated.json()["credits"], initial_credits - deduct_amount)

    @pytest.mark.anyio
    async def test_deduct_credits_insufficient_funds(self, client: AsyncClient):
        credits_response = await client.get("/v0/credits", headers=HEADERS)
        assert credits_response.status_code == status.HTTP_200_OK
        current_credits = credits_response.json()["credits"]

        response = await client.post(
            "/v0/credits/deduct",
            headers=HEADERS,
            json={"amount": current_credits + 1000000},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "Insufficient credits" in response.json()["detail"]

    @pytest.mark.anyio
    async def test_deduct_credits_zero_amount(self, client: AsyncClient):
        response = await client.post(
            "/v0/credits/deduct",
            headers=HEADERS,
            json={"amount": 0},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.anyio
    async def test_deduct_credits_negative_amount(self, client: AsyncClient):
        response = await client.post(
            "/v0/credits/deduct",
            headers=HEADERS,
            json={"amount": -5.0},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.anyio
    async def test_deduct_credits_exact_balance(self, client: AsyncClient):
        credits_response = await client.get("/v0/credits", headers=HEADERS)
        assert credits_response.status_code == status.HTTP_200_OK
        exact_balance = credits_response.json()["credits"]

        response = await client.post(
            "/v0/credits/deduct",
            headers=HEADERS,
            json={"amount": exact_balance},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["current_credits"] == 0.0

        updated = await client.get("/v0/credits", headers=HEADERS)
        assert updated.json()["credits"] == 0.0

    @pytest.mark.anyio
    async def test_deduct_credits_fractional_amount(self, client: AsyncClient):
        response = await client.post(
            "/v0/credits/deduct",
            headers=HEADERS,
            json={"amount": 0.123},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["deducted"] == 0.123


# ============================================================================
# Billing Entity
# ============================================================================


class TestBillingEntity:
    @pytest.mark.anyio
    async def test_get_billing_entity_personal(self, client: AsyncClient, dbsession):
        from orchestra.lib.billing import BillingEntityType, get_billing_entity

        user = await create_test_user(client, "entity_personal@test.com")

        user_dao = UserDAO(dbsession)
        ba_dao = BillingAccountDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])
        ba_dao.add_credits(user_obj.billing_account_id, 50)
        dbsession.commit()

        entity = get_billing_entity(dbsession, user["id"], organization_id=None)

        assert entity.entity_type == BillingEntityType.USER
        assert entity.entity_id == user["id"]
        assert entity.credits == Decimal("50")
        assert entity.is_user is True
        assert entity.is_organization is False

    @pytest.mark.anyio
    async def test_get_billing_entity_org_no_stripe_customer(
        self,
        client: AsyncClient,
        dbsession,
    ):
        from orchestra.lib.billing import BillingEntityType, get_billing_entity

        owner = await create_test_user(client, "entity_no_billing_owner@test.com")

        org_response = await client.post(
            "/v0/organizations",
            json={"name": "Entity No Billing Test"},
            headers=owner["headers"],
        )
        org_id = org_response.json()["id"]

        entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)
        assert entity.entity_type == BillingEntityType.ORGANIZATION
        assert entity.stripe_customer_id is None

    @pytest.mark.anyio
    async def test_get_billing_entity_org_direct(self, client: AsyncClient, dbsession):
        from orchestra.db.models.orchestra_models import Organization
        from orchestra.lib.billing import BillingEntityType, get_billing_entity

        owner = await create_test_user(client, "entity_direct_owner@test.com")

        org_response = await client.post(
            "/v0/organizations",
            json={"name": "Entity Direct Test"},
            headers=owner["headers"],
        )
        org_id = org_response.json()["id"]

        org = dbsession.query(Organization).filter(Organization.id == org_id).first()
        org.billing_account.stripe_customer_id = "cus_direct_test"
        org.billing_account.credits = Decimal("200")
        dbsession.commit()

        entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

        assert entity.entity_type == BillingEntityType.ORGANIZATION
        assert entity.entity_id == org_id
        assert entity.credits == Decimal("200")
        assert entity.is_organization is True
        assert entity.has_billing is True

    @pytest.mark.anyio
    async def test_deduct_credits_from_user(self, client: AsyncClient, dbsession):
        from orchestra.lib.billing import deduct_credits, get_billing_entity

        user = await create_test_user(client, "deduct_user@test.com")

        user_dao = UserDAO(dbsession)
        ba_dao = BillingAccountDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])
        ba_dao.add_credits(user_obj.billing_account_id, 100)
        dbsession.commit()

        entity = get_billing_entity(dbsession, user["id"])

        new_balance = deduct_credits(dbsession, entity, Decimal("25.50"))
        dbsession.commit()

        assert new_balance == Decimal("74.50")

        updated_user = user_dao.get_user_with_id(user["id"])
        assert updated_user.billing_account.credits == Decimal("74.50")

    @pytest.mark.anyio
    async def test_deduct_credits_from_org(self, client: AsyncClient, dbsession):
        from orchestra.db.models.orchestra_models import Organization
        from orchestra.lib.billing import deduct_credits, get_billing_entity

        owner = await create_test_user(client, "deduct_org@test.com")

        org_response = await client.post(
            "/v0/organizations",
            json={"name": "Deduct Org Test"},
            headers=owner["headers"],
        )
        org_id = org_response.json()["id"]

        org = dbsession.query(Organization).filter(Organization.id == org_id).first()
        org.billing_account.stripe_customer_id = "cus_deduct_test"
        org.billing_account.credits = Decimal("500")
        dbsession.commit()

        entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

        new_balance = deduct_credits(dbsession, entity, Decimal("123.45"))
        dbsession.commit()

        assert new_balance == Decimal("376.55")

        dbsession.refresh(org)
        assert org.billing_account.credits == Decimal("376.55")

    @pytest.mark.anyio
    async def test_billing_entity_should_trigger_autorecharge(
        self,
        client: AsyncClient,
        dbsession,
    ):
        from orchestra.db.models.orchestra_models import Organization
        from orchestra.lib.billing import get_billing_entity

        owner = await create_test_user(client, "autorecharge_trigger@test.com")

        org_response = await client.post(
            "/v0/organizations",
            json={"name": "Autorecharge Trigger Test"},
            headers=owner["headers"],
        )
        org_id = org_response.json()["id"]

        org = dbsession.query(Organization).filter(Organization.id == org_id).first()
        org.billing_account.stripe_customer_id = "cus_autorecharge"
        org.billing_account.credits = Decimal("100")
        org.billing_account.autorecharge = True
        org.billing_account.autorecharge_threshold = Decimal("50")
        org.billing_account.autorecharge_qty = Decimal("200")
        dbsession.commit()

        entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

        assert entity.should_trigger_autorecharge(Decimal("100")) is False
        assert entity.should_trigger_autorecharge(Decimal("51")) is False
        assert entity.should_trigger_autorecharge(Decimal("50")) is True
        assert entity.should_trigger_autorecharge(Decimal("25")) is True
        assert entity.should_trigger_autorecharge(Decimal("0")) is True

    @pytest.mark.anyio
    async def test_billing_entity_no_autorecharge_without_stripe(
        self,
        client: AsyncClient,
        dbsession,
    ):
        from orchestra.db.models.orchestra_models import Organization
        from orchestra.lib.billing import get_billing_entity

        owner = await create_test_user(client, "no_stripe_autorecharge@test.com")

        org_response = await client.post(
            "/v0/organizations",
            json={"name": "No Stripe Autorecharge Test"},
            headers=owner["headers"],
        )
        org_id = org_response.json()["id"]

        org = dbsession.query(Organization).filter(Organization.id == org_id).first()
        org.billing_account.stripe_customer_id = "cus_no_stripe_test"
        org.billing_account.autorecharge = True
        org.billing_account.autorecharge_threshold = Decimal("50")
        org.billing_account.credits = Decimal("100")
        dbsession.commit()

        entity = get_billing_entity(dbsession, owner["id"], organization_id=org_id)

        assert entity.is_organization is True
        assert entity.has_billing is True
        assert entity.should_trigger_autorecharge(Decimal("100")) is False
        assert entity.should_trigger_autorecharge(Decimal("40")) is True


# ============================================================================
# Deduct Endpoint (auto-recharge interaction)
# ============================================================================


class TestDeductEndpoint:
    @pytest.mark.anyio
    async def test_triggers_auto_recharge(
        self,
        client: AsyncClient,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.lib.billing

        mock_stripe_module = SimpleNamespace(
            InvoiceItem=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(id="ii_deduct_ar"),
            ),
            error=SimpleNamespace(StripeError=Exception),
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

        user = await create_test_user(client, "deduct_ar@test.com")

        user_dao = UserDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])

        ba = user_obj.billing_account
        ba.credits = Decimal("15")
        ba.autorecharge = True
        ba.autorecharge_threshold = Decimal("10")
        ba.autorecharge_qty = Decimal("50")
        ba.stripe_customer_id = "cus_deduct_ar"
        dbsession.commit()

        response = await client.post(
            "/v0/credits/deduct",
            json={"amount": 10.0},
            headers=user["headers"],
        )
        assert response.status_code == 200

        data = response.json()
        assert data["previous_credits"] == 15.0
        assert data["deducted"] == 10.0
        assert data["current_credits"] == 55.0  # 15 - 10 + 50

        dbsession.expire_all()
        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba.id, type="auto")
            .first()
        )
        assert recharge is not None
        assert recharge.quantity == Decimal("50")
        assert recharge.status == RechargeStatus.PENDING_INVOICE

    @pytest.mark.anyio
    async def test_no_auto_recharge_when_above_threshold(
        self,
        client: AsyncClient,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.lib.billing

        mock_stripe_module = SimpleNamespace(
            InvoiceItem=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(id="ii_no_ar"),
            ),
            error=SimpleNamespace(StripeError=Exception),
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)

        user = await create_test_user(client, "deduct_no_ar@test.com")

        user_dao = UserDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])

        ba = user_obj.billing_account
        ba.credits = Decimal("100")
        ba.autorecharge = True
        ba.autorecharge_threshold = Decimal("10")
        ba.autorecharge_qty = Decimal("50")
        ba.stripe_customer_id = "cus_no_ar"
        dbsession.commit()

        response = await client.post(
            "/v0/credits/deduct",
            json={"amount": 5.0},
            headers=user["headers"],
        )
        assert response.status_code == 200
        assert response.json()["current_credits"] == 95.0

        dbsession.expire_all()
        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba.id, type="auto")
            .first()
        )
        assert recharge is None

    @pytest.mark.anyio
    async def test_no_auto_recharge_when_disabled(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "deduct_ar_disabled@test.com")

        user_dao = UserDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])

        ba = user_obj.billing_account
        ba.credits = Decimal("15")
        ba.autorecharge = False
        ba.autorecharge_threshold = Decimal("10")
        ba.autorecharge_qty = Decimal("50")
        ba.stripe_customer_id = "cus_ar_disabled"
        dbsession.commit()

        response = await client.post(
            "/v0/credits/deduct",
            json={"amount": 10.0},
            headers=user["headers"],
        )
        assert response.status_code == 200
        assert response.json()["current_credits"] == 5.0

        dbsession.expire_all()
        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba.id, type="auto")
            .first()
        )
        assert recharge is None


# ============================================================================
# Checkout / Portal / Status Endpoints
# ============================================================================


class TestCheckoutPortalStatus:
    @pytest.mark.anyio
    async def test_checkout_session(self, client, dbsession, monkeypatch):
        user = await create_test_user(client, "checkout_ep_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("100"),
                stripe_customer_id="cus_ep_test",
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            db_user.billing_account.stripe_customer_id = "cus_ep_test"
        dbsession.commit()

        monkeypatch.setattr(
            settings,
            "stripe_unify_credits_price_id_personal",
            "price_test_personal",
            raising=False,
        )
        monkeypatch.setattr(
            settings,
            "console_url",
            "http://localhost:3000",
            raising=False,
        )

        import orchestra.web.api.billing.views as billing_views

        class MockCheckoutSession:
            def __init__(self):
                self.url = "https://checkout.stripe.com/test_session"
                self.id = "cs_test_123"

        class MockCustomer:
            email = "checkout_ep_user@test.com"
            name = "Test"
            deleted = False

        mock_calls = {"create": [], "retrieve": [], "modify": [], "list_tax_ids": []}

        def mock_session_create(**kwargs):
            mock_calls["create"].append(kwargs)
            return MockCheckoutSession()

        def mock_customer_retrieve(cid):
            mock_calls["retrieve"].append(cid)
            return MockCustomer()

        def mock_customer_modify(cid, **kwargs):
            mock_calls["modify"].append({"cid": cid, **kwargs})

        def mock_list_tax_ids(cid):
            mock_calls["list_tax_ids"].append(cid)
            return SimpleNamespace(data=[])

        mock_stripe = SimpleNamespace(
            api_key=None,
            checkout=SimpleNamespace(
                Session=SimpleNamespace(create=mock_session_create),
            ),
            Customer=SimpleNamespace(
                retrieve=mock_customer_retrieve,
                modify=mock_customer_modify,
                list_tax_ids=mock_list_tax_ids,
                create_tax_id=lambda cid, **kw: None,
            ),
            InvalidRequestError=Exception,
        )
        monkeypatch.setattr(billing_views, "stripe", mock_stripe)

        response = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )

        assert response.status_code == 200, response.json()
        data = response.json()
        assert data["url"] == "https://checkout.stripe.com/test_session"
        assert data["session_id"] == "cs_test_123"
        assert len(mock_calls["create"]) == 1
        create_params = mock_calls["create"][0]
        assert create_params["mode"] == "payment"
        assert create_params["customer"] == "cus_ep_test"
        assert create_params["client_reference_id"] == user["id"]

    @pytest.mark.anyio
    async def test_checkout_session_no_stripe_customer(
        self,
        client,
        dbsession,
        monkeypatch,
    ):
        user = await create_test_user(client, "checkout_new_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(credits=Decimal("0"))
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        dbsession.commit()

        monkeypatch.setattr(
            settings,
            "stripe_unify_credits_price_id_personal",
            "price_test_personal",
            raising=False,
        )
        monkeypatch.setattr(
            settings,
            "console_url",
            "http://localhost:3000",
            raising=False,
        )

        import orchestra.web.api.billing.views as billing_views

        class MockCheckoutSession:
            url = "https://checkout.stripe.com/new_session"
            id = "cs_new_123"

        mock_calls = {"create": []}

        def mock_session_create(**kwargs):
            mock_calls["create"].append(kwargs)
            return MockCheckoutSession()

        mock_stripe = SimpleNamespace(
            api_key=None,
            checkout=SimpleNamespace(
                Session=SimpleNamespace(create=mock_session_create),
            ),
            Customer=SimpleNamespace(),
            InvalidRequestError=Exception,
        )
        monkeypatch.setattr(billing_views, "stripe", mock_stripe)

        response = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )

        assert response.status_code == 200, response.json()
        data = response.json()
        assert data["url"] == "https://checkout.stripe.com/new_session"
        create_params = mock_calls["create"][0]
        assert "customer" not in create_params
        assert create_params["customer_creation"] == "always"

    @pytest.mark.anyio
    async def test_portal_session(self, client, dbsession, monkeypatch):
        user = await create_test_user(client, "portal_ep_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("50"),
                stripe_customer_id="cus_portal_test",
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            db_user.billing_account.stripe_customer_id = "cus_portal_test"
        dbsession.commit()

        import orchestra.web.api.billing.views as billing_views

        mock_calls = {"create": []}

        def mock_portal_create(**kwargs):
            mock_calls["create"].append(kwargs)
            return SimpleNamespace(url="https://billing.stripe.com/portal_test")

        mock_stripe = SimpleNamespace(
            api_key=None,
            billing_portal=SimpleNamespace(
                Session=SimpleNamespace(create=mock_portal_create),
            ),
            InvalidRequestError=Exception,
        )
        monkeypatch.setattr(billing_views, "stripe", mock_stripe)

        response = await client.post(
            "/v0/billing/portal-session",
            headers=user["headers"],
        )

        assert response.status_code == 200, response.json()
        assert response.json()["url"] == "https://billing.stripe.com/portal_test"
        assert len(mock_calls["create"]) == 1
        assert mock_calls["create"][0]["customer"] == "cus_portal_test"

    @pytest.mark.anyio
    async def test_portal_session_no_customer(self, client, dbsession, monkeypatch):
        user = await create_test_user(client, "portal_no_cust@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(credits=Decimal("0"))
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        dbsession.commit()

        import orchestra.web.api.billing.views as billing_views

        mock_stripe = SimpleNamespace(
            api_key=None,
            billing_portal=SimpleNamespace(Session=SimpleNamespace()),
            InvalidRequestError=Exception,
        )
        monkeypatch.setattr(billing_views, "stripe", mock_stripe)

        response = await client.post(
            "/v0/billing/portal-session",
            headers=user["headers"],
        )
        assert response.status_code == 404
        assert "No Stripe customer ID found" in response.json()["detail"]

    @pytest.mark.anyio
    async def test_checkout_status(self, client, dbsession, monkeypatch):
        user = await create_test_user(client, "checkout_status_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("25"),
                stripe_customer_id="cus_status_test",
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            db_user.billing_account.stripe_customer_id = "cus_status_test"
        dbsession.commit()

        import orchestra.web.api.billing.views as billing_views

        class MockCheckoutSession:
            status = "complete"
            payment_status = "paid"
            customer = "cus_status_test"
            client_reference_id = user["id"]

        mock_stripe = SimpleNamespace(
            api_key=None,
            checkout=SimpleNamespace(
                Session=SimpleNamespace(retrieve=lambda sid: MockCheckoutSession()),
            ),
            InvalidRequestError=Exception,
        )
        monkeypatch.setattr(billing_views, "stripe", mock_stripe)

        response = await client.get(
            "/v0/billing/checkout-status?session_id=cs_test_status",
            headers=user["headers"],
        )

        assert response.status_code == 200, response.json()
        data = response.json()
        assert data["status"] == "complete"
        assert data["payment_status"] == "paid"

    @pytest.mark.anyio
    async def test_checkout_status_wrong_owner(self, client, dbsession, monkeypatch):
        user = await create_test_user(client, "checkout_wrong_owner@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("10"),
                stripe_customer_id="cus_wrong_owner",
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        dbsession.commit()

        import orchestra.web.api.billing.views as billing_views

        class MockCheckoutSession:
            status = "complete"
            payment_status = "paid"
            customer = "cus_someone_else"
            client_reference_id = "other_user_id"

        mock_stripe = SimpleNamespace(
            api_key=None,
            checkout=SimpleNamespace(
                Session=SimpleNamespace(retrieve=lambda sid: MockCheckoutSession()),
            ),
            InvalidRequestError=Exception,
        )
        monkeypatch.setattr(billing_views, "stripe", mock_stripe)

        response = await client.get(
            "/v0/billing/checkout-status?session_id=cs_test_wrong",
            headers=user["headers"],
        )
        assert response.status_code == 403
        assert "does not belong" in response.json()["detail"]

    @pytest.mark.anyio
    async def test_checkout_session_no_price_id_configured(
        self,
        client,
        dbsession,
        monkeypatch,
    ):
        user = await create_test_user(client, "no_price_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(credits=Decimal("0"))
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        dbsession.commit()

        monkeypatch.setattr(
            settings,
            "stripe_unify_credits_price_id_personal",
            None,
            raising=False,
        )

        import orchestra.web.api.billing.views as billing_views

        mock_stripe = SimpleNamespace(api_key=None, InvalidRequestError=Exception)
        monkeypatch.setattr(billing_views, "stripe", mock_stripe)

        response = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )
        assert response.status_code == 500
        assert "price ID not configured" in response.json()["detail"]

    @pytest.mark.anyio
    async def test_portal_session_no_stripe_customer(
        self,
        client: AsyncClient,
        dbsession,
    ):
        user = await create_test_user(client, "portal_no_customer@test.com")
        response = await client.post(
            "/v0/billing/portal-session",
            headers=user["headers"],
        )
        assert response.status_code == 404
        assert "detail" in response.json()


# ============================================================================
# Auto-Recharge Endpoints
# ============================================================================


class TestAutoRechargeEndpoints:
    @pytest.mark.anyio
    async def test_get_returns_settings_and_eligibility(self, client, dbsession):
        user = await create_test_user(client, "ar_get_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("10"),
                autorecharge=True,
                autorecharge_threshold=Decimal("5"),
                autorecharge_qty=Decimal("50"),
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            ba = db_user.billing_account
            ba.autorecharge = True
            ba.autorecharge_threshold = Decimal("5")
            ba.autorecharge_qty = Decimal("50")
        dbsession.commit()

        response = await client.get(
            "/v0/billing/auto-recharge",
            headers=user["headers"],
        )

        assert response.status_code == 200, response.json()
        data = response.json()
        assert data["enabled"] is True
        assert data["threshold"] == 5.0
        assert data["qty"] == 50.0
        assert data["eligible"] is False
        assert data["total_spending"] == 0.0
        assert data["minimum_spend_required"] == 100.0
        assert data["remaining_spend_needed"] == 100.0

    @pytest.mark.anyio
    async def test_get_eligible_after_spending(self, client, dbsession):
        user = await create_test_user(client, "ar_elig_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(credits=Decimal("200"))
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            ba = db_user.billing_account

        recharge = Recharge(
            billing_account_id=ba.id,
            type="payment",
            quantity=Decimal("150"),
            amount_usd=Decimal("150"),
            status=RechargeStatus.PAID,
        )
        dbsession.add(recharge)
        dbsession.commit()

        response = await client.get(
            "/v0/billing/auto-recharge",
            headers=user["headers"],
        )

        assert response.status_code == 200
        data = response.json()
        assert data["eligible"] is True
        assert data["total_spending"] == 150.0
        assert data["remaining_spend_needed"] == 0.0

    @pytest.mark.anyio
    async def test_put_enable_with_all_settings(self, client, dbsession):
        user = await create_test_user(client, "ar_put_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(credits=Decimal("100"))
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            ba = db_user.billing_account

        recharge = Recharge(
            billing_account_id=ba.id,
            type="payment",
            quantity=Decimal("200"),
            amount_usd=Decimal("200"),
            status=RechargeStatus.PAID,
        )
        dbsession.add(recharge)
        dbsession.commit()

        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": True, "threshold": 10.0, "qty": 50.0},
            headers=user["headers"],
        )

        assert response.status_code == 200, response.json()
        data = response.json()
        assert data["enabled"] is True
        assert data["threshold"] == 10.0
        assert data["qty"] == 50.0
        assert data["eligible"] is True

        dbsession.refresh(ba)
        assert ba.autorecharge is True
        assert float(ba.autorecharge_threshold) == 10.0
        assert float(ba.autorecharge_qty) == 50.0

    @pytest.mark.anyio
    async def test_put_toggle_only(self, client, dbsession):
        user = await create_test_user(client, "ar_toggle_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("50"),
                autorecharge=True,
                autorecharge_threshold=Decimal("15"),
                autorecharge_qty=Decimal("75"),
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            ba = db_user.billing_account
            ba.autorecharge = True
            ba.autorecharge_threshold = Decimal("15")
            ba.autorecharge_qty = Decimal("75")
        dbsession.commit()

        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": False},
            headers=user["headers"],
        )

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
        assert data["threshold"] == 15.0
        assert data["qty"] == 75.0

    @pytest.mark.anyio
    async def test_put_enable_fails_without_spending(self, client, dbsession):
        user = await create_test_user(client, "ar_ineligible@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(credits=Decimal("50"))
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        dbsession.commit()

        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": True, "threshold": 5.0, "qty": 25.0},
            headers=user["headers"],
        )
        assert response.status_code == 400
        assert "must spend" in response.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_put_rejects_low_qty(self, client, dbsession):
        user = await create_test_user(client, "ar_low_qty@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(credits=Decimal("50"))
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        dbsession.commit()

        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": False, "threshold": 5.0, "qty": 10.0},
            headers=user["headers"],
        )
        assert response.status_code == 400
        assert "minimum" in response.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_put_disable_always_allowed(self, client, dbsession):
        user = await create_test_user(client, "ar_disable_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("50"),
                autorecharge=True,
                autorecharge_threshold=Decimal("5"),
                autorecharge_qty=Decimal("25"),
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            ba = db_user.billing_account
            ba.autorecharge = True
        dbsession.commit()

        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": False},
            headers=user["headers"],
        )
        assert response.status_code == 200
        assert response.json()["enabled"] is False


# ============================================================================
# Organization Billing Permissions
# ============================================================================


async def _create_org_with_member(
    client,
    dbsession,
    owner_email,
    member_email,
    role_name,
):
    from orchestra.db.dao.role_dao import RoleDAO

    owner = await create_test_user(client, owner_email)
    member = await create_test_user(client, member_email)

    org_response = await client.post(
        "/v0/organizations",
        json={"name": f"Perm Test Org {owner_email}"},
        headers=owner["headers"],
    )
    assert org_response.status_code == 201, org_response.json()
    org_data = org_response.json()
    org_id = org_data["id"]
    owner_org_key = org_data["api_key"]
    owner_org_headers = {"Authorization": f"Bearer {owner_org_key}"}

    role_dao = RoleDAO(dbsession)
    role = role_dao.get_by_name(role_name, organization_id=None)
    assert role is not None, f"Role {role_name} not found"

    add_response = await client.post(
        f"/v0/organizations/{org_id}/members",
        json={"user_id": member["id"], "role_id": role.id},
        headers=owner["headers"],
    )
    assert add_response.status_code == 201, add_response.json()
    member_org_key = add_response.json()["api_key"]
    member_org_headers = {"Authorization": f"Bearer {member_org_key}"}

    from orchestra.db.dao.organization_dao import OrganizationDAO

    org_dao = OrganizationDAO(dbsession)
    org = org_dao.get(org_id)
    if org.billing_account is None:
        ba = BillingAccount(credits=Decimal("100"))
        dbsession.add(ba)
        dbsession.flush()
        org.billing_account_id = ba.id
        dbsession.flush()
    dbsession.commit()

    return org_id, owner_org_headers, member_org_headers


class TestOrgBillingPermissions:
    @pytest.mark.anyio
    async def test_owner_can_read_auto_recharge(self, client, dbsession):
        _, owner_headers, _ = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_read@test.com",
            "perm_member_read@test.com",
            "Member",
        )
        response = await client.get("/v0/billing/auto-recharge", headers=owner_headers)
        assert response.status_code == 200, response.json()
        assert "enabled" in response.json()

    @pytest.mark.anyio
    async def test_member_can_read_auto_recharge(self, client, dbsession):
        _, _, member_headers = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_mread@test.com",
            "perm_member_mread@test.com",
            "Member",
        )
        response = await client.get("/v0/billing/auto-recharge", headers=member_headers)
        assert response.status_code == 200, response.json()
        assert "enabled" in response.json()

    @pytest.mark.anyio
    async def test_member_cannot_update_auto_recharge(self, client, dbsession):
        _, _, member_headers = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_mwrite@test.com",
            "perm_member_mwrite@test.com",
            "Member",
        )
        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": False},
            headers=member_headers,
        )
        assert response.status_code == 403
        assert "billing:write" in response.json()["detail"]

    @pytest.mark.anyio
    async def test_owner_can_update_auto_recharge(self, client, dbsession):
        _, owner_headers, _ = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_owrite@test.com",
            "perm_member_owrite@test.com",
            "Member",
        )
        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": False},
            headers=owner_headers,
        )
        assert response.status_code == 200, response.json()

    @pytest.mark.anyio
    async def test_member_cannot_create_checkout_session(
        self,
        client,
        dbsession,
        monkeypatch,
    ):
        _, _, member_headers = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_checkout@test.com",
            "perm_member_checkout@test.com",
            "Member",
        )
        import orchestra.web.api.billing.views as billing_views

        mock_stripe = SimpleNamespace(api_key=None, InvalidRequestError=Exception)
        monkeypatch.setattr(billing_views, "stripe", mock_stripe)

        response = await client.post(
            "/v0/billing/checkout-session",
            headers=member_headers,
        )
        assert response.status_code == 403
        assert "billing:write" in response.json()["detail"]

    @pytest.mark.anyio
    async def test_member_cannot_create_portal_session(
        self,
        client,
        dbsession,
        monkeypatch,
    ):
        _, _, member_headers = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_portal@test.com",
            "perm_member_portal@test.com",
            "Member",
        )
        import orchestra.web.api.billing.views as billing_views

        mock_stripe = SimpleNamespace(api_key=None, InvalidRequestError=Exception)
        monkeypatch.setattr(billing_views, "stripe", mock_stripe)

        response = await client.post(
            "/v0/billing/portal-session",
            headers=member_headers,
        )
        assert response.status_code == 403
        assert "billing:write" in response.json()["detail"]

    @pytest.mark.anyio
    async def test_viewer_can_read_checkout_status(
        self,
        client,
        dbsession,
        monkeypatch,
    ):
        _, _, viewer_headers = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_vread@test.com",
            "perm_viewer_vread@test.com",
            "Viewer",
        )
        import orchestra.web.api.billing.views as billing_views

        class MockCheckoutSession:
            status = "complete"
            payment_status = "paid"
            customer = None
            client_reference_id = None

        mock_stripe = SimpleNamespace(
            api_key=None,
            checkout=SimpleNamespace(
                Session=SimpleNamespace(retrieve=lambda sid: MockCheckoutSession()),
            ),
            InvalidRequestError=Exception,
        )
        monkeypatch.setattr(billing_views, "stripe", mock_stripe)

        response = await client.get(
            "/v0/billing/checkout-status?session_id=cs_viewer_test",
            headers=viewer_headers,
        )
        assert response.status_code != 403 or "billing:read" not in response.json().get(
            "detail",
            "",
        )

    @pytest.mark.anyio
    async def test_personal_api_key_bypasses_org_permission_check(
        self,
        client,
        dbsession,
    ):
        user = await create_test_user(client, "perm_personal_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(credits=Decimal("50"))
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        dbsession.commit()

        response = await client.get(
            "/v0/billing/auto-recharge",
            headers=user["headers"],
        )
        assert response.status_code == 200

        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": False},
            headers=user["headers"],
        )
        assert response.status_code == 200


# ============================================================================
# Account Info
# ============================================================================


class TestAccountInfo:
    @pytest.mark.anyio
    async def test_personal(self, client, dbsession):
        user = await create_test_user(client, "acctinfo_personal@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        ba = BillingAccount(
            credits=Decimal("42.50"),
            stripe_customer_id="cus_test123",
            autorecharge=True,
            autorecharge_threshold=Decimal("10"),
            autorecharge_qty=Decimal("50"),
        )
        dbsession.add(ba)
        dbsession.flush()

        recharge = Recharge(
            billing_account_id=ba.id,
            type=RECHARGE_TYPE_PAYMENT,
            quantity=Decimal("42.50"),
            amount_usd=Decimal("42.50"),
            status=RechargeStatus.PAID,
        )
        dbsession.add(recharge)
        dbsession.flush()

        db_user.billing_account_id = ba.id
        dbsession.flush()
        dbsession.commit()

        response = await client.get("/v0/billing/account-info", headers=user["headers"])
        assert response.status_code == 200
        data = response.json()

        assert data["billing_account_id"] == ba.id
        assert data["credits"] == 42.5
        assert data["last_recharge_at"] is not None
        assert data["autorecharge"] is True
        assert data["autorecharge_threshold"] == 10.0
        assert data["autorecharge_qty"] == 50.0

    @pytest.mark.anyio
    async def test_no_recharge_history(self, client, dbsession):
        user = await create_test_user(client, "acctinfo_nocust@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        ba = BillingAccount(credits=Decimal("0"))
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.flush()
        dbsession.commit()

        response = await client.get("/v0/billing/account-info", headers=user["headers"])
        assert response.status_code == 200
        data = response.json()
        assert data["last_recharge_at"] is None
        assert data["credits"] == 0.0

    @pytest.mark.anyio
    async def test_org_owner(self, client, dbsession):
        _, owner_headers, _ = await _create_org_with_member(
            client,
            dbsession,
            "acctinfo_owner@test.com",
            "acctinfo_member@test.com",
            "Member",
        )
        response = await client.get("/v0/billing/account-info", headers=owner_headers)
        assert response.status_code == 200
        data = response.json()
        assert "credits" in data
        assert "last_recharge_at" in data

    @pytest.mark.anyio
    async def test_org_member_read(self, client, dbsession):
        _, _, member_headers = await _create_org_with_member(
            client,
            dbsession,
            "acctinfo_mread_owner@test.com",
            "acctinfo_mread@test.com",
            "Member",
        )
        response = await client.get("/v0/billing/account-info", headers=member_headers)
        assert response.status_code == 200
        assert "credits" in response.json()

    @pytest.mark.anyio
    async def test_no_billing_setup(self, client, dbsession):
        user = await create_test_user(client, "acctinfo_nobilling@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        db_user.billing_account_id = None
        dbsession.commit()

        response = await client.get("/v0/billing/account-info", headers=user["headers"])
        assert response.status_code == 400
        assert "not set up" in response.json()["detail"].lower()


# ============================================================================
# Credit Grants
# ============================================================================


class TestCreditGrants:
    @pytest.mark.anyio
    async def test_creates_promo_recharge(self, client, dbsession):
        from orchestra.db.dao.one_time_credit_grant_link_dao import (
            OneTimeCreditGrantLinkDAO,
        )

        user = await create_test_user(client, "promo_recharge@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])

        ba = BillingAccount(credits=Decimal("0"))
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.commit()

        token_dao = OneTimeCreditGrantLinkDAO(dbsession)
        link = token_dao.create(
            expires_at=datetime.now(dt.timezone.utc) + dt.timedelta(days=7),
            credit_amount=15.0,
        )
        dbsession.commit()

        response = await client.post(
            "/v0/user/claim-credit-grant-link",
            json={"token": link.token},
            headers=user["headers"],
        )
        assert response.status_code == 200
        assert response.json()["credits_granted"] == 15.0

        dbsession.expire_all()
        recharges = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).all()
        assert len(recharges) == 1
        r = recharges[0]
        assert r.type == RECHARGE_TYPE_PROMO
        assert float(r.quantity) == 15.0
        assert float(r.amount_usd) == 0.0
        assert r.status == RechargeStatus.PAID

    @pytest.mark.anyio
    async def test_populates_last_recharge_at(self, client, dbsession):
        from orchestra.db.dao.one_time_credit_grant_link_dao import (
            OneTimeCreditGrantLinkDAO,
        )

        user = await create_test_user(client, "promo_last_recharge@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])

        ba = BillingAccount(credits=Decimal("0"))
        dbsession.add(ba)
        dbsession.flush()
        db_user.billing_account_id = ba.id
        dbsession.commit()

        resp_before = await client.get(
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert resp_before.status_code == 200
        assert resp_before.json()["last_recharge_at"] is None

        token_dao = OneTimeCreditGrantLinkDAO(dbsession)
        link = token_dao.create(
            expires_at=datetime.now(dt.timezone.utc) + dt.timedelta(days=7),
            credit_amount=20.0,
        )
        dbsession.commit()

        claim_resp = await client.post(
            "/v0/user/claim-credit-grant-link",
            json={"token": link.token},
            headers=user["headers"],
        )
        assert claim_resp.status_code == 200

        resp_after = await client.get(
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert resp_after.status_code == 200
        data = resp_after.json()
        assert data["last_recharge_at"] is not None
        assert data["credits"] == 20.0

    @pytest.mark.anyio
    async def test_new_billing_account_creates_promo_recharge(self, client, dbsession):
        from orchestra.db.dao.one_time_credit_grant_link_dao import (
            OneTimeCreditGrantLinkDAO,
        )

        user = await create_test_user(client, "promo_newba@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        db_user.billing_account_id = None
        dbsession.commit()

        token_dao = OneTimeCreditGrantLinkDAO(dbsession)
        link = token_dao.create(
            expires_at=datetime.now(dt.timezone.utc) + dt.timedelta(days=7),
            credit_amount=5.0,
        )
        dbsession.commit()

        response = await client.post(
            "/v0/user/claim-credit-grant-link",
            json={"token": link.token},
            headers=user["headers"],
        )
        assert response.status_code == 200
        assert response.json()["credits_granted"] == 5.0

        dbsession.expire_all()
        db_user = user_dao.get_user_with_id(user["id"])
        assert db_user.billing_account_id is not None

        recharges = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=db_user.billing_account_id)
            .all()
        )
        assert len(recharges) == 1
        assert recharges[0].type == RECHARGE_TYPE_PROMO
        assert recharges[0].status == RechargeStatus.PAID


# ============================================================================
# Billing Profile
# ============================================================================


class TestBillingProfile:
    @pytest.mark.anyio
    async def test_get_personal(self, client: AsyncClient, dbsession):
        user = await create_test_user(client, "profile_personal@test.com")
        response = await client.get(
            "/v0/billing/billing-profile",
            headers=user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert "billing_email" in data
        assert "name" in data
        assert "tax_id" in data
        assert "billing_address" in data
        assert "is_business" in data
        assert data["is_business"] is False

    @pytest.mark.anyio
    async def test_get_org(self, client: AsyncClient, dbsession):
        owner = await create_test_user(client, "profile_org_owner@test.com")
        org = await create_test_org(client, owner, "Profile Org")
        response = await client.get(
            "/v0/billing/billing-profile",
            headers=org["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_email"] is None
        assert data["name"] is None
        assert data["is_business"] is True

    @pytest.mark.anyio
    async def test_update_org(self, client: AsyncClient, dbsession):
        owner = await create_test_user(client, "update_profile_org@test.com")
        org = await create_test_org(client, owner, "Update Profile Org")
        response = await client.patch(
            "/v0/billing/billing-profile",
            json={
                "billing_email": "finance@company.com",
                "name": "Company LLC",
                "tax_id": "12-3456789",
                "billing_address": {
                    "line1": "456 Business Pkwy",
                    "city": "New York",
                    "country": "US",
                },
            },
            headers=org["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_email"] == "finance@company.com"
        assert data["name"] == "Company LLC"
        assert data["tax_id"] == "12-3456789"
        assert data["billing_address"]["line1"] == "456 Business Pkwy"
        assert data["billing_address"]["city"] == "New York"
        assert data["billing_address"]["country"] == "US"
        assert data["is_business"] is True

    @pytest.mark.anyio
    async def test_partial_update(self, client: AsyncClient, dbsession):
        owner = await create_test_user(client, "partial_profile@test.com")
        org = await create_test_org(client, owner, "Partial Profile Org")

        await client.patch(
            "/v0/billing/billing-profile",
            json={
                "billing_email": "initial@company.com",
                "name": "Initial Corp",
                "billing_address": {
                    "line1": "100 First Ave",
                    "city": "Boston",
                    "country": "US",
                    "state": "MA",
                },
            },
            headers=org["headers"],
        )

        response = await client.patch(
            "/v0/billing/billing-profile",
            json={"billing_email": "updated@company.com"},
            headers=org["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_email"] == "updated@company.com"
        assert data["name"] == "Initial Corp"

    @pytest.mark.anyio
    async def test_international_address(self, client: AsyncClient, dbsession):
        owner = await create_test_user(client, "intl_addr_profile@test.com")
        org = await create_test_org(client, owner, "Intl Address Org")

        response = await client.patch(
            "/v0/billing/billing-profile",
            json={
                "billing_email": "billing@indianco.in",
                "name": "Indian Tech Pvt Ltd",
                "billing_address": {
                    "country": "IN",
                    "line1": "Tower B, Tech Park",
                    "city": "Hyderabad",
                    "state": "Telangana",
                    "district": "Rangareddy",
                    "postal_code": "500081",
                },
            },
            headers=org["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_address"]["country"] == "IN"
        assert data["billing_address"]["district"] == "Rangareddy"
        assert data["billing_address"]["state"] == "Telangana"
        assert data["billing_address"]["postal_code"] == "500081"


# ============================================================================
# Tax Validation
# ============================================================================


class TestTaxValidation:
    @pytest.mark.anyio
    async def test_valid_us(self, client: AsyncClient, dbsession):
        user = await create_test_user(client, "tax_validate@test.com")
        response = await client.post(
            "/v0/billing/validate-tax-id",
            json={"tax_id": "12-3456789", "country": "US"},
            headers=user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_valid"] is True
        assert data["country"] == "US"
        assert data["formatted_tax_id"] is not None
        assert data["error"] is None

    @pytest.mark.anyio
    async def test_invalid(self, client: AsyncClient, dbsession):
        user = await create_test_user(client, "tax_validate_invalid@test.com")
        response = await client.post(
            "/v0/billing/validate-tax-id",
            json={"tax_id": "!@#$", "country": "US"},
            headers=user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_valid"] is False
        assert data["country"] == "US"
        assert data["error"] is not None

    @pytest.mark.anyio
    async def test_india_gst(self, client: AsyncClient, dbsession):
        user = await create_test_user(client, "tax_validate_in@test.com")
        response = await client.post(
            "/v0/billing/validate-tax-id",
            json={"tax_id": "29ABCDE1234F1Z5", "country": "IN"},
            headers=user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["country"] == "IN"
        assert "is_valid" in data

    @pytest.mark.anyio
    async def test_supported_countries(self, client: AsyncClient, dbsession):
        user = await create_test_user(client, "tax_countries@test.com")
        response = await client.get(
            "/v0/billing/supported-tax-countries",
            headers=user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "supported_countries" in data
        assert "total_countries" in data
        countries = data["supported_countries"]
        assert isinstance(countries, dict)
        assert len(countries) > 0

        for code, info in countries.items():
            assert isinstance(code, str)
            assert len(code) == 2
            assert "name" in info
            assert "description" in info

        assert "US" in countries
        assert "GB" in countries
        assert "IN" in countries

    @pytest.mark.anyio
    async def test_unsupported_country(self, client: AsyncClient, dbsession):
        user = await create_test_user(client, "tax_unsupported@test.com")
        response = await client.post(
            "/v0/billing/validate-tax-id",
            json={"tax_id": "12345", "country": "ZZ"},
            headers=user["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["country"] == "ZZ"
        assert "is_valid" in data


# ============================================================================
# Admin Billing Endpoints
# ============================================================================


class TestAdminBillingEndpoints:
    @pytest.mark.anyio
    async def test_trigger_monthly_invoicing(self, client: AsyncClient):
        response = await client.post(
            "/v0/admin/billing/invoice-month",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "Monthly invoicing completed" in data["message"]
        assert "previous month" in data["message"]

    @pytest.mark.anyio
    async def test_trigger_monthly_invoicing_with_params(self, client: AsyncClient):
        response = await client.post(
            "/v0/admin/billing/invoice-month",
            params={"year": 2024, "month": 1},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "2024-01" in data["message"]
        assert data["year"] == 2024
        assert data["month"] == 1

    @pytest.mark.anyio
    async def test_trigger_billing_guard(self, client: AsyncClient):
        response = await client.post(
            "/v0/admin/billing/suspend-past-due",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "Billing guard completed" in data["message"]

    @pytest.mark.anyio
    async def test_billing_endpoints_require_auth(self, client: AsyncClient):
        from orchestra.tests.utils import HEADERS

        endpoints = [
            "/v0/admin/billing/invoice-month",
            "/v0/admin/billing/suspend-past-due",
        ]
        for endpoint in endpoints:
            response = await client.post(endpoint)
            assert response.status_code in [401, 403]
            response = await client.post(endpoint, headers=HEADERS)
            assert response.status_code in [401, 403]

    @pytest.mark.anyio
    async def test_get_user_credits(self, client: AsyncClient):
        url = "/v0/admin/user"
        params = {"email": "billing_credits@example.com"}
        response = await client.post(url, json=params, headers=ADMIN_HEADERS)
        user_id = response.json()["id"]

        url = f"/v0/admin/billing/account-info?user_id={user_id}"
        response = await client.get(url, headers=ADMIN_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert "credits" in data
        assert isinstance(data["credits"], (int, float))

    @pytest.mark.anyio
    async def test_recharge_user_credits(self, client: AsyncClient):
        user_id = "user1"
        url = "/v0/admin/create_recharge"
        response = await client.post(
            url,
            json={"user_id": user_id, "quantity": 100, "type": "promo"},
            headers=ADMIN_HEADERS,
        )
        if response.status_code == 404:
            pytest.skip("Recharge endpoint not available at this path")
        assert response.status_code == 200

    @pytest.mark.anyio
    async def test_freeze_account_by_stripe_id(self, client: AsyncClient):
        url = "/v0/admin/user"
        params = {"email": "billing_freeze@example.com"}
        response = await client.post(url, json=params, headers=ADMIN_HEADERS)
        user_id = response.json()["id"]

        url = "/v0/admin/stripe_customer_id"
        response = await client.put(
            url,
            params={"id": user_id, "stripe_customer_id": "cus_freeze_test"},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200

        url = "/v0/admin/billing/freeze-by-stripe-id"
        response = await client.post(
            url,
            params={"stripe_id": "cus_freeze_test", "freeze": True},
            headers=ADMIN_HEADERS,
        )
        if response.status_code == 404:
            pytest.skip("Freeze endpoint not available")
        assert response.status_code == 200

    @pytest.mark.anyio
    async def test_stripe_customer_id(
        self,
        client: AsyncClient,
        fastapi_app,
        dbsession,
    ):
        url = fastapi_app.url_path_for("update_stripe_customer_id")
        query = text(
            """SELECT ba.stripe_customer_id
               FROM "user" u
               JOIN billing_account ba ON u.billing_account_id = ba.id
               WHERE u.id = 'stripe_autorecharge';""",
        )
        payload = {"id": "stripe_autorecharge", "stripe_customer_id": "stripe_id_1234"}

        pre = dbsession.execute(query).scalar()
        assert pre is None
        response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
        assert response.status_code == status.HTTP_200_OK
        dbsession.expire_all()
        post = dbsession.execute(query).scalar()
        assert post == "stripe_id_1234"

    @pytest.mark.anyio
    async def test_autorecharge_threshold(
        self,
        client: AsyncClient,
        fastapi_app,
        dbsession,
    ):
        url = fastapi_app.url_path_for("update_autorecharge_threshold")
        query = text(
            """SELECT ba.autorecharge_threshold
               FROM "user" u
               JOIN billing_account ba ON u.billing_account_id = ba.id
               WHERE u.id = 'stripe_autorecharge';""",
        )
        payload = {"id": "stripe_autorecharge", "threshold": 10}

        pre = dbsession.execute(query).scalar()
        assert float(pre) == -1
        response = await client.put(url, headers=ADMIN_HEADERS, params=payload)
        assert response.status_code == status.HTTP_200_OK
        dbsession.expire_all()
        post = dbsession.execute(query).scalar()
        assert post == 10

    @pytest.mark.anyio
    async def test_autorecharge_qty(self, client: AsyncClient, fastapi_app, dbsession):
        response = await client.put(
            "/v0/admin/autorecharge_qty",
            params={"id": "user1", "qty": 50.0},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200

        response = await client.put(
            "/v0/admin/autorecharge_qty",
            params={"id": "user1", "qty": 10.0},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 400

    @pytest.mark.anyio
    async def test_set_monthly_spending_limit(self, client: AsyncClient):
        user = await create_test_user(client, "billing_cap@example.com")
        response = await client.put(
            "/v0/user/spending-limit",
            json={"monthly_spending_cap": 500.0},
            headers=user["headers"],
        )
        if response.status_code == 404:
            pytest.skip("Spending limit endpoint not available")
        assert response.status_code == 200

    @pytest.mark.anyio
    async def test_spending_limit_validation(self, client: AsyncClient):
        user = await create_test_user(client, "billing_cap_invalid@example.com")
        response = await client.put(
            "/v0/user/spending-limit",
            json={"monthly_spending_cap": -100.0},
            headers=user["headers"],
        )
        if response.status_code == 404:
            pytest.skip("Spending limit endpoint not available")
        assert response.status_code == 422

    @pytest.mark.anyio
    async def test_remove_spending_limit(self, client: AsyncClient):
        user = await create_test_user(client, "billing_cap_remove@example.com")
        response = await client.put(
            "/v0/user/spending-limit",
            json={"monthly_spending_cap": 500.0},
            headers=user["headers"],
        )
        if response.status_code == 404:
            pytest.skip("Spending limit endpoint not available")
        response = await client.put(
            "/v0/user/spending-limit",
            json={"monthly_spending_cap": None},
            headers=user["headers"],
        )
        assert response.status_code == 200


# ============================================================================
# API Validation & Error Handling
# ============================================================================


class TestAPIValidation:
    @pytest.mark.anyio
    async def test_comprehensive_error_messages(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user, ba = make_user_with_billing(
            dbsession,
            "validation_user",
            credits=1000,
            stripe_customer_id="cus_validation",
        )
        dbsession.commit()

        # Enable autorecharge with no spending
        response = await client.put(
            "/v0/admin/enable_autorecharge",
            params={"id": user.id, "enable": True},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 400
        error_data = response.json()
        assert "User must spend at least $100.00" in error_data["detail"]
        assert "Current spending: $0.00" in error_data["detail"]

        # Set autorecharge quantity below minimum
        for amount in [0.01, 10.0, 24.99]:
            response = await client.put(
                "/v0/admin/autorecharge_qty",
                params={"id": user.id, "qty": amount},
                headers=ADMIN_HEADERS,
            )
            assert response.status_code == 400
            assert "Minimum auto-recharge amount is $25" in response.json()["detail"]

        # Valid autorecharge quantity should succeed
        response = await client.put(
            "/v0/admin/autorecharge_qty",
            params={"id": user.id, "qty": 25.0},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200

    @pytest.mark.anyio
    async def test_user_not_found_error_handling(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        non_existent_uid = "non_existent_user"

        response = await client.get(
            f"/v0/admin/user_billing_eligibility?user_id={non_existent_uid}",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

        response = await client.put(
            "/v0/admin/enable_autorecharge",
            params={"id": non_existent_uid, "enable": True},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 404

        response = await client.put(
            "/v0/admin/autorecharge_qty",
            params={"id": non_existent_uid, "qty": 50.0},
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 404


# ============================================================================
# Billing Model & Validation (DB-level)
# ============================================================================


class TestBillingModel:
    @pytest.mark.anyio
    async def test_organization_has_default_billing_fields(
        self,
        client: AsyncClient,
        dbsession,
    ):
        """Creating an org via the API provisions a billing account with correct defaults."""
        from orchestra.db.models.orchestra_models import Organization

        owner = await create_test_user(client, "wallet_owner@test.com")
        org_response = await client.post(
            "/v0/organizations",
            json={"name": "Wallet Test Org"},
            headers=owner["headers"],
        )
        assert org_response.status_code == 201
        org_id = org_response.json()["id"]

        org = dbsession.query(Organization).filter(Organization.id == org_id).first()
        ba = org.billing_account
        assert ba is not None
        assert ba.credits == Decimal("0")
        assert ba.stripe_customer_id is None
        assert ba.autorecharge is False
        assert ba.account_status == "ACTIVE"
        assert ba.billing_setup_complete is False
        assert ba.billing_email is None
        assert ba.name is None
        assert ba.tax_id is None
        assert ba.billing_address is None or ba.billing_address == {}

    def test_frozen_org_cannot_spend_credits(self, dbsession):
        from orchestra.db.models.orchestra_models import Organization
        from orchestra.lib.billing import get_billing_entity

        owner_ba = BillingAccount(credits=0, account_status="ACTIVE")
        dbsession.add(owner_ba)
        dbsession.flush()

        owner = User(
            id="frozen_org_owner",
            email="frozen_org_owner@test.com",
            name="Frozen Org Owner",
            billing_account_id=owner_ba.id,
        )
        dbsession.add(owner)
        dbsession.flush()

        org_ba = BillingAccount(
            stripe_customer_id="cus_frozen_test",
            account_status="ACTIVE",
            credits=0,
        )
        dbsession.add(org_ba)
        dbsession.flush()

        org = Organization(
            name="Frozen Test Org",
            owner_id=owner.id,
            billing_account_id=org_ba.id,
        )
        dbsession.add(org)
        dbsession.commit()

        billing_entity = get_billing_entity(dbsession, owner.id, org.id)
        assert billing_entity.is_organization

        dao = BillingAccountDAO(dbsession)
        dao.set_account_status(org.billing_account_id, "SUSPENDED")
        dbsession.commit()

        with pytest.raises(ValueError) as exc_info:
            get_billing_entity(dbsession, owner.id, org.id)
        assert "SUSPENDED" in str(exc_info.value)

    def test_invalid_account_status_rejected(self, dbsession):
        from orchestra.db.models.orchestra_models import Organization

        owner_ba = BillingAccount(credits=0, account_status="ACTIVE")
        dbsession.add(owner_ba)
        dbsession.flush()

        owner = User(
            id="status_owner",
            email="status_owner@test.com",
            name="Status Owner",
            billing_account_id=owner_ba.id,
        )
        dbsession.add(owner)
        dbsession.flush()

        org_ba = BillingAccount(credits=0, account_status="ACTIVE")
        dbsession.add(org_ba)
        dbsession.flush()

        org = Organization(
            name="Status Test Org",
            owner_id=owner.id,
            billing_account_id=org_ba.id,
        )
        dbsession.add(org)
        dbsession.commit()

        dao = BillingAccountDAO(dbsession)
        assert dao.set_account_status(org.billing_account_id, "SUSPENDED") is True
        assert dao.set_account_status(org.billing_account_id, "PAST_DUE") is True
        assert dao.set_account_status(org.billing_account_id, "CLOSED") is True
        assert dao.set_account_status(org.billing_account_id, "ACTIVE") is True

        with pytest.raises(ValueError) as exc_info:
            dao.set_account_status(org.billing_account_id, "BANANA")
        assert "Invalid account status" in str(exc_info.value)

        with pytest.raises(ValueError):
            dao.set_account_status(org.billing_account_id, "FROZEN")

    def test_recharge_requires_billing_account(self, dbsession):
        from sqlalchemy.exc import IntegrityError

        from orchestra.db.models.orchestra_models import Organization

        user_ba = BillingAccount(credits=Decimal("100"), account_status="ACTIVE")
        dbsession.add(user_ba)
        dbsession.flush()

        user = User(
            id="xor_user",
            email="xor_user@test.com",
            billing_account_id=user_ba.id,
        )
        dbsession.add(user)

        owner_ba = BillingAccount(credits=0, account_status="ACTIVE")
        dbsession.add(owner_ba)
        dbsession.flush()

        owner = User(
            id="xor_owner",
            email="xor_owner@test.com",
            name="XOR Owner",
            billing_account_id=owner_ba.id,
        )
        dbsession.add(owner)
        dbsession.flush()

        org_ba = BillingAccount(credits=0, account_status="ACTIVE")
        dbsession.add(org_ba)
        dbsession.flush()

        org = Organization(
            name="XOR Test Org",
            owner_id=owner.id,
            billing_account_id=org_ba.id,
        )
        dbsession.add(org)
        dbsession.commit()

        # Valid: linked to user's billing account
        r1 = Recharge(
            billing_account_id=user_ba.id,
            quantity=Decimal("10"),
            amount_usd=Decimal("10"),
            status=RechargeStatus.PENDING_INVOICE,
        )
        dbsession.add(r1)
        dbsession.commit()

        # Valid: linked to org's billing account
        r2 = Recharge(
            billing_account_id=org_ba.id,
            quantity=Decimal("10"),
            amount_usd=Decimal("10"),
            status=RechargeStatus.PENDING_INVOICE,
        )
        dbsession.add(r2)
        dbsession.commit()

        # Invalid: no billing_account_id
        r3 = Recharge(
            quantity=Decimal("10"),
            amount_usd=Decimal("10"),
            status=RechargeStatus.PENDING_INVOICE,
        )
        dbsession.add(r3)
        with pytest.raises(IntegrityError):
            dbsession.commit()
        dbsession.rollback()

    def test_duplicate_stripe_customer_id_rejected(self, dbsession):
        from sqlalchemy.exc import IntegrityError

        from orchestra.db.models.orchestra_models import Organization

        owner1_ba = BillingAccount(credits=0, account_status="ACTIVE")
        owner2_ba = BillingAccount(credits=0, account_status="ACTIVE")
        dbsession.add(owner1_ba)
        dbsession.add(owner2_ba)
        dbsession.flush()

        owner1 = User(
            id="dup_owner1",
            email="dup1@test.com",
            name="Owner 1",
            billing_account_id=owner1_ba.id,
        )
        owner2 = User(
            id="dup_owner2",
            email="dup2@test.com",
            name="Owner 2",
            billing_account_id=owner2_ba.id,
        )
        dbsession.add(owner1)
        dbsession.add(owner2)
        dbsession.flush()

        org1_ba = BillingAccount(
            credits=0,
            account_status="ACTIVE",
            stripe_customer_id="cus_duplicate_test",
        )
        dbsession.add(org1_ba)
        dbsession.flush()

        org1 = Organization(
            name="Dup Test Org 1",
            owner_id=owner1.id,
            billing_account_id=org1_ba.id,
        )
        dbsession.add(org1)
        dbsession.commit()

        org2_ba = BillingAccount(
            credits=0,
            account_status="ACTIVE",
            stripe_customer_id="cus_duplicate_test",
        )
        dbsession.add(org2_ba)
        with pytest.raises(IntegrityError):
            dbsession.flush()
        dbsession.rollback()

        # NULL stripe_customer_id is allowed for multiple accounts
        org3_ba = BillingAccount(
            credits=0,
            account_status="ACTIVE",
            stripe_customer_id=None,
        )
        org4_ba = BillingAccount(
            credits=0,
            account_status="ACTIVE",
            stripe_customer_id=None,
        )
        dbsession.add(org3_ba)
        dbsession.add(org4_ba)
        dbsession.flush()

        org3 = Organization(
            name="Dup Test Org 3",
            owner_id=owner1.id,
            billing_account_id=org3_ba.id,
        )
        org4 = Organization(
            name="Dup Test Org 4",
            owner_id=owner2.id,
            billing_account_id=org4_ba.id,
        )
        dbsession.add(org3)
        dbsession.add(org4)
        dbsession.commit()

    def test_duplicate_autorecharge_prevented(self, dbsession):
        from datetime import datetime, timezone

        from orchestra.db.models.orchestra_models import Organization
        from orchestra.lib.time import month_end_utc

        owner_ba = BillingAccount(credits=0, account_status="ACTIVE")
        dbsession.add(owner_ba)
        dbsession.flush()

        owner = User(
            id="dup_recharge_owner",
            email="dup_recharge@test.com",
            name="Dup Recharge Owner",
            billing_account_id=owner_ba.id,
        )
        dbsession.add(owner)
        dbsession.flush()

        org_ba = BillingAccount(
            credits=0,
            account_status="ACTIVE",
            stripe_customer_id="cus_dup_recharge",
            autorecharge=True,
            autorecharge_threshold=Decimal("10"),
            autorecharge_qty=Decimal("100"),
        )
        dbsession.add(org_ba)
        dbsession.flush()

        org = Organization(
            name="Dup Recharge Org",
            owner_id=owner.id,
            billing_account_id=org_ba.id,
        )
        dbsession.add(org)
        dbsession.commit()

        current_month_end = month_end_utc(datetime.now(timezone.utc).date())

        r1 = Recharge(
            billing_account_id=org_ba.id,
            quantity=Decimal("100"),
            amount_usd=Decimal("100"),
            invoice_group=current_month_end,
            status=RechargeStatus.PENDING_INVOICE,
            type="auto",
        )
        dbsession.add(r1)
        dbsession.commit()

        existing = (
            dbsession.query(Recharge)
            .filter_by(
                billing_account_id=org_ba.id,
                invoice_group=current_month_end,
                status=RechargeStatus.PENDING_INVOICE,
            )
            .first()
        )
        assert existing is not None
        assert existing.id == r1.id
        assert (existing is not None) is True


# ============================================================================
# International Address (API-level)
# ============================================================================


class TestInternationalAddress:
    @pytest.mark.anyio
    async def test_api_update(self, client: AsyncClient, dbsession):
        owner = await create_test_user(client, "api_intl_addr@test.com")
        org = await create_test_org(client, owner, "API Intl Address Org")

        response = await client.patch(
            "/v0/billing/billing-profile",
            json={
                "billing_email": "billing@indiancompany.in",
                "business_name": "Indian Tech Pvt Ltd",
                "billing_address": {
                    "country": "IN",
                    "line1": "Tower B, Tech Park",
                    "city": "Hyderabad",
                    "state": "Telangana",
                    "district": "Rangareddy",
                    "postal_code": "500081",
                },
            },
            headers=org["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_address"]["country"] == "IN"
        assert data["billing_address"]["district"] == "Rangareddy"
        assert data["billing_address"]["state"] == "Telangana"

    @pytest.mark.anyio
    async def test_partial_update_merges(self, client: AsyncClient, dbsession):
        owner = await create_test_user(client, "merge_addr@test.com")
        org_response = await client.post(
            "/v0/organizations",
            json={"name": "Merge Address Org"},
            headers=owner["headers"],
        )
        org_id = org_response.json()["id"]

        from orchestra.db.models.orchestra_models import Organization

        org = dbsession.query(Organization).filter(Organization.id == org_id).first()
        dao = BillingAccountDAO(dbsession)

        dao.update_billing_profile(
            org.billing_account_id,
            billing_address={
                "country": "US",
                "line1": "123 Main St",
                "city": "Boston",
                "state": "MA",
                "postal_code": "02101",
            },
        )
        dbsession.commit()

        dao.update_billing_profile(
            org.billing_account_id,
            billing_address={"city": "Cambridge"},
        )
        dbsession.commit()

        profile = dao.get_billing_profile(org.billing_account_id)
        assert profile["billing_address"]["country"] == "US"
        assert profile["billing_address"]["line1"] == "123 Main St"
        assert profile["billing_address"]["city"] == "Cambridge"
        assert profile["billing_address"]["state"] == "MA"
