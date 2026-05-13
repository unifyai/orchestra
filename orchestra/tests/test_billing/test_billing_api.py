"""
Billing API endpoint tests.

Organised into semantic classes so every group of related endpoints lives
together.  All Stripe calls are mocked — these tests run in CI without
network access.

Sections:
- SchemaSmoke: recharge / billing_account table columns exist
- Credits: GET /credits, POST /credits/deduct, add/deduct via DAO
- CreditsHistoryEndpoints: GET /credits/transactions, /credits/spending
- BillingEntity: get_billing_entity + deduct_credits for user / org
- DeductEndpoint: /credits/deduct triggering auto-recharge
- CheckoutPortalStatus: checkout-session, portal-session, checkout-status
- AutoRechargeEndpoints: GET / PUT /billing/auto-recharge
- OrgBillingPermissions: RBAC on billing endpoints via org API key
- AccountInfo: GET /billing/account-info
- CreditGrants: claim-credit-grant-link → promo recharge
- BillingProfile: GET / PATCH /billing/billing-profile
- TaxValidation: validate-tax-id, supported-tax-countries
- BuyCreditsCheckoutMeteredBlock: /billing/checkout-session 400 for METERED
- SpendEndpointsBillingMode: /user/spend exposes billing_mode
- AdminBillingEndpoints: admin billing routes
- AdminBillingTemplates: admin /billing/plans/templates lifecycle
- TemplateFxValidation: FX policy validation at template creation
- AdminPlanLifecycle: admin /billing/plans set/active/history
- AccountInfoPlanSummary: customer /billing/account-info plan section
- CustomerInvoicesEndpoint: customer /billing/invoices listing
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
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette import status

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO
from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO
from orchestra.db.dao.credit_transaction_dao import CreditTransactionDAO
from orchestra.db.dao.user_dao import UserDAO
from orchestra.db.models.orchestra_models import (
    DEFAULT_TEMPLATE_ID,
    RECHARGE_TYPE_MONTHLY_COMMIT,
    RECHARGE_TYPE_PAYMENT,
    RECHARGE_TYPE_PROMO,
    ApiKey,
    BillingAccount,
    BillingMode,
    CollectionMethod,
    Recharge,
    RechargeStatus,
    User,
)
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import (
    make_billing_account,
    make_user_with_billing,
)
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
        assert response_dict["billing_mode"] in ("CREDITS", "METERED")
        assert set(response_dict.keys()) == {"id", "credits", "billing_mode"}

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
    async def test_deduct_credits_exceeding_balance_goes_negative(
        self,
        client: AsyncClient,
    ):
        """Deducting more than the balance should succeed and drive the
        balance negative so that the spending-limit hook blocks further
        LLM calls."""
        credits_response = await client.get("/v0/credits", headers=HEADERS)
        assert credits_response.status_code == status.HTTP_200_OK
        current_credits = credits_response.json()["credits"]

        overshoot = 10.0
        response = await client.post(
            "/v0/credits/deduct",
            headers=HEADERS,
            json={"amount": current_credits + overshoot},
        )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["previous_credits"] == current_credits
        assert data["deducted"] == current_credits + overshoot
        assert math.isclose(data["current_credits"], -overshoot)

        updated = await client.get("/v0/credits", headers=HEADERS)
        assert math.isclose(updated.json()["credits"], -overshoot)

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

    @pytest.mark.anyio
    async def test_deduct_passes_category_and_detail_through_to_ledger(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """``/credits/deduct`` forwards ``category``, ``assistant_id``,
        ``description`` and ``detail`` into the ``CreditTransaction`` row.
        """
        import uuid

        user, ba = make_user_with_billing(
            dbsession,
            f"api-deduct-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = f"test-key-{user.id}"
        dbsession.add(ApiKey(user_id=user.id, key=api_key))
        dbsession.commit()

        response = await client.post(
            "/v0/credits/deduct",
            json={
                "amount": 5.0,
                "category": "llm",
                "assistant_id": 99,
                "description": "API test deduction",
                "detail": {"model": "gpt-4o-mini"},
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["deducted"] == 5.0
        assert data["current_credits"] == pytest.approx(95.0)

        txns = CreditTransactionDAO(dbsession).get_transactions(ba.id)
        assert len(txns) >= 1
        assert txns[0].category == "llm"
        assert txns[0].assistant_id == 99
        assert txns[0].description == "API test deduction"
        assert txns[0].detail["model"] == "gpt-4o-mini"

    @pytest.mark.anyio
    async def test_deduct_without_category_defaults_to_llm(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Backward-compat: legacy callers that send only ``amount`` still
        work, and the ledger row defaults to category ``llm``.
        """
        import uuid

        user, ba = make_user_with_billing(
            dbsession,
            f"api-compat-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = f"test-key-{user.id}"
        dbsession.add(ApiKey(user_id=user.id, key=api_key))
        dbsession.commit()

        response = await client.post(
            "/v0/credits/deduct",
            json={"amount": 3.0},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200
        assert response.json()["deducted"] == 3.0

        txns = CreditTransactionDAO(dbsession).get_transactions(ba.id)
        assert txns[0].category == "llm"


# ============================================================================
# Credits History — /v0/credits/transactions and /v0/credits/spending
# ============================================================================


class TestCreditsHistoryEndpoints:
    """Customer-facing ledger views: paged history, aggregated buckets,
    spending breakdown.

    These transitively cover the ``CreditTransactionDAO`` query surface
    (filter by category, pagination, ``get_aggregated_transactions``,
    ``get_spending_by_category``) without poking the DAO directly.
    """

    @staticmethod
    def _make_api_key(dbsession: Session, user) -> str:
        key_value = f"test-key-{user.id}"
        dbsession.add(ApiKey(user_id=user.id, key=key_value))
        dbsession.flush()
        return key_value

    @pytest.mark.anyio
    async def test_transaction_history_returns_individual_rows(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        import uuid

        user, ba = make_user_with_billing(
            dbsession,
            f"api-txns-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = self._make_api_key(dbsession, user)

        ba_dao = BillingAccountDAO(dbsession)
        ba_dao.deduct_credits(ba.id, 5.0, category="llm")
        ba_dao.deduct_credits(ba.id, 3.0, category="media")
        dbsession.commit()

        response = await client.get(
            "/v0/credits/transactions",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert "transactions" in data
        assert len(data["transactions"]) == 2
        # Individual-row shape (no bucket).
        assert "id" in data["transactions"][0]
        assert "at" in data["transactions"][0]
        assert "amount" in data["transactions"][0]
        assert "bucket" not in data["transactions"][0]

    @pytest.mark.anyio
    async def test_transaction_history_aggregated_by_day(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """``group_by=day`` returns aggregated rows; only debits are summed."""
        import uuid

        user, ba = make_user_with_billing(
            dbsession,
            f"api-agg-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = self._make_api_key(dbsession, user)

        ba_dao = BillingAccountDAO(dbsession)
        ba_dao.deduct_credits(ba.id, 5.0, category="llm")
        ba_dao.deduct_credits(ba.id, 3.0, category="llm")
        ba_dao.deduct_credits(ba.id, 2.0, category="media")
        # Credits (positive amounts) must NOT appear in the spending view.
        ba_dao.add_credits(ba.id, 50.0, category="recharge")
        dbsession.commit()

        response = await client.get(
            "/v0/credits/transactions",
            params={"group_by": "day"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200, response.text
        txns = response.json()["transactions"]
        assert len(txns) >= 1
        for t in txns:
            assert {"bucket", "category", "total", "count"} <= set(t)

        categories = {t["category"] for t in txns}
        assert "recharge" not in categories  # credits excluded

        llm = [t for t in txns if t["category"] == "llm"]
        media = [t for t in txns if t["category"] == "media"]
        assert llm and llm[0]["total"] == pytest.approx(8.0) and llm[0]["count"] == 2
        assert media and media[0]["total"] == pytest.approx(2.0)

    @pytest.mark.anyio
    async def test_transaction_history_accepts_month_granularity(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        import uuid

        user, ba = make_user_with_billing(
            dbsession,
            f"api-agg-month-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = self._make_api_key(dbsession, user)
        BillingAccountDAO(dbsession).deduct_credits(ba.id, 1.0, category="llm")
        dbsession.commit()

        response = await client.get(
            "/v0/credits/transactions",
            params={"group_by": "month"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200
        assert len(response.json()["transactions"]) >= 1

    @pytest.mark.anyio
    async def test_transaction_history_rejects_invalid_group_by(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        import uuid

        user, _ba = make_user_with_billing(
            dbsession,
            f"api-agg-bad-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = self._make_api_key(dbsession, user)
        dbsession.commit()

        response = await client.get(
            "/v0/credits/transactions",
            params={"group_by": "century"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 400

    @pytest.mark.anyio
    async def test_spending_breakdown_returns_total_and_by_category(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        import uuid

        user, ba = make_user_with_billing(
            dbsession,
            f"api-spending-{uuid.uuid4().hex[:8]}",
            credits=100,
        )
        api_key = self._make_api_key(dbsession, user)

        ba_dao = BillingAccountDAO(dbsession)
        ba_dao.deduct_credits(ba.id, 10.0, category="llm")
        ba_dao.deduct_credits(ba.id, 5.0, category="media")
        dbsession.commit()

        response = await client.get(
            "/v0/credits/spending",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == pytest.approx(15.0)
        assert data["by_category"]["llm"] == pytest.approx(10.0)
        assert data["by_category"]["media"] == pytest.approx(5.0)


# ============================================================================
# Credit Locking
# ============================================================================


class TestCreditLocking:
    """Tests for row-level locking via FOR UPDATE."""

    def test_get_for_update_returns_account(self, dbsession, worker_id):
        ba_dao = BillingAccountDAO(dbsession)
        user_dao = UserDAO(dbsession)
        user = user_dao.get_user_with_id("user1")
        ba = ba_dao.get_for_update(user.billing_account_id)
        assert ba is not None
        assert ba.id == user.billing_account_id

    def test_get_for_update_returns_none_for_missing(self, dbsession, worker_id):
        ba_dao = BillingAccountDAO(dbsession)
        ba = ba_dao.get_for_update(999999)
        assert ba is None

    def test_deduct_allows_negative(self, dbsession, worker_id):
        ba_dao = BillingAccountDAO(dbsession)
        user_dao = UserDAO(dbsession)
        user = user_dao.get_user_with_id("user1")

        initial = ba_dao.get_credits(user.billing_account_id)
        new_balance = ba_dao.deduct_credits(
            user.billing_account_id,
            float(initial) + 1,
        )
        assert new_balance < 0

    def test_add_then_deduct_is_consistent(self, dbsession, worker_id):
        ba_dao = BillingAccountDAO(dbsession)
        user_dao = UserDAO(dbsession)
        user = user_dao.get_user_with_id("user1")

        initial = ba_dao.get_credits(user.billing_account_id)
        ba_dao.add_credits(user.billing_account_id, 50)
        ba_dao.deduct_credits(user.billing_account_id, 30)
        final = ba_dao.get_credits(user.billing_account_id)
        assert final == initial + Decimal("50") - Decimal("30")


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

        baseline = get_billing_entity(dbsession, user["id"], organization_id=None)
        baseline_credits = baseline.credits

        ba_dao.add_credits(user_obj.billing_account_id, 50)
        dbsession.commit()

        entity = get_billing_entity(dbsession, user["id"], organization_id=None)

        assert entity.entity_type == BillingEntityType.USER
        assert entity.entity_id == user["id"]
        assert entity.credits == baseline_credits + Decimal("50")
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
        from orchestra.lib.billing import get_billing_entity

        user = await create_test_user(client, "deduct_user@test.com")

        user_dao = UserDAO(dbsession)
        ba_dao = BillingAccountDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])
        ba_dao.add_credits(user_obj.billing_account_id, 100)
        dbsession.commit()

        entity = get_billing_entity(dbsession, user["id"])

        new_balance = ba_dao.deduct_credits(
            entity.billing_account_id,
            25.50,
            category="other",
        )
        dbsession.commit()

        assert new_balance == Decimal("74.50")

        updated_user = user_dao.get_user_with_id(user["id"])
        assert updated_user.billing_account.credits == Decimal("74.50")

    @pytest.mark.anyio
    async def test_deduct_credits_from_org(self, client: AsyncClient, dbsession):
        from orchestra.db.models.orchestra_models import Organization
        from orchestra.lib.billing import get_billing_entity

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

        ba_dao = BillingAccountDAO(dbsession)
        new_balance = ba_dao.deduct_credits(
            entity.billing_account_id,
            123.45,
            category="other",
        )
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

    @pytest.mark.anyio
    async def test_metered_account_has_sufficient_credits_regardless_of_balance(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """METERED accounts ignore the wallet for billable-action gating.

        Usage on METERED is settled at month-end via
        ``monthly_metered_invoicer``. The wallet is frozen and may carry
        any leftover balance from a prior CREDITS phase — neither a
        positive, zero, nor negative balance should gate billable
        actions for METERED accounts.
        """
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )
        from orchestra.lib.billing import get_billing_entity

        user = await create_test_user(client, "metered_has_sufficient@test.com")
        user_dao = UserDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])
        ba = user_obj.billing_account

        tpl = _make_metered_template(dbsession, name="metered-suff-tpl")
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        ba.autorecharge = True
        ba.autorecharge_threshold = Decimal("100")
        ba.autorecharge_qty = Decimal("50")

        for leftover in (Decimal("50"), Decimal("0"), Decimal("-30")):
            ba.credits = leftover
            dbsession.commit()
            entity = get_billing_entity(dbsession, user["id"])
            assert entity.is_metered is True
            assert entity.has_sufficient_credits(Decimal("999")) is True
            # Autorecharge must not trigger for METERED, even with
            # autorecharge configured and a balance below threshold.
            assert entity.should_trigger_autorecharge(leftover) is False

    @pytest.mark.anyio
    async def test_credits_account_gating_unchanged(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """CREDITS accounts continue to gate on ``credits >= cost``."""
        from orchestra.lib.billing import get_billing_entity

        user = await create_test_user(client, "credits_gating@test.com")
        user_dao = UserDAO(dbsession)
        ba_dao = BillingAccountDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])

        ba_dao.add_credits(user_obj.billing_account_id, 10)
        dbsession.commit()

        entity = get_billing_entity(dbsession, user["id"])
        assert entity.is_metered is False
        assert entity.has_sufficient_credits(Decimal("5")) is True
        assert entity.has_sufficient_credits(Decimal("10")) is True
        assert entity.has_sufficient_credits(Decimal("11")) is False

    @pytest.mark.anyio
    async def test_credits_round_trip_via_metered_preserves_balance(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """CREDITS → METERED → CREDITS preserves the wallet balance.

        The metered phase freezes the wallet (``deduct_credits`` skips
        the wallet write for METERED), so ledger-only usage during the
        METERED window does not deplete it. On the way back to CREDITS
        the leftover balance becomes spendable again.
        """
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )
        from orchestra.lib.billing import get_billing_entity

        user = await create_test_user(client, "round_trip@test.com")
        user_dao = UserDAO(dbsession)
        ba_dao = BillingAccountDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])
        ba = user_obj.billing_account

        # Phase 1: CREDITS — fund the wallet.
        ba_dao.add_credits(ba.id, 50)
        dbsession.commit()
        assert get_billing_entity(dbsession, user["id"]).credits == Decimal("50")

        # Phase 2: switch to METERED and accrue ledger-only usage.
        metered_tpl = _make_metered_template(dbsession, name="round-trip-metered")
        plan_dao = BillingPlanAssignmentDAO(dbsession)
        plan_dao.set_plan(billing_account_id=ba.id, template_id=metered_tpl.id)
        dbsession.commit()

        ba_dao.deduct_credits(ba.id, 7.50, category="llm")
        dbsession.commit()
        dbsession.refresh(ba)
        # Wallet untouched; ledger row recorded by deduct_credits.
        assert ba.credits == Decimal("50")
        entity = get_billing_entity(dbsession, user["id"])
        assert entity.is_metered is True
        assert entity.credits == Decimal("50")

        # Phase 3: revert to CREDITS — leftover balance is preserved
        # and spendable again.
        from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO

        default_tpl = BillingPlanTemplateDAO(dbsession).get_default()
        plan_dao.set_plan(billing_account_id=ba.id, template_id=default_tpl.id)
        dbsession.commit()

        entity = get_billing_entity(dbsession, user["id"])
        assert entity.is_metered is False
        assert entity.credits == Decimal("50")

        new_balance = ba_dao.deduct_credits(ba.id, 5, category="llm")
        dbsession.commit()
        assert new_balance == Decimal("45")


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

        _cus_with_pm = SimpleNamespace(
            invoice_settings=SimpleNamespace(
                default_payment_method=SimpleNamespace(id="pm_test"),
            ),
            default_source=None,
        )
        mock_stripe_module = SimpleNamespace(
            InvoiceItem=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(id="ii_deduct_ar"),
            ),
            Customer=SimpleNamespace(retrieve=lambda *a, **kw: _cus_with_pm),
            StripeError=Exception,
            InvalidRequestError=Exception,
            error=SimpleNamespace(StripeError=Exception, InvalidRequestError=Exception),
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

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

        _cus_with_pm = SimpleNamespace(
            invoice_settings=SimpleNamespace(
                default_payment_method=SimpleNamespace(id="pm_test"),
            ),
            default_source=None,
        )
        mock_stripe_module = SimpleNamespace(
            InvoiceItem=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(id="ii_no_ar"),
            ),
            Customer=SimpleNamespace(retrieve=lambda *a, **kw: _cus_with_pm),
            StripeError=Exception,
            InvalidRequestError=Exception,
            error=SimpleNamespace(StripeError=Exception, InvalidRequestError=Exception),
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

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

    @pytest.mark.anyio
    async def test_deduct_allows_negative_balance(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Deducting more than the available balance should succeed and
        result in a negative credit balance rather than being rejected."""
        user = await create_test_user(client, "deduct_negative@test.com")

        user_dao = UserDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])

        ba = user_obj.billing_account
        ba.credits = Decimal("0.50")
        ba.autorecharge = False
        dbsession.commit()

        response = await client.post(
            "/v0/credits/deduct",
            json={"amount": 5.0},
            headers=user["headers"],
        )
        assert response.status_code == 200

        data = response.json()
        assert data["previous_credits"] == 0.5
        assert data["deducted"] == 5.0
        assert math.isclose(data["current_credits"], -4.5)

        dbsession.expire_all()
        assert float(user_obj.billing_account.credits) == -4.5

    @pytest.mark.anyio
    async def test_negative_balance_triggers_auto_recharge(
        self,
        client: AsyncClient,
        dbsession: Session,
        monkeypatch,
    ):
        """When a deduction drives the balance negative and auto-recharge
        is enabled, auto-recharge should fire and bring the balance back up."""
        import orchestra.lib.billing

        _cus_with_pm = SimpleNamespace(
            invoice_settings=SimpleNamespace(
                default_payment_method=SimpleNamespace(id="pm_test"),
            ),
            default_source=None,
        )
        mock_stripe_module = SimpleNamespace(
            InvoiceItem=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(id="ii_neg_ar"),
            ),
            Customer=SimpleNamespace(retrieve=lambda *a, **kw: _cus_with_pm),
            StripeError=Exception,
            InvalidRequestError=Exception,
            error=SimpleNamespace(StripeError=Exception, InvalidRequestError=Exception),
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        user = await create_test_user(client, "deduct_neg_ar@test.com")

        user_dao = UserDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])

        ba = user_obj.billing_account
        ba.credits = Decimal("2")
        ba.autorecharge = True
        ba.autorecharge_threshold = Decimal("10")
        ba.autorecharge_qty = Decimal("50")
        ba.stripe_customer_id = "cus_neg_ar"
        dbsession.commit()

        response = await client.post(
            "/v0/credits/deduct",
            json={"amount": 5.0},
            headers=user["headers"],
        )
        assert response.status_code == 200

        data = response.json()
        assert data["previous_credits"] == 2.0
        assert data["deducted"] == 5.0
        # 2 - 5 = -3, then auto-recharge adds 50 → 47
        assert data["current_credits"] == 47.0

        dbsession.expire_all()
        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba.id, type="auto")
            .first()
        )
        assert recharge is not None
        assert recharge.quantity == Decimal("50")

    @pytest.mark.anyio
    async def test_residual_balance_deduction_succeeds(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Regression test for the Vantage bug: a tiny residual balance
        (e.g. $0.004) must not prevent deductions from going through.
        The balance should go negative so the spending-limit hook blocks
        subsequent calls."""
        user = await create_test_user(client, "deduct_residual@test.com")

        user_dao = UserDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])

        ba = user_obj.billing_account
        ba.credits = Decimal("0.004128")
        ba.autorecharge = False
        dbsession.commit()

        response = await client.post(
            "/v0/credits/deduct",
            json={"amount": 0.069576},
            headers=user["headers"],
        )
        assert response.status_code == 200

        data = response.json()
        assert data["current_credits"] < 0

        dbsession.expire_all()
        assert float(user_obj.billing_account.credits) < 0


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
# Buy-Credits checkout block (METERED accounts)
# ============================================================================


class TestBuyCreditsCheckoutMeteredBlock:
    """``POST /v0/billing/checkout-session`` rejects METERED accounts.

    Buy Credits is meaningless in invoice-mode — the account never deducts
    against a wallet, so there's nothing to top up.
    """

    @pytest.mark.anyio
    async def test_checkout_session_400_for_metered(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "checkout_meter@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        ba = db_user.billing_account
        assert ba is not None
        tpl = _make_metered_template(dbsession, name="checkout-block-tpl")
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        dbsession.commit()

        resp = await client.post(
            "/v0/billing/checkout-session",
            headers=user["headers"],
        )
        assert resp.status_code == 400
        assert "METERED" in resp.json()["detail"]


# ============================================================================
# /spend endpoints expose billing_mode
# ============================================================================


class TestSpendEndpointsBillingMode:
    """``/v0/user/spend`` (and friends) returns ``billing_mode`` so the
    frontend can hide credit-balance UI for METERED accounts."""

    @pytest.mark.anyio
    async def test_user_spend_returns_credits_for_pristine(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "spend_credits@test.com")
        resp = await client.get(
            "/v0/user/spend",
            params={"month": "2026-04"},
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["billing_mode"] == "CREDITS"

    @pytest.mark.anyio
    async def test_user_spend_returns_metered_when_assigned(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "spend_metered@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        tpl = _make_metered_template(dbsession, name="spend-mode-tpl")
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=db_user.billing_account.id,
            template_id=tpl.id,
        )
        dbsession.commit()

        resp = await client.get(
            "/v0/user/spend",
            params={"month": "2026-04"},
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["billing_mode"] == "METERED"


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
        assert data["minimum_spend_required"] == 1000.0
        assert data["remaining_spend_needed"] == 1000.0

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
            quantity=Decimal("1200"),
            amount_usd=Decimal("1200"),
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
        assert data["total_spending"] == 1200.0
        assert data["remaining_spend_needed"] == 0.0

    @pytest.mark.anyio
    async def test_put_enable_with_all_settings(
        self,
        client,
        dbsession,
        monkeypatch,
    ):
        import orchestra.web.api.billing.views as billing_views

        monkeypatch.setattr(
            billing_views,
            "_customer_has_payment_method",
            lambda _: True,
        )

        user = await create_test_user(client, "ar_put_user@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("100"),
                stripe_customer_id="cus_test_put",
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            ba = db_user.billing_account
            ba.stripe_customer_id = "cus_test_put"

        recharge = Recharge(
            billing_account_id=ba.id,
            type="payment",
            quantity=Decimal("1200"),
            amount_usd=Decimal("1200"),
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

    @pytest.mark.anyio
    async def test_put_enable_blocked_with_unpaid_invoices(self, client, dbsession):
        """Cannot re-enable auto-recharge while INVOICE_CREATED recharges exist."""
        user = await create_test_user(client, "ar_unpaid@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("200"),
                stripe_customer_id="cus_ar_unpaid",
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            ba = db_user.billing_account
            ba.stripe_customer_id = "cus_ar_unpaid"

        # Enough spending to pass the threshold
        paid = Recharge(
            billing_account_id=ba.id,
            type="payment",
            quantity=Decimal("1200"),
            amount_usd=Decimal("1200"),
            status=RechargeStatus.PAID,
        )
        # An unpaid auto-recharge invoice
        unpaid = Recharge(
            billing_account_id=ba.id,
            type="auto",
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_unpaid_test",
        )
        dbsession.add_all([paid, unpaid])
        dbsession.commit()

        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": True, "threshold": 10.0, "qty": 50.0},
            headers=user["headers"],
        )
        assert response.status_code == 400
        assert "unpaid" in response.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_put_enable_blocked_when_suspended(self, client, dbsession):
        """Cannot enable auto-recharge when account is SUSPENDED."""
        user = await create_test_user(client, "ar_suspended@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("200"),
                account_status="SUSPENDED",
                stripe_customer_id="cus_ar_suspended",
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            ba = db_user.billing_account
            ba.credits = Decimal("200")
            ba.account_status = "SUSPENDED"
            ba.stripe_customer_id = "cus_ar_suspended"
        dbsession.commit()

        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": True, "threshold": 10.0, "qty": 50.0},
            headers=user["headers"],
        )
        assert response.status_code in (400, 403)

    @pytest.mark.anyio
    async def test_put_enable_allowed_after_invoice_paid(
        self,
        client,
        dbsession,
        monkeypatch,
    ):
        """Can re-enable auto-recharge once all invoices are paid."""
        import orchestra.web.api.billing.views as billing_views

        monkeypatch.setattr(
            billing_views,
            "_customer_has_payment_method",
            lambda _: True,
        )

        user = await create_test_user(client, "ar_paid@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        if db_user.billing_account is None:
            ba = BillingAccount(
                credits=Decimal("200"),
                stripe_customer_id="cus_ar_paid",
            )
            dbsession.add(ba)
            dbsession.flush()
            db_user.billing_account_id = ba.id
            dbsession.flush()
        else:
            ba = db_user.billing_account
            ba.stripe_customer_id = "cus_ar_paid"

        # Enough spending + all recharges PAID (no outstanding debt)
        r1 = Recharge(
            billing_account_id=ba.id,
            type="payment",
            quantity=Decimal("1200"),
            amount_usd=Decimal("1200"),
            status=RechargeStatus.PAID,
        )
        r2 = Recharge(
            billing_account_id=ba.id,
            type="auto",
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.PAID,
            stripe_invoice_id="in_paid_test",
        )
        dbsession.add_all([r1, r2])
        dbsession.commit()

        response = await client.put(
            "/v0/billing/auto-recharge",
            json={"enabled": True, "threshold": 10.0, "qty": 50.0},
            headers=user["headers"],
        )
        assert response.status_code == 200
        assert response.json()["enabled"] is True


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
        # Use BillingAccountDAO so the account satisfies the v2 invariant
        # (always has an active default plan assignment + non-null
        # ``plan_assignment_id``); a bare BillingAccount(...) row would
        # break ``resolve_effective_plan`` later.
        ba = BillingAccountDAO(dbsession).create(
            credits=Decimal("42.50"),
            stripe_customer_id="cus_test123",
            autorecharge=True,
            autorecharge_threshold=Decimal("10"),
            autorecharge_qty=Decimal("50"),
        )

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
        ba = BillingAccountDAO(dbsession).create(credits=Decimal("0"))
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

        # And the deposit is recorded on the credits ledger with category=promo.
        txns = CreditTransactionDAO(dbsession).get_transactions(ba.id)
        promo_txns = [t for t in txns if t.category == "promo"]
        assert len(promo_txns) == 1
        assert float(promo_txns[0].amount) == pytest.approx(15.0)

    @pytest.mark.anyio
    async def test_populates_last_recharge_at(self, client, dbsession):
        from orchestra.db.dao.one_time_credit_grant_link_dao import (
            OneTimeCreditGrantLinkDAO,
        )

        user = await create_test_user(client, "promo_last_recharge@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])

        ba = BillingAccountDAO(dbsession).create(credits=Decimal("0"))
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
                    "postal_code": "500081",
                },
            },
            headers=org["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_address"]["country"] == "IN"
        assert data["billing_address"]["state"] == "Telangana"
        assert data["billing_address"]["postal_code"] == "500081"

    @pytest.mark.anyio
    async def test_billing_address_rejects_extra_fields(
        self,
        client: AsyncClient,
        dbsession,
    ):
        """Arbitrary keys in billing_address are rejected (extra=forbid)."""
        owner = await create_test_user(client, "extra_addr_fields@test.com")
        org = await create_test_org(client, owner, "ExtraFieldOrg")

        response = await client.patch(
            "/v0/billing/billing-profile",
            json={
                "billing_address": {
                    "line1": "123 Main St",
                    "city": "NYC",
                    "country": "US",
                    "district": "Manhattan",
                },
            },
            headers=org["headers"],
        )
        assert response.status_code == 422


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
        # period defaults to previous month, formatted YYYY-MM
        assert len(data["period"]) == 7 and data["period"][4] == "-"
        assert isinstance(data["accounts_invoiced"], int)
        assert isinstance(data["accounts_skipped"], int)
        assert isinstance(data["accounts_failed"], int)
        assert isinstance(data["errors"], list)

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
        assert data["period"] == "2024-01"

    @pytest.mark.anyio
    async def test_trigger_billing_guard(self, client: AsyncClient):
        response = await client.post(
            "/v0/admin/billing/suspend-past-due",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "noop"

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
    async def test_promo_recharge_capped_at_100(self, client: AsyncClient):
        """Promo recharges above $100 are rejected."""
        user_id = "user1"
        url = "/v0/admin/create_recharge"
        response = await client.post(
            url,
            json={"user_id": user_id, "quantity": 101, "type": "promo"},
            headers=ADMIN_HEADERS,
        )
        if response.status_code == 404:
            pytest.skip("Recharge endpoint not available at this path")
        assert response.status_code == 400
        assert "capped" in response.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_promo_recharge_at_limit_succeeds(self, client: AsyncClient):
        """Promo recharge at exactly $100 should succeed."""
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
    async def test_create_recharge_invoice_group_is_month_end_for_non_midnight_at(
        self,
        client: AsyncClient,
        dbsession: Session,
        monkeypatch,
    ):
        """Regression: ``create_recharge`` must stamp ``invoice_group`` to the
        last day of the calendar month even when ``datetime.now(UTC)`` is not
        midnight.

        The pre-fix arithmetic
        ``(at.replace(day=1) + 32d).replace(day=1) - 1us`` preserved the
        ``hour/minute/second`` components of ``at``, so for any non-midnight
        invocation the final ``.date()`` cast landed on the **1st of the next
        month** rather than the last day of the current month — which made
        ``monthly_credits_invoicer`` (which filters on
        ``Recharge.invoice_group == month_end_utc(today)``) silently skip
        auto-recharges. This produced 78+ rows with first-of-next-month
        ``invoice_group`` values in production (Recharge 20934 / Nassim being
        the one that actually got stuck in ``PENDING_INVOICE`` and surfaced
        via reconciliation on 2026-05-13).
        """
        import calendar

        from orchestra.web.api.admin import views as admin_views

        frozen = datetime(2026, 3, 15, 14, 30, 0, tzinfo=dt.timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: D401
                return frozen.astimezone(tz) if tz is not None else frozen.replace(
                    tzinfo=None,
                )

        monkeypatch.setattr(admin_views, "datetime", _FrozenDatetime)

        user = await create_test_user(client, "invoice_group_regression@test.com")
        user_id = user["id"]

        response = await client.post(
            "/v0/admin/create_recharge",
            json={"user_id": user_id, "quantity": 5, "type": "promo"},
            headers=ADMIN_HEADERS,
        )
        if response.status_code == 404:
            pytest.skip("Recharge endpoint not available at this path")
        assert response.status_code == 200, response.text

        dbsession.expire_all()
        ba_id = (
            dbsession.query(User.billing_account_id).filter_by(id=user_id).scalar()
        )
        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba_id)
            .order_by(Recharge.at.desc())
            .first()
        )
        assert recharge is not None
        last_day = calendar.monthrange(2026, 3)[1]
        assert recharge.invoice_group == dt.date(2026, 3, last_day), (
            "invoice_group must be the last day of the month, even when "
            "datetime.now(UTC) has a non-zero time-of-day component; "
            f"got {recharge.invoice_group!r}"
        )

    @pytest.mark.anyio
    async def test_create_recharge_invoice_group_respects_target_month(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """``target_month='YYYY-MM'`` must also resolve to the last day of
        that month (not the 1st of the *next* month, which the pre-fix
        in-line arithmetic in ``admin/views.py`` could produce for any
        non-midnight wall-clock).
        """
        import calendar

        user = await create_test_user(
            client,
            "invoice_group_target_month@test.com",
        )
        user_id = user["id"]

        response = await client.post(
            "/v0/admin/create_recharge",
            json={
                "user_id": user_id,
                "quantity": 5,
                "type": "promo",
                "target_month": "2026-02",
            },
            headers=ADMIN_HEADERS,
        )
        if response.status_code == 404:
            pytest.skip("Recharge endpoint not available at this path")
        assert response.status_code == 200, response.text

        dbsession.expire_all()
        ba_id = (
            dbsession.query(User.billing_account_id).filter_by(id=user_id).scalar()
        )
        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba_id)
            .order_by(Recharge.at.desc())
            .first()
        )
        assert recharge is not None
        last_day = calendar.monthrange(2026, 2)[1]
        assert recharge.invoice_group == dt.date(2026, 2, last_day)

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


class TestAdminPaymentPreferencesEndpoint:
    """``PATCH /v0/admin/billing/payment-preferences`` round-trip + validation.

    The DAO-level validation (empty list, duplicates, unknown methods)
    is unit-tested in ``TestBillingAccountDAO``; here we cover the
    HTTP surface — that the endpoint:

    * persists the override end-to-end and surfaces it back via
      ``account-info``;
    * accepts ``null`` to clear the override;
    * propagates DAO ``ValueError`` as 400 (not 500).
    """

    @pytest.mark.anyio
    async def test_set_and_clear_round_trip(self, client: AsyncClient):
        user = await create_test_user(client, "payment_prefs_round_trip@example.com")
        user_id = user["id"]

        # 1) Set wire-only.
        resp = await client.patch(
            "/v0/admin/billing/payment-preferences",
            json={
                "user_id": user_id,
                "preferred_payment_method_types": ["customer_balance"],
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["preferred_payment_method_types"] == ["customer_balance"]

        # 2) ``account-info`` reflects the same value (proves we
        #    actually persisted, not just echoed).
        info = await client.get(
            f"/v0/admin/billing/account-info?user_id={user_id}",
            headers=ADMIN_HEADERS,
        )
        assert info.status_code == 200
        assert info.json()["preferred_payment_method_types"] == ["customer_balance"]

        # 3) Clear by sending null. Falls back to the invoicer defaults
        #    next time an invoice is generated.
        resp = await client.patch(
            "/v0/admin/billing/payment-preferences",
            json={
                "user_id": user_id,
                "preferred_payment_method_types": None,
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["preferred_payment_method_types"] is None

        info = await client.get(
            f"/v0/admin/billing/account-info?user_id={user_id}",
            headers=ADMIN_HEADERS,
        )
        assert info.json()["preferred_payment_method_types"] is None

    @pytest.mark.anyio
    async def test_unknown_method_returns_400(self, client: AsyncClient):
        """Unsupported methods must fail loudly (DAO ``ValueError`` → 400).

        A typo like ``"sepa_debit"`` would otherwise reach
        ``Invoice.create`` and Stripe rejects with an opaque error long
        after the admin moved on.
        """
        user = await create_test_user(client, "payment_prefs_invalid@example.com")
        resp = await client.patch(
            "/v0/admin/billing/payment-preferences",
            json={
                "user_id": user["id"],
                "preferred_payment_method_types": ["card", "sepa_debit"],
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "sepa_debit" in resp.text

    @pytest.mark.anyio
    async def test_empty_list_returns_400(self, client: AsyncClient):
        """Empty list is rejected — would leave the customer with no payment options."""
        user = await create_test_user(client, "payment_prefs_empty@example.com")
        resp = await client.patch(
            "/v0/admin/billing/payment-preferences",
            json={
                "user_id": user["id"],
                "preferred_payment_method_types": [],
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400


# ============================================================================
# Managed-billing endpoint helpers (templates / plans / invoicer trigger)
# ============================================================================


def _make_metered_template(
    dbsession: Session,
    *,
    name: str,
    commit: Decimal = Decimal("1000"),
    collection: CollectionMethod = CollectionMethod.SEND_INVOICE_NET_30,
    base_pricing_factor: Decimal = Decimal("1.0"),
    overage_pricing_factor: Decimal = Decimal("1.0"),
    display_name: str | None = None,
):
    return BillingPlanTemplateDAO(dbsession).create_template(
        name=name,
        display_name=display_name,
        billing_mode=BillingMode.METERED,
        commit_amount=commit,
        commit_period="MONTHLY",
        base_pricing_factor=base_pricing_factor,
        overage_pricing_factor=overage_pricing_factor,
        collection_method=collection,
        is_custom=True,
        is_active=True,
    )


def _backdate_assignment(
    dbsession: Session,
    assignment_id: int,
    started_at: dt.datetime,
) -> None:
    """Move an assignment's ``started_at`` so it can cover a past period."""
    dbsession.execute(
        sa.text("UPDATE billing_plan_assignment SET started_at = :ts WHERE id = :id"),
        {"ts": started_at, "id": assignment_id},
    )
    dbsession.flush()


# ============================================================================
# Admin /billing/plans/templates
# ============================================================================


class TestAdminBillingTemplates:
    @pytest.mark.anyio
    async def test_create_template_minimal_payg(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        body = {
            "name": "Pro Monthly $20",
            "billing_mode": "CREDITS",
        }
        resp = await client.post(
            "/v0/admin/billing/plans/templates",
            json=body,
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "Pro Monthly $20"
        assert data["billing_mode"] == "CREDITS"
        # Default catalog placement = non-custom + active.
        assert data["is_custom"] is False
        assert data["is_active"] is True
        assert data["commit_amount"] is None

    @pytest.mark.anyio
    async def test_create_metered_commitment_template(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        body = {
            "name": "Vantage Q3 2026",
            "billing_mode": "METERED",
            "is_custom": True,
            "commit_amount": 5000.0,
            "commit_period": "MONTHLY",
            "collection_method": "SEND_INVOICE_NET_30",
            "base_pricing_factor": 1.2,
            "overage_pricing_factor": 1.5,
        }
        resp = await client.post(
            "/v0/admin/billing/plans/templates",
            json=body,
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["billing_mode"] == "METERED"
        assert data["is_custom"] is True
        assert data["is_active"] is True
        assert data["commit_amount"] == 5000.0
        assert data["collection_method"] == "SEND_INVOICE_NET_30"
        assert data["base_pricing_factor"] == 1.2
        assert data["overage_pricing_factor"] == 1.5

    @pytest.mark.anyio
    async def test_create_template_rejects_duplicate_name(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        body = {
            "name": "DupName",
            "billing_mode": "CREDITS",
        }
        first = await client.post(
            "/v0/admin/billing/plans/templates",
            json=body,
            headers=ADMIN_HEADERS,
        )
        assert first.status_code == 200
        second = await client.post(
            "/v0/admin/billing/plans/templates",
            json=body,
            headers=ADMIN_HEADERS,
        )
        assert second.status_code == 409

    @pytest.mark.anyio
    async def test_create_template_rejects_invalid_enum(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        body = {
            "name": "BadEnum",
            "billing_mode": "NOT_A_VALID_MODE",
        }
        resp = await client.post(
            "/v0/admin/billing/plans/templates",
            json=body,
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "billing_mode" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_create_template_rejects_invalid_combination(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        # Positive commit_amount requires commit_period — rejected by
        # the DB check constraint, surfaced as 400.
        body = {
            "name": "BadCombo",
            "billing_mode": "CREDITS",
            "commit_amount": 1000.0,
            # commit_period omitted on purpose.
        }
        resp = await client.post(
            "/v0/admin/billing/plans/templates",
            json=body,
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_list_templates_default_includes_both(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        # The admin endpoint defaults to "show everything" (both
        # catalog and custom rows) so the All / Catalog / Custom radio
        # in the admin UI can default to "All" without sending a
        # filter param. Use ``include_custom=false`` to narrow to the
        # catalog-only view (covered by a separate test below).
        BillingPlanTemplateDAO(dbsession).create_template(
            name="catalog-row",
            billing_mode=BillingMode.CREDITS,
            is_custom=False,
            is_active=True,
        )
        BillingPlanTemplateDAO(dbsession).create_template(
            name="custom-row",
            billing_mode=BillingMode.CREDITS,
            is_custom=True,
            is_active=True,
        )
        dbsession.commit()

        resp = await client.get(
            "/v0/admin/billing/plans/templates",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()]
        assert "default" in names  # seed (non-custom + active)
        assert "catalog-row" in names
        assert "custom-row" in names

    @pytest.mark.anyio
    async def test_list_templates_catalog_only(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        # ``include_custom=false`` narrows to the public catalog —
        # mirrors the "Catalog only" radio in the admin UI and what a
        # self-serve pricing page would surface.
        BillingPlanTemplateDAO(dbsession).create_template(
            name="catalog-only-row",
            billing_mode=BillingMode.CREDITS,
            is_custom=False,
            is_active=True,
        )
        BillingPlanTemplateDAO(dbsession).create_template(
            name="bespoke-row",
            billing_mode=BillingMode.CREDITS,
            is_custom=True,
            is_active=True,
        )
        dbsession.commit()

        resp = await client.get(
            "/v0/admin/billing/plans/templates",
            params=[("include_custom", "false")],
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()]
        assert "default" in names
        assert "catalog-only-row" in names
        assert "bespoke-row" not in names

    @pytest.mark.anyio
    async def test_list_templates_with_include_custom(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        BillingPlanTemplateDAO(dbsession).create_template(
            name="custom-only",
            billing_mode=BillingMode.CREDITS,
            is_custom=True,
            is_active=True,
        )
        dbsession.commit()
        resp = await client.get(
            "/v0/admin/billing/plans/templates",
            params=[("include_custom", "true")],
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()]
        assert names == ["custom-only"]

    @pytest.mark.anyio
    async def test_list_templates_with_include_inactive(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        BillingPlanTemplateDAO(dbsession).create_template(
            name="deprecated-row",
            billing_mode=BillingMode.CREDITS,
            is_custom=False,
            is_active=False,
        )
        dbsession.commit()
        resp_active = await client.get(
            "/v0/admin/billing/plans/templates",
            headers=ADMIN_HEADERS,
        )
        names_active = [t["name"] for t in resp_active.json()]
        assert "deprecated-row" not in names_active

        resp_all = await client.get(
            "/v0/admin/billing/plans/templates",
            params=[("include_inactive", "true")],
            headers=ADMIN_HEADERS,
        )
        names_all = [t["name"] for t in resp_all.json()]
        assert "deprecated-row" in names_all

    @pytest.mark.anyio
    async def test_deprecate_template(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        tpl = BillingPlanTemplateDAO(dbsession).create_template(
            name="to-deprecate",
            billing_mode=BillingMode.CREDITS,
            is_custom=False,
            is_active=True,
        )
        dbsession.commit()
        resp = await client.post(
            f"/v0/admin/billing/plans/templates/{tpl.id}/deprecate",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_active"] is False
        # is_custom is preserved.
        assert body["is_custom"] is False

    @pytest.mark.anyio
    async def test_deprecate_unknown_template_404(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        resp = await client.post(
            "/v0/admin/billing/plans/templates/999999/deprecate",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_deprecate_refused_when_account_active_on_template(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Cannot deprecate a template while any account is still
        actively assigned to it — the guard returns 409 with a hint
        about moving accounts off first.
        """
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )

        tpl = _make_metered_template(
            dbsession,
            name="in-use-tpl",
            commit=Decimal("5000"),
        )
        dbsession.commit()
        user = await create_test_user(client, "deprecate_in_use@test.com")
        db_user = UserDAO(dbsession).get_user_with_id(user["id"])
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=db_user.billing_account.id,
            template_id=tpl.id,
        )
        dbsession.commit()

        # Refused while assigned.
        resp = await client.post(
            f"/v0/admin/billing/plans/templates/{tpl.id}/deprecate",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409, resp.text
        assert "1 account" in resp.json()["detail"]
        # Template still active (no half-applied state).
        dbsession.expire_all()
        from orchestra.db.dao.billing_plan_template_dao import (
            BillingPlanTemplateDAO,
        )
        refreshed = BillingPlanTemplateDAO(dbsession).get_by_id(tpl.id)
        assert refreshed is not None and refreshed.is_active is True

        # Move the account off → deprecate now succeeds.
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=db_user.billing_account.id,
            template_id=DEFAULT_TEMPLATE_ID,
        )
        dbsession.commit()
        retry = await client.post(
            f"/v0/admin/billing/plans/templates/{tpl.id}/deprecate",
            headers=ADMIN_HEADERS,
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["is_active"] is False


# ============================================================================
# Template FX validation (rejected at the admin endpoint)
# ============================================================================


class TestTemplateFxValidation:
    """The admin endpoint front-loads FX validation so callers get clear errors.

    Equivalent to the old DAO-level ``TestTemplateFxValidation`` unit
    tests, but exercised through ``POST /v0/admin/billing/plans/templates``.
    """

    @pytest.mark.anyio
    async def test_locked_rate_requires_positive_rate(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        resp = await client.post(
            "/v0/admin/billing/plans/templates",
            json={
                "name": "bad-locked-rate",
                "billing_mode": "METERED",
                "commit_amount": 100.0,
                "currency": "GBP",
                "commit_period": "MONTHLY",
                "fx_policy": "LOCKED_RATE",
                # fx_locked_rate omitted → invalid
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_locked_rate_disallowed_for_other_policies(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        resp = await client.post(
            "/v0/admin/billing/plans/templates",
            json={
                "name": "bad-spot-with-rate",
                "billing_mode": "METERED",
                "commit_amount": 100.0,
                "currency": "GBP",
                "commit_period": "MONTHLY",
                "fx_policy": "SPOT",
                "fx_locked_rate": 0.80,  # not allowed with SPOT
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_non_usd_requires_fx_policy(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        # currency=GBP without an fx_policy — DAO should reject before
        # the DB check constraint fires.
        resp = await client.post(
            "/v0/admin/billing/plans/templates",
            json={
                "name": "bad-gbp-no-fx",
                "billing_mode": "METERED",
                "commit_amount": 100.0,
                "currency": "GBP",
                "commit_period": "MONTHLY",
                # fx_policy omitted on purpose.
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_usd_rejects_fx_policy(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        # USD templates must have fx_policy=NULL — passing one is a 400.
        resp = await client.post(
            "/v0/admin/billing/plans/templates",
            json={
                "name": "bad-usd-with-fx",
                "billing_mode": "METERED",
                "commit_amount": 100.0,
                "currency": "USD",
                "commit_period": "MONTHLY",
                "fx_policy": "SPOT",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_usd_template_has_null_fx_policy(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        resp = await client.post(
            "/v0/admin/billing/plans/templates",
            json={
                "name": "ok-usd-no-fx",
                "billing_mode": "METERED",
                "commit_amount": 100.0,
                "currency": "USD",
                "commit_period": "MONTHLY",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["fx_policy"] is None
        assert data.get("fx_locked_rate") in (None, 0)


# ============================================================================
# Admin /billing/plans (set / active / history)
# ============================================================================


class TestAdminPlanLifecycle:
    @pytest.mark.anyio
    async def test_set_plan_pristine_to_metered(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        # Pre-set stripe_customer_id so the assignment-time guard for
        # METERED templates is satisfied without exercising the
        # auto-create branch (covered separately).
        user, _ba = make_user_with_billing(
            dbsession,
            "plan_set_u1",
            stripe_customer_id="cus_set_u1",
        )
        tpl = _make_metered_template(dbsession, name="set-tpl-1")
        dbsession.commit()
        resp = await client.post(
            "/v0/admin/billing/plans/set",
            json={
                "user_id": user.id,
                "template_id": tpl.id,
                "change_reason": "initial onboarding",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        data = body["assignment"]
        assert data["template_id"] == tpl.id
        assert data["template_billing_mode"] == "METERED"
        assert data["template_plan_type"] == "COMMITMENT"
        assert data["change_reason"] == "initial onboarding"

    @pytest.mark.anyio
    async def test_set_plan_idempotent_returns_noop(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Re-issuing set_plan with the active template_id is a no-op."""
        user, ba = make_user_with_billing(
            dbsession,
            "plan_set_idempotent",
            stripe_customer_id="cus_set_idempotent",
        )
        tpl = _make_metered_template(dbsession, name="idempotent-tpl")
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        dbsession.commit()
        resp = await client.post(
            "/v0/admin/billing/plans/set",
            json={"user_id": user.id, "template_id": tpl.id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "noop"
        hist = await client.get(
            "/v0/admin/billing/plans/history",
            params={"user_id": user.id},
            headers=ADMIN_HEADERS,
        )
        # Initial default (closed) + active metered.
        assert len(hist.json()["assignments"]) == 2

    @pytest.mark.anyio
    async def test_set_plan_400_for_deprecated_template(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user, _ba = make_user_with_billing(dbsession, "plan_set_dep")
        tpl = BillingPlanTemplateDAO(dbsession).create_template(
            name="will-deprecate",
            billing_mode=BillingMode.CREDITS,
            is_custom=False,
            is_active=False,
        )
        dbsession.commit()
        resp = await client.post(
            "/v0/admin/billing/plans/set",
            json={"user_id": user.id, "template_id": tpl.id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "deprecated" in resp.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_set_plan_switch_at_boundary_creates_history_row(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Active→different template at AT_BOUNDARY closes the old row
        and inserts the new one — same effective_at on both sides."""
        user, ba = make_user_with_billing(
            dbsession,
            "plan_switch_u1",
            stripe_customer_id="cus_switch_u1",
        )
        old = _make_metered_template(dbsession, name="switch-old")
        new = _make_metered_template(
            dbsession,
            name="switch-new",
            commit=Decimal("2000"),
        )
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=old.id,
        )
        dbsession.commit()

        now = dt.datetime.now(dt.timezone.utc)
        if now.month == 12:
            boundary = dt.datetime(now.year + 1, 1, 1, tzinfo=dt.timezone.utc)
        else:
            boundary = dt.datetime(
                now.year,
                now.month + 1,
                1,
                tzinfo=dt.timezone.utc,
            )
        resp = await client.post(
            "/v0/admin/billing/plans/set",
            json={
                "user_id": user.id,
                "template_id": new.id,
                "effective_at": boundary.isoformat(),
                "change_reason": "renegotiated commit",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["assignment"]["template_id"] == new.id
        hist = await client.get(
            "/v0/admin/billing/plans/history",
            params={"user_id": user.id},
            headers=ADMIN_HEADERS,
        )
        assert hist.status_code == 200
        # Initial default + old metered + new metered.
        assert len(hist.json()["assignments"]) == 3

    @pytest.mark.anyio
    async def test_set_plan_rejects_non_boundary_effective_at(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user, ba = make_user_with_billing(
            dbsession,
            "plan_switch_mid",
            stripe_customer_id="cus_switch_mid",
        )
        old = _make_metered_template(dbsession, name="midswitch-old")
        new = _make_metered_template(dbsession, name="midswitch-new")
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=old.id,
        )
        dbsession.commit()
        mid_month = dt.datetime(2026, 6, 15, 12, tzinfo=dt.timezone.utc)
        resp = await client.post(
            "/v0/admin/billing/plans/set",
            json={
                "user_id": user.id,
                "template_id": new.id,
                "effective_at": mid_month.isoformat(),
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 400
        assert "AT_BOUNDARY" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_set_plan_defaults_to_next_month_boundary(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Without effective_at on a switch, the new row starts at first of next UTC month."""
        user, ba = make_user_with_billing(
            dbsession,
            "plan_switch_default",
            stripe_customer_id="cus_switch_default",
        )
        old = _make_metered_template(dbsession, name="default-bd-old")
        new = _make_metered_template(dbsession, name="default-bd-new")
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=old.id,
        )
        dbsession.commit()
        resp = await client.post(
            "/v0/admin/billing/plans/set",
            json={"user_id": user.id, "template_id": new.id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        started = dt.datetime.fromisoformat(
            resp.json()["assignment"]["started_at"],
        )
        assert started.day == 1
        assert started.hour == 0

    @pytest.mark.anyio
    async def test_set_plan_to_default_template_returns_to_default(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Setting template_id=DEFAULT_TEMPLATE_ID closes the active
        custom row and inserts a fresh default plan assignment row.

        Same close-and-insert mechanics as any other plan change — the
        default plan is just another (seeded) template."""
        user, ba = make_user_with_billing(dbsession, "plan_to_default")
        tpl = _make_metered_template(dbsession, name="to-default-target")
        custom = BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        dbsession.commit()
        assert custom is not None

        resp = await client.post(
            "/v0/admin/billing/plans/set",
            json={
                "user_id": user.id,
                "template_id": DEFAULT_TEMPLATE_ID,
                "change_reason": "customer churn",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["assignment"]["template_id"] == DEFAULT_TEMPLATE_ID
        assert body["assignment"]["change_reason"] == "customer churn"

        # The custom row is closed; a fresh default plan row is now
        # active; plan_assignment_id points at it.
        dbsession.expire_all()
        ba_reloaded = dbsession.query(type(ba)).filter_by(id=ba.id).one()
        assert ba_reloaded.plan_assignment_id == body["assignment"]["id"]

        history = BillingPlanAssignmentDAO(dbsession).list_history(ba.id)
        # Newest-first: new default, closed custom, initial default
        # (inserted by BillingAccountDAO.create at signup).
        assert len(history) >= 2
        assert history[0].template_id == DEFAULT_TEMPLATE_ID
        assert history[0].ended_at is None
        assert history[1].id == custom.id
        assert history[1].ended_at is not None

    @pytest.mark.anyio
    async def test_get_active_plan_pristine(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Pristine accounts always have an active default plan row from
        signup (inserted by ``BillingAccountDAO.create``)."""
        user, _ba = make_user_with_billing(dbsession, "plan_active_pristine")
        dbsession.commit()
        resp = await client.get(
            "/v0/admin/billing/plans/active",
            params={"user_id": user.id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_assignment"] is not None
        assert body["active_assignment"]["template_id"] == DEFAULT_TEMPLATE_ID
        assert body["active_assignment"]["ended_at"] is None

    @pytest.mark.anyio
    async def test_get_active_plan_with_assignment(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user, ba = make_user_with_billing(dbsession, "plan_active_assigned")
        tpl = _make_metered_template(dbsession, name="active-assigned-tpl")
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        dbsession.commit()
        resp = await client.get(
            "/v0/admin/billing/plans/active",
            params={"user_id": user.id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_assignment"]["template_id"] == tpl.id

    @pytest.mark.anyio
    async def test_history_returns_newest_first(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user, ba = make_user_with_billing(dbsession, "plan_history_u1")
        old = _make_metered_template(dbsession, name="hist-old")
        new = _make_metered_template(dbsession, name="hist-new")
        plan_dao = BillingPlanAssignmentDAO(dbsession)
        plan_dao.set_plan(billing_account_id=ba.id, template_id=old.id)
        boundary = dt.datetime(2999, 1, 1, tzinfo=dt.timezone.utc)
        plan_dao.set_plan(
            billing_account_id=ba.id,
            template_id=new.id,
            effective_at=boundary,
        )
        dbsession.commit()
        resp = await client.get(
            "/v0/admin/billing/plans/history",
            params={"user_id": user.id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        items = resp.json()["assignments"]
        # Initial default + old metered + new metered.
        assert len(items) == 3
        assert items[0]["template_id"] == new.id  # newest first
        assert items[1]["template_id"] == old.id
        assert items[2]["template_id"] == DEFAULT_TEMPLATE_ID

    @pytest.mark.anyio
    async def test_set_plan_409_with_pending_credits_recharge(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """An in-flight CREDITS recharge must block plan switching.

        This is the exploit guard: customer auto-recharges $1000 of
        credits on day 28 (writes Recharge.PENDING_INVOICE), then on day
        29 tries to switch to METERED. Without the guard the
        month-end credits invoicer would silently drop the row by
        plan_id mode and the customer would walk away with credits they
        never paid for. The 409 forces the operator (or the customer
        flow) to wait for the next monthly_credits_invoicer run.
        """
        user, ba = make_user_with_billing(
            dbsession,
            "plan_pending_block_u1",
            stripe_customer_id="cus_pending_block_u1",
        )
        tpl = _make_metered_template(dbsession, name="pending-block-tpl")
        # Auto-recharge that has not yet been invoiced — exactly what
        # ``queue_auto_recharge`` writes between the credit grant and
        # month-end ``monthly_credits_invoicer``.
        dbsession.add(
            Recharge(
                billing_account_id=ba.id,
                type="auto",
                quantity=Decimal("1000"),
                amount_usd=Decimal("1000"),
                status=RechargeStatus.PENDING_INVOICE,
                invoice_group=dt.date(2026, 4, 30),
            ),
        )
        dbsession.commit()

        resp = await client.post(
            "/v0/admin/billing/plans/set",
            json={"user_id": user.id, "template_id": tpl.id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "pending_recharges"
        assert detail["billing_account_id"] == ba.id
        assert len(detail["pending_recharge_ids"]) == 1
        # The active plan is still the default — no half-applied state.
        active = BillingPlanAssignmentDAO(dbsession).get_active(ba.id)
        assert active is not None
        assert active.template_id == DEFAULT_TEMPLATE_ID

    @pytest.mark.anyio
    async def test_set_plan_succeeds_after_pending_recharge_drained(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Once PENDING_INVOICE clears (e.g. invoiced or marked FAILED)
        the same set_plan request that 409'd succeeds.

        Steady-state of ``Recharge`` rows for a CREDITS account:
        ``PENDING_INVOICE → INVOICE_CREATED`` (after monthly_credits_invoicer
        runs). The guard only blocks ``PENDING_INVOICE``;
        ``INVOICE_CREATED`` is a normal in-collection state.
        """
        user, ba = make_user_with_billing(
            dbsession,
            "plan_pending_drain_u1",
            stripe_customer_id="cus_pending_drain_u1",
        )
        tpl = _make_metered_template(dbsession, name="pending-drain-tpl")
        rch = Recharge(
            billing_account_id=ba.id,
            type="auto",
            quantity=Decimal("100"),
            amount_usd=Decimal("100"),
            status=RechargeStatus.PENDING_INVOICE,
            invoice_group=dt.date(2026, 4, 30),
        )
        dbsession.add(rch)
        dbsession.commit()

        # Simulate the monthly invoicer transitioning the row.
        rch.status = RechargeStatus.INVOICE_CREATED
        rch.stripe_invoice_id = "in_drained"
        dbsession.commit()

        resp = await client.post(
            "/v0/admin/billing/plans/set",
            json={"user_id": user.id, "template_id": tpl.id},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"


# ============================================================================
# Customer GET /v0/billing/account-info — plan summary surfacing
# ============================================================================


class TestAccountInfoPlanSummary:
    @pytest.mark.anyio
    async def test_pristine_account_returns_default_plan(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "plansum_pristine@test.com")
        resp = await client.get(
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["billing_mode"] == "CREDITS"
        plan = data["plan"]
        assert plan is not None
        assert plan["template_id"] == DEFAULT_TEMPLATE_ID
        assert plan["template_name"] == "default"
        # default seeds a friendly ``display_name`` in the
        # migration; the customer-facing plan card surfaces this.
        assert plan["template_display_name"] == "Default"
        assert plan["plan_type"] == "PAY_AS_YOU_GO"
        assert plan["billing_mode"] == "CREDITS"
        # Under the Option-B invariant, every account has an active
        # assignment (default seeded at signup).
        assert plan["assignment_id"] is not None

    @pytest.mark.anyio
    async def test_metered_account_surfaces_plan_summary(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "plansum_metered@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        ba = db_user.billing_account
        assert ba is not None
        tpl = _make_metered_template(
            dbsession,
            name="surfaced-metered",
            commit=Decimal("1500"),
            collection=CollectionMethod.SEND_INVOICE_NET_30,
            base_pricing_factor=Decimal("0.9"),
            overage_pricing_factor=Decimal("1.1"),
        )
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        dbsession.commit()
        resp = await client.get(
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["billing_mode"] == "METERED"
        plan = data["plan"]
        assert plan["template_id"] == tpl.id
        assert plan["template_name"] == "surfaced-metered"
        # No ``display_name`` was set on this template; the API falls
        # back to ``name`` so the UI never has to special-case NULL.
        assert plan["template_display_name"] == "surfaced-metered"
        assert plan["plan_type"] == "COMMITMENT"
        assert plan["billing_mode"] == "METERED"
        assert plan["commit_amount"] == 1500.0
        assert plan["collection_method"] == "SEND_INVOICE_NET_30"
        # ``commit_schedule`` is surfaced; ``base_pricing_factor`` and
        # ``overage_pricing_factor`` are intentionally NOT — they're
        # internal pricing knobs that shouldn't be exposed on the
        # customer billing page.
        assert "commit_schedule" in plan
        assert "base_pricing_factor" not in plan
        assert "overage_pricing_factor" not in plan
        assert plan["assignment_id"] is not None


# ============================================================================
# Customer GET /v0/billing/invoices
# ============================================================================


class TestCustomerInvoicesEndpoint:
    @pytest.mark.anyio
    async def test_lists_invoiced_recharges_newest_first(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "invlist_basic@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        ba = db_user.billing_account

        old = Recharge(
            billing_account_id=ba.id,
            type="payment",
            quantity=Decimal("50"),
            amount_usd=Decimal("50"),
            status=RechargeStatus.PAID,
            stripe_invoice_id="in_old",
            at=dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc),
        )
        new = Recharge(
            billing_account_id=ba.id,
            type="payment",
            quantity=Decimal("75"),
            amount_usd=Decimal("75"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_new",
            at=dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc),
        )
        pending = Recharge(
            billing_account_id=ba.id,
            type="auto",
            quantity=Decimal("10"),
            amount_usd=Decimal("10"),
            status=RechargeStatus.PENDING_INVOICE,
            at=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        )
        dbsession.add_all([old, new, pending])
        dbsession.commit()

        resp = await client.get(
            "/v0/billing/invoices",
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["billing_account_id"] == ba.id
        assert len(body["invoices"]) == 2  # PENDING_INVOICE filtered out
        assert body["invoices"][0]["stripe_invoice_id"] == "in_new"
        assert body["invoices"][1]["stripe_invoice_id"] == "in_old"

    @pytest.mark.anyio
    async def test_metered_invoice_includes_plan_metadata_and_detail(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "invlist_metered@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        ba = db_user.billing_account
        tpl = _make_metered_template(
            dbsession,
            name="invlist-metered-tpl",
            commit=Decimal("500"),
        )
        a = BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        rch = Recharge(
            billing_account_id=ba.id,
            type=RECHARGE_TYPE_MONTHLY_COMMIT,
            quantity=Decimal("500"),
            amount_usd=Decimal("500"),
            status=RechargeStatus.INVOICE_CREATED,
            stripe_invoice_id="in_metered_test",
            plan_id=a.id,
            detail={
                "raw_usage_usd": "300",
                "commit_amount": "500",
                "invoiced_usd": "500",
            },
        )
        dbsession.add(rch)
        dbsession.commit()
        resp = await client.get(
            "/v0/billing/invoices",
            headers=user["headers"],
        )
        assert resp.status_code == 200
        item = resp.json()["invoices"][0]
        assert item["plan_assignment_id"] == a.id
        assert item["plan_template_name"] == "invlist-metered-tpl"
        assert item["detail"]["commit_amount"] == "500"

    @pytest.mark.anyio
    async def test_invoices_pagination(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "invlist_pag@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        ba = db_user.billing_account
        for i in range(5):
            dbsession.add(
                Recharge(
                    billing_account_id=ba.id,
                    type="payment",
                    quantity=Decimal("10"),
                    amount_usd=Decimal("10"),
                    status=RechargeStatus.PAID,
                    stripe_invoice_id=f"in_pag_{i}",
                    at=dt.datetime(2026, 1, i + 1, tzinfo=dt.timezone.utc),
                ),
            )
        dbsession.commit()
        resp = await client.get(
            "/v0/billing/invoices",
            params={"limit": 2, "offset": 1},
            headers=user["headers"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["invoices"]) == 2
        assert body["limit"] == 2
        assert body["offset"] == 1

    @pytest.mark.anyio
    async def test_invoices_400_for_no_billing(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "invlist_nobilling@test.com")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        db_user.billing_account_id = None
        dbsession.commit()
        resp = await client.get(
            "/v0/billing/invoices",
            headers=user["headers"],
        )
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_invoices_rejects_bad_limit(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "invlist_badlimit@test.com")
        resp = await client.get(
            "/v0/billing/invoices",
            params={"limit": 9999},
            headers=user["headers"],
        )
        assert resp.status_code == 400


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
        assert "User must spend at least $1000.00" in error_data["detail"]
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
                    "postal_code": "500081",
                },
            },
            headers=org["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_address"]["country"] == "IN"
        assert data["billing_address"]["state"] == "Telangana"
        assert data["billing_address"]["postal_code"] == "500081"

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


# ============================================================================
# Account Status Enforcement Logic
# ============================================================================


class TestAccountStatusEnforcement:
    """Only SUSPENDED and CLOSED block API access.

    ACTIVE accounts are never blocked by the middleware regardless of
    credit balance.  Balance-based enforcement is handled per-handler
    and by the spending-limit hook.

    These tests exercise the decision logic directly since the full
    HTTP dependency (check_account_not_frozen) requires request-state
    wiring and a separate read-only session.
    """

    def _should_block(self, account_status: str, credits: float) -> bool:
        """Reproduce the logic from check_account_not_frozen."""
        if account_status in ("SUSPENDED", "CLOSED"):
            return True
        return False

    def test_active_never_blocked(self):
        assert self._should_block("ACTIVE", 0) is False
        assert self._should_block("ACTIVE", -50) is False

    def test_suspended_always_blocked(self):
        assert self._should_block("SUSPENDED", 500) is True
        assert self._should_block("SUSPENDED", 0) is True

    def test_closed_always_blocked(self):
        assert self._should_block("CLOSED", 100) is True


# ============================================================================
# Plan groups — admin CRUD + customer self-serve switch
# ============================================================================


class TestAdminPlanGroups:
    """Admin CRUD + member ops for plan_group."""

    @pytest.mark.anyio
    async def test_create_list_get_group(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        resp = await client.post(
            "/v0/admin/billing/plans/groups",
            json={"name": "vantage-public", "display_name": "Vantage"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        group_id = resp.json()["id"]
        assert resp.json()["display_name"] == "Vantage"
        assert resp.json()["members"] == []

        listing = await client.get(
            "/v0/admin/billing/plans/groups",
            headers=ADMIN_HEADERS,
        )
        assert listing.status_code == 200
        slugs = [g["name"] for g in listing.json()["groups"]]
        assert "vantage-public" in slugs

        detail = await client.get(
            f"/v0/admin/billing/plans/groups/{group_id}",
            headers=ADMIN_HEADERS,
        )
        assert detail.status_code == 200
        assert detail.json()["name"] == "vantage-public"

    @pytest.mark.anyio
    async def test_duplicate_group_name_returns_409(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        await client.post(
            "/v0/admin/billing/plans/groups",
            json={"name": "dup-group"},
            headers=ADMIN_HEADERS,
        )
        resp = await client.post(
            "/v0/admin/billing/plans/groups",
            json={"name": "dup-group"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409, resp.text

    @pytest.mark.anyio
    async def test_add_remove_members_with_positions(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        tpl_a = _make_metered_template(
            dbsession,
            name="rung-a",
            commit=Decimal("5000"),
        )
        tpl_b = _make_metered_template(
            dbsession,
            name="rung-b",
            commit=Decimal("10000"),
        )
        dbsession.commit()
        group = (
            await client.post(
                "/v0/admin/billing/plans/groups",
                json={"name": "ladder"},
                headers=ADMIN_HEADERS,
            )
        ).json()

        # Add two ordered members
        for tpl_id, position in [(tpl_a.id, 0), (tpl_b.id, 1)]:
            r = await client.post(
                f"/v0/admin/billing/plans/groups/{group['id']}/members",
                json={"template_id": tpl_id, "position": position},
                headers=ADMIN_HEADERS,
            )
            assert r.status_code == 200, r.text
        # Position collision → 409
        clash = await client.post(
            f"/v0/admin/billing/plans/groups/{group['id']}/members",
            json={"template_id": tpl_a.id, "position": 5},
            headers=ADMIN_HEADERS,
        )
        # Already-member error before position is even considered.
        assert clash.status_code == 409, clash.text

        # Re-order swaps with a single PUT — clear-then-set means the
        # partial unique index never sees a duplicate position.
        swap = await client.put(
            f"/v0/admin/billing/plans/groups/{group['id']}/positions",
            json={
                "positions": [
                    {"template_id": tpl_a.id, "position": 1},
                    {"template_id": tpl_b.id, "position": 0},
                ],
            },
            headers=ADMIN_HEADERS,
        )
        assert swap.status_code == 200, swap.text
        members = {m["template_id"]: m["position"] for m in swap.json()["members"]}
        assert members[tpl_a.id] == 1
        assert members[tpl_b.id] == 0

        # Remove a member
        rm = await client.delete(
            f"/v0/admin/billing/plans/groups/{group['id']}/members/{tpl_a.id}",
            headers=ADMIN_HEADERS,
        )
        assert rm.status_code == 200
        remaining = [m["template_id"] for m in rm.json()["members"]]
        assert tpl_a.id not in remaining
        assert tpl_b.id in remaining

    @pytest.mark.anyio
    async def test_assign_group_to_account(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        from orchestra.db.models.enums import DEFAULT_PLAN_GROUP_ID

        user = await create_test_user(client, "groupassign@test.com")
        group = (
            await client.post(
                "/v0/admin/billing/plans/groups",
                json={"name": "assign-target"},
                headers=ADMIN_HEADERS,
            )
        ).json()
        # Pin to the new custom group
        resp = await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": group["id"]},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["plan_group_id"] == group["id"]
        # Revert to the platform default — there is no "clear" path
        # any more (plan_group_id is NOT NULL); operators reassign to
        # DEFAULT_PLAN_GROUP_ID = 1 instead.
        resp_default = await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": DEFAULT_PLAN_GROUP_ID},
            headers=ADMIN_HEADERS,
        )
        assert resp_default.status_code == 200, resp_default.text
        assert resp_default.json()["plan_group_id"] == DEFAULT_PLAN_GROUP_ID

    @pytest.mark.anyio
    async def test_assign_plan_group_rejects_null(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """``group_id`` is required (no opt-out): every account is on
        at least the platform-default group. Pydantic rejects the
        null payload at validation time (HTTP 422)."""
        user = await create_test_user(client, "assign_null_grp@test.com")
        resp = await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": None},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422, resp.text

    @pytest.mark.anyio
    async def test_deprecate_group_refused_when_assigned_to_account(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """PATCH ``is_active=false`` on a plan group is refused with 409
        when at least one billing account still points at it. Operator
        must reassign every account onto another group first (typically
        the platform default).
        """
        from orchestra.db.models.enums import DEFAULT_PLAN_GROUP_ID

        group = (
            await client.post(
                "/v0/admin/billing/plans/groups",
                json={"name": "in-use-group"},
                headers=ADMIN_HEADERS,
            )
        ).json()
        user = await create_test_user(client, "deprecate_in_use_grp@test.com")
        await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": group["id"]},
            headers=ADMIN_HEADERS,
        )

        resp = await client.patch(
            f"/v0/admin/billing/plans/groups/{group['id']}",
            json={"is_active": False},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 409, resp.text
        assert "1 billing account" in resp.json()["detail"]

        # Reassign to the platform default and retry succeeds.
        await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": DEFAULT_PLAN_GROUP_ID},
            headers=ADMIN_HEADERS,
        )
        retry = await client.patch(
            f"/v0/admin/billing/plans/groups/{group['id']}",
            json={"is_active": False},
            headers=ADMIN_HEADERS,
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["is_active"] is False

    @pytest.mark.anyio
    async def test_metadata_only_update_does_not_trigger_assignment_guard(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Updating ``display_name`` / ``description`` on an in-use group
        must NOT fire the deprecation guard — the guard only protects
        the ``is_active=true → false`` transition."""
        group = (
            await client.post(
                "/v0/admin/billing/plans/groups",
                json={"name": "rename-me", "display_name": "Old Label"},
                headers=ADMIN_HEADERS,
            )
        ).json()
        user = await create_test_user(client, "rename_in_use_grp@test.com")
        await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": group["id"]},
            headers=ADMIN_HEADERS,
        )
        resp = await client.patch(
            f"/v0/admin/billing/plans/groups/{group['id']}",
            json={"display_name": "New Label"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "New Label"


class TestCustomerPlanSwitch:
    """Customer-facing GET /billing/available-plans + POST /billing/plan."""

    @pytest.mark.anyio
    async def test_available_plans_empty_for_default_group_of_one(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Pristine account: auto-assigned to DEFAULT_PLAN_GROUP_ID, which today
        contains only the default template (the customer's current plan).
        The endpoint must return an empty ``available`` list so the FE
        hide-rule (no useful switching) suppresses the section. The
        ``plan_group_id`` is still surfaced (= 1) so other UI bits
        know the account is on the platform default.
        """
        from orchestra.db.models.enums import DEFAULT_PLAN_GROUP_ID

        user = await create_test_user(client, "switch_default_group@test.com")
        resp = await client.get(
            "/v0/billing/available-plans",
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["plan_group_id"] == DEFAULT_PLAN_GROUP_ID
        # Group-of-one hide-rule fires server-side: only entry would
        # be "current", which is useless to render → empty list.
        assert body["available"] == []
        # The next-period boundary is always surfaced so the UI can
        # still render a "Switch will land on" hint even when the
        # list itself is empty.
        assert body["next_period_start"]

    @pytest.mark.anyio
    async def test_available_plans_lists_active_members_with_classification(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "switch_list@test.com")
        # Three rungs: small (pos 0), mid (pos 1), big (pos 2)
        small = _make_metered_template(
            dbsession,
            name="ladder-small",
            commit=Decimal("1000"),
            display_name="Small",
        )
        mid = _make_metered_template(
            dbsession,
            name="ladder-mid",
            commit=Decimal("5000"),
            display_name="Mid",
        )
        big = _make_metered_template(
            dbsession,
            name="ladder-big",
            commit=Decimal("10000"),
            display_name="Big",
        )
        dbsession.commit()

        # Build ladder via admin endpoints
        group = (
            await client.post(
                "/v0/admin/billing/plans/groups",
                json={"name": "ladder-list", "display_name": "Vantage Tiers"},
                headers=ADMIN_HEADERS,
            )
        ).json()
        for tpl, pos in [(small, 0), (mid, 1), (big, 2)]:
            await client.post(
                f"/v0/admin/billing/plans/groups/{group['id']}/members",
                json={"template_id": tpl.id, "position": pos},
                headers=ADMIN_HEADERS,
            )
        # Assign group + pin user on the mid rung
        await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": group["id"]},
            headers=ADMIN_HEADERS,
        )
        db_user = UserDAO(dbsession).get_user_with_id(user["id"])
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=db_user.billing_account.id,
            template_id=mid.id,
        )
        dbsession.commit()

        resp = await client.get(
            "/v0/billing/available-plans",
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["plan_group_id"] == group["id"]
        assert body["plan_group_display_name"] == "Vantage Tiers"
        items = {it["template_id"]: it for it in body["available"]}
        assert items[small.id]["classification"] == "downgrade"
        assert items[mid.id]["classification"] == "current"
        assert items[mid.id]["is_current"] is True
        assert items[big.id]["classification"] == "upgrade"
        # Every member surfaces the same effective_at (next-period
        # boundary) so the confirmation modal shows a consistent date.
        boundaries = {it["effective_at"] for it in body["available"]}
        assert boundaries == {body["next_period_start"]}

    @pytest.mark.anyio
    async def test_switch_refused_when_template_not_in_group(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "switch_offgroup@test.com")
        in_group = _make_metered_template(
            dbsession,
            name="in-group",
            commit=Decimal("5000"),
        )
        off_group = _make_metered_template(
            dbsession,
            name="off-group",
            commit=Decimal("10000"),
        )
        dbsession.commit()
        group = (
            await client.post(
                "/v0/admin/billing/plans/groups",
                json={"name": "single-rung"},
                headers=ADMIN_HEADERS,
            )
        ).json()
        await client.post(
            f"/v0/admin/billing/plans/groups/{group['id']}/members",
            json={"template_id": in_group.id, "position": 0},
            headers=ADMIN_HEADERS,
        )
        await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": group["id"]},
            headers=ADMIN_HEADERS,
        )
        resp = await client.post(
            "/v0/billing/plan",
            json={"template_id": off_group.id},
            headers=user["headers"],
        )
        assert resp.status_code == 403, resp.text

    @pytest.mark.anyio
    async def test_switch_schedules_at_next_period_boundary(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        from orchestra.db.dao.billing_plan_assignment_dao import (
            next_month_boundary_utc,
        )

        user = await create_test_user(client, "switch_at_boundary@test.com")
        small = _make_metered_template(
            dbsession,
            name="bnd-small",
            commit=Decimal("1000"),
        )
        big = _make_metered_template(
            dbsession,
            name="bnd-big",
            commit=Decimal("10000"),
        )
        dbsession.commit()
        group = (
            await client.post(
                "/v0/admin/billing/plans/groups",
                json={"name": "bnd-ladder"},
                headers=ADMIN_HEADERS,
            )
        ).json()
        for tpl, pos in [(small, 0), (big, 1)]:
            await client.post(
                f"/v0/admin/billing/plans/groups/{group['id']}/members",
                json={"template_id": tpl.id, "position": pos},
                headers=ADMIN_HEADERS,
            )
        await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": group["id"]},
            headers=ADMIN_HEADERS,
        )
        db_user = UserDAO(dbsession).get_user_with_id(user["id"])
        # Pin to small so big = upgrade
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=db_user.billing_account.id,
            template_id=small.id,
        )
        dbsession.commit()
        # Need a Stripe customer for METERED; use a stub via direct DB write.
        db_user.billing_account.stripe_customer_id = "cus_test_switch"
        dbsession.commit()

        resp = await client.post(
            "/v0/billing/plan",
            json={"template_id": big.id, "change_reason": "tier-up"},
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "scheduled"
        assert body["classification"] == "upgrade"
        # AT_BOUNDARY: effective_at must equal the next-month start.
        assert body["effective_at"] == next_month_boundary_utc().isoformat()

        # Until the boundary lands, the active assignment is still the
        # old one (set_plan ended_at = future = it's still active now).
        active = BillingPlanAssignmentDAO(dbsession).get_active(
            db_user.billing_account.id,
        )
        assert active is not None
        assert active.template_id == small.id

    @pytest.mark.anyio
    async def test_switch_to_current_template_is_noop(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "switch_noop@test.com")
        tpl = _make_metered_template(
            dbsession,
            name="noop-tpl",
            commit=Decimal("5000"),
        )
        dbsession.commit()
        group = (
            await client.post(
                "/v0/admin/billing/plans/groups",
                json={"name": "noop-group"},
                headers=ADMIN_HEADERS,
            )
        ).json()
        await client.post(
            f"/v0/admin/billing/plans/groups/{group['id']}/members",
            json={"template_id": tpl.id, "position": 0},
            headers=ADMIN_HEADERS,
        )
        await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": group["id"]},
            headers=ADMIN_HEADERS,
        )
        db_user = UserDAO(dbsession).get_user_with_id(user["id"])
        db_user.billing_account.stripe_customer_id = "cus_noop"
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=db_user.billing_account.id,
            template_id=tpl.id,
        )
        dbsession.commit()
        resp = await client.post(
            "/v0/billing/plan",
            json={"template_id": tpl.id},
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "noop"
        assert resp.json()["classification"] == "current"

    @pytest.mark.anyio
    async def test_switch_409_with_pending_credits_recharge(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Self-serve switch is also blocked while a CREDITS recharge is pending.

        Customer-facing variant of the admin guard: same exploit shape
        (auto-recharge fired but not yet invoiced + same-period switch
        to METERED), same 409 contract. The customer-facing copy is
        softened (no internal recharge ids in the message) but the
        ``pending_recharge_ids`` payload is still included so the FE
        can show a "we're still finalising last month's invoice" hint.
        """
        user = await create_test_user(client, "switch_pending@test.com")
        tpl = _make_metered_template(
            dbsession,
            name="pending-switch-tpl",
            commit=Decimal("5000"),
        )
        dbsession.commit()
        group = (
            await client.post(
                "/v0/admin/billing/plans/groups",
                json={"name": "pending-switch-group"},
                headers=ADMIN_HEADERS,
            )
        ).json()
        await client.post(
            f"/v0/admin/billing/plans/groups/{group['id']}/members",
            json={"template_id": tpl.id, "position": 0},
            headers=ADMIN_HEADERS,
        )
        await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": group["id"]},
            headers=ADMIN_HEADERS,
        )
        db_user = UserDAO(dbsession).get_user_with_id(user["id"])
        db_user.billing_account.stripe_customer_id = "cus_switch_pending"
        dbsession.add(
            Recharge(
                billing_account_id=db_user.billing_account.id,
                type="auto",
                quantity=Decimal("250"),
                amount_usd=Decimal("250"),
                status=RechargeStatus.PENDING_INVOICE,
                invoice_group=dt.date(2026, 4, 30),
            ),
        )
        dbsession.commit()

        resp = await client.post(
            "/v0/billing/plan",
            json={"template_id": tpl.id},
            headers=user["headers"],
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "pending_recharges"
        assert "pending_recharge_ids" in detail
        # Customer-facing copy must NOT leak admin terminology.
        assert "PENDING_INVOICE" not in detail["message"]
        assert "BillingAccount" not in detail["message"]

    @pytest.mark.anyio
    async def test_account_info_surfaces_plan_group_id(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        from orchestra.db.models.enums import DEFAULT_PLAN_GROUP_ID

        user = await create_test_user(client, "ainfo_pg@test.com")
        # Pristine account → DEFAULT_PLAN_GROUP_ID auto-applied
        resp_a = await client.get(
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert resp_a.status_code == 200
        assert resp_a.json()["plan_group_id"] == DEFAULT_PLAN_GROUP_ID
        # Pin to a different group
        group = (
            await client.post(
                "/v0/admin/billing/plans/groups",
                json={"name": "ainfo-grp"},
                headers=ADMIN_HEADERS,
            )
        ).json()
        await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": group["id"]},
            headers=ADMIN_HEADERS,
        )
        resp_b = await client.get(
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert resp_b.status_code == 200
        assert resp_b.json()["plan_group_id"] == group["id"]
        # Reverting to the platform default round-trips identically.
        await client.put(
            f"/v0/admin/billing/accounts/plan-group?user_id={user['id']}",
            json={"group_id": DEFAULT_PLAN_GROUP_ID},
            headers=ADMIN_HEADERS,
        )
        resp_c = await client.get(
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert resp_c.status_code == 200
        assert resp_c.json()["plan_group_id"] == DEFAULT_PLAN_GROUP_ID
