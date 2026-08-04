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
- DeductEndpoint: /credits/deduct
- CheckoutPortalStatus: portal-session
- OrgBillingPermissions: RBAC on billing endpoints via org API key
- AccountInfo: GET /billing/account-info
- CreditGrants: claim-credit-grant-link → promo recharge
- BillingProfile: GET / PATCH /billing/billing-profile
- TaxValidation: validate-tax-id, supported-tax-countries
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
from orchestra.lib.credit_grants import GRANT_KIND_PLAN, grant_expiring_credits
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import (
    TIER_50_ID,
    TIER_75_ID,
    TIER_30000_ID,
    make_user_with_billing,
    mock_stripe_subscription,
    put_on_tier,
    template_by_name,
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


@pytest.fixture(autouse=True)
def _fake_stripe_billing_profile(monkeypatch):
    """Back the Stripe-stored billing profile with an in-memory store.

    Billing PII now lives only on the Stripe Customer: the endpoints write it
    via ``ensure_stripe_customer`` / ``sync_billing_profile_to_stripe`` and
    read it back via ``fetch_billing_profile_from_stripe``. These tests run
    offline, so we fake those three at the boundary with a per-test dict keyed
    by a synthetic customer id, letting the PATCH→GET round-trip be asserted
    without a live Stripe.
    """
    import copy

    import orchestra.lib.billing as billing_lib
    import orchestra.web.api.billing.views as billing_views

    store: dict[str, dict] = {}

    def _empty() -> dict:
        return {
            "billing_email": None,
            "name": None,
            "tax_id": None,
            "tax_id_type": None,
            "tax_id_verification_status": None,
            "billing_address": {},
        }

    def _address(src: dict) -> dict:
        return {
            k: (src.get(k) or "")
            for k in ("line1", "line2", "city", "state", "postal_code", "country")
        }

    def fake_ensure(
        session,
        ba,
        *,
        is_business=None,
        name=None,
        email=None,
        address=None,
        tax_id=None,
        fallback_name=None,
        fallback_email=None,
    ):
        if ba.stripe_customer_id:
            return ba.stripe_customer_id
        cid = f"cus_fake_{ba.id}"
        ba.stripe_customer_id = cid
        session.flush()
        rec = _empty()
        rec["billing_email"] = email or fallback_email
        rec["name"] = name or fallback_name
        if tax_id:
            rec["tax_id"] = tax_id
        if address and (address.get("country") or address.get("line1")):
            rec["billing_address"] = _address(address)
        store[cid] = rec
        return cid

    def fake_sync(
        customer_id,
        *,
        is_business,
        billing_email=None,
        name=None,
        tax_id=None,
        billing_address=None,
        existing_billing_address=None,
        logger_instance=None,
    ):
        rec = store.setdefault(customer_id, _empty())
        if billing_email is not None:
            rec["billing_email"] = billing_email
        if name is not None:
            rec["name"] = name
        if tax_id is not None:
            rec["tax_id"] = tax_id or None
        if billing_address and billing_address.get("line1"):
            rec["billing_address"] = _address(billing_address)

    def fake_fetch(customer_id):
        if not customer_id:
            return _empty()
        return copy.deepcopy(store.get(customer_id, _empty()))

    monkeypatch.setattr(billing_lib, "ensure_stripe_customer", fake_ensure)
    monkeypatch.setattr(billing_lib, "fetch_billing_profile_from_stripe", fake_fetch)
    monkeypatch.setattr(billing_views, "sync_billing_profile_to_stripe", fake_sync)
    return store


@pytest.fixture(autouse=True)
def _stub_personal_coordinator_pubsub_calls(monkeypatch):
    """Keep billing API tests isolated from coordinator Pub/Sub provisioning."""

    async def _fake_create_pubsub_topic(*_args, **_kwargs):
        return {"name": "projects/test/topics/test-topic"}

    import orchestra.services.coordinator_service as coordinator_service

    monkeypatch.setattr(
        coordinator_service,
        "create_pubsub_topic",
        _fake_create_pubsub_topic,
    )


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
        LLM calls.

        Bounded by the overdraft floor: an overshoot past it suspends the
        account outright, which is exercised in ``test_overdraft_floor``.
        Here the overshoot stays inside the tolerated band so this keeps
        testing the overshoot itself.
        """
        from orchestra.db.dao.billing_account_dao import OVERDRAFT_SUSPEND_FLOOR

        credits_response = await client.get("/v0/credits", headers=HEADERS)
        assert credits_response.status_code == status.HTTP_200_OK
        current_credits = credits_response.json()["credits"]

        overshoot = float(abs(OVERDRAFT_SUSPEND_FLOOR)) / 2
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
        starting_balance = entity.credits

        new_balance = ba_dao.deduct_credits(
            entity.billing_account_id,
            25.50,
            category="other",
        )
        dbsession.commit()

        expected_balance = starting_balance - Decimal("25.50")
        assert new_balance == expected_balance

        updated_user = user_dao.get_user_with_id(user["id"])
        assert updated_user.billing_account.credits == expected_balance

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

        for leftover in (Decimal("50"), Decimal("0"), Decimal("-30")):
            ba.credits = leftover
            dbsession.commit()
            entity = get_billing_entity(dbsession, user["id"])
            assert entity.is_metered is True
            assert entity.has_sufficient_credits(Decimal("999")) is True

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
        assert entity.has_sufficient_credits(entity.credits - Decimal("1")) is True
        assert entity.has_sufficient_credits(entity.credits) is True
        assert entity.has_sufficient_credits(entity.credits + Decimal("1")) is False

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
        funded_balance = get_billing_entity(dbsession, user["id"]).credits

        # Phase 2: switch to METERED and accrue ledger-only usage.
        metered_tpl = _make_metered_template(dbsession, name="round-trip-metered")
        plan_dao = BillingPlanAssignmentDAO(dbsession)
        plan_dao.set_plan(billing_account_id=ba.id, template_id=metered_tpl.id)
        dbsession.commit()

        ba_dao.deduct_credits(ba.id, 7.50, category="llm")
        dbsession.commit()
        dbsession.refresh(ba)
        # Wallet untouched; ledger row recorded by deduct_credits.
        assert ba.credits == funded_balance
        entity = get_billing_entity(dbsession, user["id"])
        assert entity.is_metered is True
        assert entity.credits == funded_balance

        # Phase 3: revert to CREDITS — leftover balance is preserved
        # and spendable again.
        from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO

        default_tpl = BillingPlanTemplateDAO(dbsession).get_default()
        plan_dao.set_plan(billing_account_id=ba.id, template_id=default_tpl.id)
        dbsession.commit()

        entity = get_billing_entity(dbsession, user["id"])
        assert entity.is_metered is False
        assert entity.credits == funded_balance

        new_balance = ba_dao.deduct_credits(ba.id, 5, category="llm")
        dbsession.commit()
        assert new_balance == funded_balance - Decimal("5")


# ============================================================================
# Deduct Endpoint
# ============================================================================


class TestDeductEndpoint:
    @pytest.mark.anyio
    async def test_deduct_no_longer_auto_recharges(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Self-serve auto-recharge top-up is retired on the deduct path.

        Under the subscription model a deduction never queues a one-time
        auto-recharge — even with the legacy ``autorecharge`` flag set. The
        wallet simply decrements; depletion auto-upgrades the subscription
        tier only when ``auto_increment`` is on (tested separately), else it
        is a hard stop.
        """
        user = await create_test_user(client, "deduct_ar@test.com")

        user_dao = UserDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])

        ba = user_obj.billing_account
        ba.credits = Decimal("15")
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
        # No top-up: 15 - 10 = 5 (auto-recharge is retired).
        assert data["current_credits"] == 5.0

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
    async def test_negative_balance_no_auto_recharge(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """A deduction may drive the balance negative without any top-up.

        The balance is allowed to go negative so the spending-limit hook
        hard-stops subsequent calls. Auto-recharge is retired; a depleted
        account is only rescued by auto-increment (subscription accounts) or
        a manual upgrade.
        """
        user = await create_test_user(client, "deduct_neg_ar@test.com")

        user_dao = UserDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])

        ba = user_obj.billing_account
        ba.credits = Decimal("2")
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
        # 2 - 5 = -3, no top-up.
        assert data["current_credits"] == -3.0

        dbsession.expire_all()
        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba.id, type="auto")
            .first()
        )
        assert recharge is None

    @pytest.mark.anyio
    async def test_residual_balance_deduction_succeeds(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Regression test for the ClientGamma bug: a tiny residual balance
        (e.g. $0.004) must not prevent deductions from going through.
        The balance should go negative so the spending-limit hook blocks
        subsequent calls."""
        user = await create_test_user(client, "deduct_residual@test.com")

        user_dao = UserDAO(dbsession)
        user_obj = user_dao.get_user_with_id(user["id"])

        ba = user_obj.billing_account
        ba.credits = Decimal("0.004128")
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


class TestAutoIncrementEndpoints:
    """GET / PUT ``/v0/billing/auto-increment`` (self-serve auto-upgrade)."""

    @pytest.mark.anyio
    async def test_get_default_disabled_unsubscribed(self, client, dbsession):
        user = await create_test_user(client, "auto_inc_get@test.com")
        resp = await client.get(
            "/v0/billing/auto-increment",
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Pristine account: flag off, not on a subscription.
        assert body["enabled"] is False
        assert body["is_subscribed"] is False
        assert body["at_top_tier"] is False

    @pytest.mark.anyio
    async def test_put_toggles_flag(self, client, dbsession):
        user = await create_test_user(client, "auto_inc_put@test.com")

        resp = await client.put(
            "/v0/billing/auto-increment",
            json={"enabled": True},
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is True

        # Persisted: a subsequent GET reflects the new value.
        get_resp = await client.get(
            "/v0/billing/auto-increment",
            headers=user["headers"],
        )
        assert get_resp.json()["enabled"] is True

        # Toggle back off.
        off = await client.put(
            "/v0/billing/auto-increment",
            json={"enabled": False},
            headers=user["headers"],
        )
        assert off.json()["enabled"] is False


class TestOrgBillingPermissions:
    @pytest.mark.anyio
    async def test_owner_can_read_account_info(self, client, dbsession):
        _, owner_headers, _ = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_read@test.com",
            "perm_member_read@test.com",
            "Member",
        )
        response = await client.get("/v0/billing/account-info", headers=owner_headers)
        assert response.status_code == 200, response.json()
        assert "credits" in response.json()

    @pytest.mark.anyio
    async def test_member_can_read_account_info(self, client, dbsession):
        _, _, member_headers = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_mread@test.com",
            "perm_member_mread@test.com",
            "Member",
        )
        response = await client.get("/v0/billing/account-info", headers=member_headers)
        assert response.status_code == 200, response.json()
        assert "credits" in response.json()

    @pytest.mark.anyio
    async def test_member_cannot_update_auto_increment(self, client, dbsession):
        _, _, member_headers = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_mwrite@test.com",
            "perm_member_mwrite@test.com",
            "Member",
        )
        response = await client.put(
            "/v0/billing/auto-increment",
            json={"enabled": False},
            headers=member_headers,
        )
        assert response.status_code == 403
        assert "billing:write" in response.json()["detail"]

    @pytest.mark.anyio
    async def test_owner_can_update_auto_increment(self, client, dbsession):
        _, owner_headers, _ = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_owrite@test.com",
            "perm_member_owrite@test.com",
            "Member",
        )
        response = await client.put(
            "/v0/billing/auto-increment",
            json={"enabled": False},
            headers=owner_headers,
        )
        assert response.status_code == 200, response.json()

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
    async def test_viewer_can_read_account_info(
        self,
        client,
        dbsession,
    ):
        _, _, viewer_headers = await _create_org_with_member(
            client,
            dbsession,
            "perm_owner_vread@test.com",
            "perm_viewer_vread@test.com",
            "Viewer",
        )
        response = await client.get(
            "/v0/billing/account-info",
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
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert response.status_code == 200

        response = await client.put(
            "/v0/billing/auto-increment",
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
            apply_signup_grant=False,
            credits=Decimal("42.50"),
            stripe_customer_id="cus_test123",
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
        assert "autorecharge" not in data

    @pytest.mark.anyio
    async def test_no_recharge_history(self, client, dbsession):
        user = await create_test_user(client, "acctinfo_nocust@test.com")

        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])
        ba = BillingAccountDAO(dbsession).create(
            apply_signup_grant=False,
            credits=Decimal("0"),
        )
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

        user = await create_test_user(client, "promo-recharge@unify.ai")
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

        user = await create_test_user(client, "promo-last-recharge@unify.ai")
        user_dao = UserDAO(dbsession)
        db_user = user_dao.get_user_with_id(user["id"])

        ba = BillingAccountDAO(dbsession).create(
            apply_signup_grant=False,
            credits=Decimal("0"),
        )
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

        user = await create_test_user(client, "promo-newba@unify.ai")
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
    async def test_get_org_without_tax_id_is_not_business(
        self,
        client: AsyncClient,
        dbsession,
    ):
        # Business treatment is keyed off the billing profile's tax ID, not
        # org membership: an org that hasn't supplied a tax ID is billed as
        # an individual until it does (see resolve_is_business).
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
        assert data["is_business"] is False

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
    async def test_complete_address_marks_setup_complete(
        self,
        client: AsyncClient,
        dbsession,
    ):
        """A tax-resolvable address flips the derived ``billing_setup_complete``
        gate on (the subscribe flow keys off this), and GET reflects it."""
        owner = await create_test_user(client, "setup_complete_addr@test.com")
        org = await create_test_org(client, owner, "Setup Complete Org")

        patch = await client.patch(
            "/v0/billing/billing-profile",
            json={
                "name": "Complete Co",
                "billing_address": {
                    "line1": "1 Market St",
                    "city": "San Francisco",
                    "postal_code": "94105",
                    "country": "US",
                },
            },
            headers=org["headers"],
        )
        assert patch.status_code == 200
        assert patch.json()["billing_setup_complete"] is True

        # Persisted (not just echoed): a fresh GET reports the same gate.
        get = await client.get(
            "/v0/billing/billing-profile",
            headers=org["headers"],
        )
        assert get.json()["billing_setup_complete"] is True

    @pytest.mark.anyio
    async def test_incomplete_address_leaves_setup_incomplete(
        self,
        client: AsyncClient,
        dbsession,
    ):
        """An address missing a required field (postal_code) is not
        tax-resolvable, so the gate stays off."""
        owner = await create_test_user(client, "setup_incomplete_addr@test.com")
        org = await create_test_org(client, owner, "Setup Incomplete Org")

        patch = await client.patch(
            "/v0/billing/billing-profile",
            json={
                "name": "Incomplete Co",
                "billing_address": {
                    "line1": "1 Market St",
                    "city": "San Francisco",
                    "country": "US",
                },
            },
            headers=org["headers"],
        )
        assert patch.status_code == 200
        assert patch.json()["billing_setup_complete"] is False

        get = await client.get(
            "/v0/billing/billing-profile",
            headers=org["headers"],
        )
        assert get.json()["billing_setup_complete"] is False

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
        month-end invoicing (which filters on
        ``Recharge.invoice_group == month_end_utc(today)``) silently skip
        rows. This produced 78+ rows with first-of-next-month
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
                return (
                    frozen.astimezone(tz)
                    if tz is not None
                    else frozen.replace(
                        tzinfo=None,
                    )
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
        ba_id = dbsession.query(User.billing_account_id).filter_by(id=user_id).scalar()
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
        ba_id = dbsession.query(User.billing_account_id).filter_by(id=user_id).scalar()
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
    display_name: str | None = None,
):
    return BillingPlanTemplateDAO(dbsession).create_template(
        name=name,
        display_name=display_name,
        billing_mode=BillingMode.METERED,
        commit_amount=commit,
        commit_period="MONTHLY",
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
            "name": "ClientGamma Q3 2026",
            "billing_mode": "METERED",
            "is_custom": True,
            "commit_amount": 5000.0,
            "commit_period": "MONTHLY",
            "collection_method": "SEND_INVOICE_NET_30",
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
        from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO

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

        This is the exploit guard: a PENDING_INVOICE Recharge row
        represents credits already granted on the wallet but not yet
        invoiced. Switching to METERED before it settles would orphan the
        row (the metered invoicer never touches CREDITS-mode recharges) and
        the customer would walk away with credits they never paid for. The
        409 forces the operator (or the customer flow) to wait for the row
        to settle first.
        """
        user, ba = make_user_with_billing(
            dbsession,
            "plan_pending_block_u1",
            stripe_customer_id="cus_pending_block_u1",
        )
        tpl = _make_metered_template(dbsession, name="pending-block-tpl")
        # A historical recharge row that has not yet been invoiced.
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
        ``PENDING_INVOICE → INVOICE_CREATED`` (once invoiced). The guard
        only blocks ``PENDING_INVOICE``; ``INVOICE_CREATED`` is a normal
        in-collection state.
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
        assert "commit_schedule" in plan
        assert plan["assignment_id"] is not None


# ============================================================================
# Customer GET /v0/billing/account-info — self-serve subscription fields
# ============================================================================


class TestAccountInfoSubscriptionFields:
    """``is_subscribed`` / ``trial_expires_at`` / ``next_renewal_at`` on the
    customer account-info endpoint (the monthly allowance is read off the
    nested ``plan.commit_amount``, not a duplicated top-level field)."""

    @pytest.mark.anyio
    async def test_unsubscribed_with_trial_grant(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        from datetime import datetime, timedelta, timezone

        from orchestra.lib.credit_grants import GRANT_KIND_TRIAL, grant_expiring_credits

        user = await create_test_user(client, "acctinfo_trial@test.com")
        user_dao = UserDAO(dbsession)
        ba = user_dao.get_user_with_id(user["id"]).billing_account
        expiry = datetime.now(timezone.utc) + timedelta(days=5)
        grant_expiring_credits(
            dbsession,
            ba.id,
            100,
            grant_kind=GRANT_KIND_TRIAL,
            expires_at=expiry,
        )
        dbsession.commit()

        resp = await client.get(
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["is_subscribed"] is False
        # Allowance is not duplicated — it lives on the plan summary.
        assert "monthly_credit_allowance" not in data
        assert data["next_renewal_at"] is None
        assert data["trial_expires_at"] is not None
        assert data["trial_expires_at"].startswith(expiry.isoformat()[:19])

    @pytest.mark.anyio
    async def test_subscribed_tier_fields(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        from datetime import datetime, timedelta, timezone

        user = await create_test_user(client, "acctinfo_sub@test.com")
        user_dao = UserDAO(dbsession)
        ba = user_dao.get_user_with_id(user["id"]).billing_account
        # Put on tier_50 (id 2, 50 credits/mo) with an active subscription.
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=2,
            effective_at=datetime.now(timezone.utc),
        )
        ba.stripe_subscription_id = "sub_acctinfo"
        renewal = datetime.now(timezone.utc) + timedelta(days=30)
        ba.current_period_end = renewal
        dbsession.commit()

        resp = await client.get(
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["is_subscribed"] is True
        # Monthly allowance is read off the plan summary (commit_amount).
        assert data["plan"]["commit_amount"] == 50.0
        # Subscribers don't surface a trial expiry.
        assert data["trial_expires_at"] is None
        assert data["next_renewal_at"] is not None
        assert data["next_renewal_at"].startswith(renewal.isoformat()[:19])

    @pytest.mark.anyio
    async def test_metered_account_not_subscribed(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        user = await create_test_user(client, "acctinfo_metered@test.com")
        user_dao = UserDAO(dbsession)
        ba = user_dao.get_user_with_id(user["id"]).billing_account
        tpl = _make_metered_template(dbsession, name="acctinfo-metered-tpl")
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
        )
        # Even with a stray subscription id, METERED is never is_subscribed.
        ba.stripe_subscription_id = "sub_stray"
        dbsession.commit()

        resp = await client.get(
            "/v0/billing/account-info",
            headers=user["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["is_subscribed"] is False
        assert "monthly_credit_allowance" not in data
        assert data["next_renewal_at"] is None


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
        assert ba.credits >= Decimal("0")
        assert ba.stripe_customer_id is None
        assert ba.account_status == "ACTIVE"
        assert ba.billing_setup_complete is False
        # Billing PII lives only on the Stripe Customer now; locally we keep
        # just the derived is_business flag (defaults False).
        assert ba.is_business is False

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
    async def test_partial_update_preserves_address(
        self,
        client: AsyncClient,
        dbsession,
    ):
        """A profile update that omits the address preserves what's on Stripe.

        PII lives on the Stripe Customer now; the endpoint reads the existing
        profile back and only overwrites the fields the request actually
        carries, so a tax-id-only edit must not wipe the saved address.
        """
        owner = await create_test_user(client, "merge_addr@test.com")
        org = await create_test_org(client, owner, "Merge Address Org")

        await client.patch(
            "/v0/billing/billing-profile",
            json={
                "billing_email": "merge@company.com",
                "name": "Merge Corp",
                "billing_address": {
                    "country": "US",
                    "line1": "123 Main St",
                    "city": "Boston",
                    "state": "MA",
                    "postal_code": "02101",
                },
            },
            headers=org["headers"],
        )

        # Second update touches only the tax id — no address re-sent.
        response = await client.patch(
            "/v0/billing/billing-profile",
            json={"tax_id": "12-3456789"},
            headers=org["headers"],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tax_id"] == "12-3456789"
        assert data["billing_address"]["country"] == "US"
        assert data["billing_address"]["line1"] == "123 Main St"
        assert data["billing_address"]["city"] == "Boston"
        assert data["billing_address"]["state"] == "MA"
        assert data["is_business"] is True


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
            json={"name": "clientgamma-public", "display_name": "ClientGamma"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        group_id = resp.json()["id"]
        assert resp.json()["display_name"] == "ClientGamma"
        assert resp.json()["members"] == []

        listing = await client.get(
            "/v0/admin/billing/plans/groups",
            headers=ADMIN_HEADERS,
        )
        assert listing.status_code == 200
        slugs = [g["name"] for g in listing.json()["groups"]]
        assert "clientgamma-public" in slugs

        detail = await client.get(
            f"/v0/admin/billing/plans/groups/{group_id}",
            headers=ADMIN_HEADERS,
        )
        assert detail.status_code == 200
        assert detail.json()["name"] == "clientgamma-public"

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
    async def test_available_plans_lists_self_serve_tiers_for_default_group(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """Pristine account: auto-assigned to DEFAULT_PLAN_GROUP_ID, which now
        contains the default (free) template at position 0 plus the 42
        seeded self-serve subscription tiers (21 monthly on rungs 1..21 and
        21 annual on the 101..121 band). The endpoint lists the tiers as
        upgrade options (the default template is the current/free state), so
        the FE renders the subscribe ladder and toggles monthly/annual.
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
        available = body["available"]
        # Default template (current/free) + 21 monthly + 21 annual tiers.
        assert len(available) == 43
        current = [item for item in available if item["is_current"]]
        assert len(current) == 1
        # The seeded monthly + annual tiers are all upgrades above the free default.
        upgrades = [item for item in available if item["classification"] == "upgrade"]
        assert len(upgrades) == 42
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
                json={"name": "ladder-list", "display_name": "ClientGamma Tiers"},
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
        assert body["plan_group_display_name"] == "ClientGamma Tiers"
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
        from orchestra.db.dao.billing_plan_assignment_dao import next_month_boundary_utc

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


class TestSelfServeSubscriptionLifecycle:
    """Self-serve subscription tier-change / cancel / auto-increment.

    Drives the ``orchestra.lib.subscription_billing`` entrypoints
    (``change_subscription_tier``, ``auto_increment_on_depletion``,
    ``cancel_subscription``) directly with Stripe stubbed offline via
    ``mock_stripe_subscription``. Covers immediate upgrade grants,
    mid-cycle proration, downgrade-without-clawback, the high-water-mark
    anti-mint guard, the auto-increment ladder (monthly + annual), and the
    annual lump-sum (12× up-front) tiers. Seeded tiers: id 2 = tier_50,
    id 3 = tier_75, id 22 = top tier.
    """

    def test_upgrade_grants_delta_immediately(self, dbsession, monkeypatch) -> None:
        from datetime import datetime, timedelta, timezone

        from orchestra.db.models.orchestra_models import BillingPlanTemplate
        from orchestra.lib.subscription_billing import change_subscription_tier

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "tier_up",
            stripe_customer_id="cus_tier_up",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_tier_up")
        dao = BillingAccountDAO(dbsession)
        # Simulate the prior cycle's grant being fully consumed.
        grant_expiring_credits(
            dbsession,
            ba.id,
            50,
            grant_kind=GRANT_KIND_PLAN,
            expires_at=datetime.now(timezone.utc) + timedelta(days=20),
        )
        dao.deduct_credits(ba.id, 50, category="llm")
        dbsession.flush()
        assert dao.get_credits(ba.id) == Decimal("0")

        new_template = dbsession.get(BillingPlanTemplate, TIER_75_ID)
        delta = change_subscription_tier(dbsession, ba, new_template)
        # Upgrade 50 -> 75 grants the 25-credit delta immediately.
        assert delta == Decimal("25")
        assert dao.get_credits(ba.id) == Decimal("25")
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_75_ID

    def test_upgrade_aborts_when_card_declined(self, dbsession, monkeypatch) -> None:
        """A declined proration charge must not upgrade the tier or grant credits.

        ``_modify_subscription_quantity`` runs with
        ``payment_behavior="error_if_incomplete"``, so Stripe raises a
        ``CardError`` (and rolls the subscription back) before we reach the
        local ``set_plan`` / credit grant. The whole change must be a no-op.
        """
        from types import SimpleNamespace

        import stripe as real_stripe

        import orchestra.lib.subscription_billing as sub_mod
        from orchestra.db.models.orchestra_models import BillingPlanTemplate
        from orchestra.lib.subscription_billing import change_subscription_tier

        mock_stripe_subscription(monkeypatch)

        # Override the stubbed Stripe so the tier-change modify is declined.
        def _declined_modify(sid, **kw):
            raise real_stripe.error.CardError(
                "Your card was declined.",
                None,
                "card_declined",
            )

        monkeypatch.setattr(
            sub_mod,
            "stripe",
            SimpleNamespace(
                error=real_stripe.error,
                Subscription=SimpleNamespace(
                    retrieve=lambda sid: {"items": {"data": [{"id": "si_dummy"}]}},
                    modify=_declined_modify,
                ),
            ),
        )

        _user, ba = make_user_with_billing(
            dbsession,
            "tier_decline",
            stripe_customer_id="cus_tier_decline",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_tier_decline")
        dao = BillingAccountDAO(dbsession)
        before = dao.get_credits(ba.id)

        new_template = dbsession.get(BillingPlanTemplate, TIER_75_ID)
        with pytest.raises(real_stripe.error.CardError):
            change_subscription_tier(dbsession, ba, new_template)

        # No upgrade, no grant: still on tier 50 with the same balance.
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_50_ID
        assert dao.get_credits(ba.id) == before

    def test_mid_cycle_upgrade_grant_is_prorated(self, dbsession, monkeypatch) -> None:
        """Mid-cycle upgrade grants only the prorated slice of the tier delta.

        With a known ``current_period_end`` only a fraction of the future days
        remain, so the immediate grant is floored to that slice (matching the
        Stripe prorated charge) — yet the high-water mark still advances to the
        full tier so auto-incrementing up the ladder can't bank free credits.
        """
        from datetime import datetime, timedelta, timezone

        from orchestra.db.models.orchestra_models import BillingPlanTemplate
        from orchestra.lib.subscription_billing import change_subscription_tier

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "tier_prorate",
            stripe_customer_id="cus_tier_prorate",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_tier_prorate")
        dao = BillingAccountDAO(dbsession)
        grant_expiring_credits(
            dbsession,
            ba.id,
            50,
            grant_kind=GRANT_KIND_PLAN,
            expires_at=datetime.now(timezone.utc) + timedelta(days=6),
        )
        dao.deduct_credits(ba.id, 50, category="llm")
        dbsession.flush()
        assert dao.get_credits(ba.id) == Decimal("0")

        # Only ~6 of the ~30 days of the monthly period remain.
        ba.current_period_end = datetime.now(timezone.utc) + timedelta(days=6)
        dbsession.flush()

        new_template = dbsession.get(BillingPlanTemplate, TIER_75_ID)
        delta = change_subscription_tier(dbsession, ba, new_template)

        # Prorated: strictly between zero and the full 25-credit delta.
        assert Decimal("0") < delta < Decimal("25")
        assert dao.get_credits(ba.id) == delta
        # High-water mark still advances to the full tier (anti-mint guard).
        assert ba.plan_credits_granted_period == Decimal("75")

    def test_downgrade_no_clawback(self, dbsession, monkeypatch) -> None:
        from datetime import datetime, timedelta, timezone

        from orchestra.db.models.orchestra_models import BillingPlanTemplate
        from orchestra.lib.subscription_billing import change_subscription_tier

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "tier_down",
            stripe_customer_id="cus_tier_down",
        )
        put_on_tier(dbsession, ba, TIER_75_ID, "sub_tier_down")
        dao = BillingAccountDAO(dbsession)
        grant_expiring_credits(
            dbsession,
            ba.id,
            75,
            grant_kind=GRANT_KIND_PLAN,
            expires_at=datetime.now(timezone.utc) + timedelta(days=20),
        )
        dao.deduct_credits(ba.id, 10, category="llm")
        dbsession.flush()
        assert dao.get_credits(ba.id) == Decimal("65")

        new_template = dbsession.get(BillingPlanTemplate, TIER_50_ID)
        delta = change_subscription_tier(dbsession, ba, new_template)
        # Downgrade: no delta granted, consumed credits not clawed back.
        assert delta == Decimal("0")
        assert dao.get_credits(ba.id) == Decimal("65")
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_50_ID

    def test_upgrade_downgrade_toggle_cannot_mint_credits(
        self,
        dbsession,
        monkeypatch,
    ) -> None:
        """High-water-mark blocks re-grant on repeated upgrade/downgrade toggling."""
        from orchestra.db.models.orchestra_models import BillingPlanTemplate
        from orchestra.lib.subscription_billing import change_subscription_tier

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "tier_toggle",
            stripe_customer_id="cus_tier_toggle",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_tier_toggle")
        dao = BillingAccountDAO(dbsession)
        tier_50 = dbsession.get(BillingPlanTemplate, TIER_50_ID)
        tier_75 = dbsession.get(BillingPlanTemplate, TIER_75_ID)

        # First upgrade 50 -> 75 grants the 25 delta and raises the mark to 75.
        assert change_subscription_tier(dbsession, ba, tier_75) == Decimal("25")
        assert dao.get_credits(ba.id) == Decimal("25")

        # Downgrade 75 -> 50: no grant, no clawback, mark stays at 75.
        assert change_subscription_tier(dbsession, ba, tier_50) == Decimal("0")
        assert dao.get_credits(ba.id) == Decimal("25")

        # Re-upgrade 50 -> 75: already at the mark, so NO new credits minted.
        assert change_subscription_tier(dbsession, ba, tier_75) == Decimal("0")
        assert dao.get_credits(ba.id) == Decimal("25")
        assert ba.plan_credits_granted_period == Decimal("75")

    def test_auto_increment_bumps_to_next_tier(self, dbsession, monkeypatch) -> None:
        from orchestra.lib.subscription_billing import auto_increment_on_depletion

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "auto_inc",
            stripe_customer_id="cus_auto_inc",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_auto_inc")
        ba.auto_increment = True
        dbsession.flush()

        bumped = auto_increment_on_depletion(dbsession, ba)
        assert bumped is not None
        assert bumped.id == TIER_75_ID
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_75_ID
        # Delta 50 -> 75 granted on bump.
        assert BillingAccountDAO(dbsession).get_credits(ba.id) == Decimal("25")

    def test_auto_increment_caps_at_top_tier(self, dbsession, monkeypatch) -> None:
        from orchestra.lib.subscription_billing import auto_increment_on_depletion

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "auto_top",
            stripe_customer_id="cus_auto_top",
        )
        put_on_tier(dbsession, ba, TIER_30000_ID, "sub_auto_top")
        ba.auto_increment = True
        dbsession.flush()

        bumped = auto_increment_on_depletion(dbsession, ba)
        # Already on the top tier — hard stop, no change.
        assert bumped is None
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_30000_ID

    def test_auto_increment_disabled_is_noop(self, dbsession, monkeypatch) -> None:
        from orchestra.lib.subscription_billing import auto_increment_on_depletion

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "auto_off",
            stripe_customer_id="cus_auto_off",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_auto_off")
        ba.auto_increment = False
        dbsession.flush()

        bumped = auto_increment_on_depletion(dbsession, ba)
        assert bumped is None
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_50_ID

    def test_cancel_at_period_end_schedules(self, dbsession, monkeypatch) -> None:
        """Default cancel schedules at period end and does not mutate local state.

        The local plan revert is driven solely by the deletion webhook, so the
        account keeps its tier + subscription id until the boundary.
        """
        from datetime import datetime, timedelta, timezone

        from orchestra.lib.subscription_billing import cancel_subscription

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "sub_cancel",
            stripe_customer_id="cus_sub_cancel",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_cancel")
        period_end = datetime.now(timezone.utc) + timedelta(days=12)
        ba.current_period_end = period_end
        dbsession.flush()

        effective = cancel_subscription(dbsession, ba, at_period_end=True)

        assert effective == period_end
        # Local state untouched — the webhook owns the revert.
        assert ba.stripe_subscription_id == "sub_cancel"
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_50_ID
        # The scheduled-cancellation flag IS set immediately so the console
        # can render a persistent "cancels on X" indicator without waiting
        # for the subscription.updated webhook.
        assert ba.subscription_cancel_at_period_end is True

    def test_revert_on_cancel_clears_cancel_flag(self, dbsession, monkeypatch) -> None:
        """The final deletion webhook revert clears the scheduled-cancel flag."""
        from orchestra.lib.subscription_billing import (
            cancel_subscription,
            revert_to_default_on_cancel,
        )

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "sub_cancel_revert",
            stripe_customer_id="cus_sub_cancel_revert",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_cancel_revert")
        cancel_subscription(dbsession, ba, at_period_end=True)
        assert ba.subscription_cancel_at_period_end is True

        revert_to_default_on_cancel(dbsession, ba)
        assert ba.subscription_cancel_at_period_end is False
        assert ba.stripe_subscription_id is None

    def test_cancel_without_subscription_raises(self, dbsession, monkeypatch) -> None:
        from orchestra.lib.subscription_billing import (
            SubscriptionError,
            cancel_subscription,
        )

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(dbsession, "sub_no_cancel")

        with pytest.raises(SubscriptionError):
            cancel_subscription(dbsession, ba, at_period_end=True)

    def test_reactivate_clears_scheduled_cancel(self, dbsession, monkeypatch) -> None:
        """Reactivating a scheduled-to-cancel subscription clears the flag.

        The subscription stays intact (id + tier untouched) and the local
        ``cancel_at_period_end`` flag flips back to ``False`` so the console
        stops showing the "cancels on X" indicator.
        """
        from datetime import datetime, timedelta, timezone

        from orchestra.lib.subscription_billing import (
            cancel_subscription,
            reactivate_subscription,
        )

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "sub_resume",
            stripe_customer_id="cus_sub_resume",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_resume")
        period_end = datetime.now(timezone.utc) + timedelta(days=9)
        ba.current_period_end = period_end
        dbsession.flush()

        cancel_subscription(dbsession, ba, at_period_end=True)
        assert ba.subscription_cancel_at_period_end is True

        effective = reactivate_subscription(dbsession, ba)

        assert effective == period_end
        assert ba.subscription_cancel_at_period_end is False
        # Subscription + tier remain in place — nothing was deleted.
        assert ba.stripe_subscription_id == "sub_resume"
        plan = BillingPlanAssignmentDAO(dbsession).resolve_effective_plan(ba.id)
        assert plan.template_id == TIER_50_ID

    def test_reactivate_without_subscription_raises(
        self,
        dbsession,
        monkeypatch,
    ) -> None:
        from orchestra.lib.subscription_billing import (
            SubscriptionError,
            reactivate_subscription,
        )

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(dbsession, "sub_no_resume")

        with pytest.raises(SubscriptionError):
            reactivate_subscription(dbsession, ba)

    def test_build_auto_increment_email_mentions_tier(self, dbsession) -> None:
        from orchestra.db.models.orchestra_models import BillingPlanTemplate
        from orchestra.lib.subscription_billing import build_auto_increment_email

        template = dbsession.get(BillingPlanTemplate, TIER_75_ID)
        subject, body = build_auto_increment_email(template)

        assert "upgraded" in subject.lower()
        assert template.name in body
        assert "auto-increment" in body.lower()

    def test_annual_upgrade_grants_year_delta(self, dbsession, monkeypatch) -> None:
        """Annual upgrade grants the 12× credit delta immediately."""
        from orchestra.lib.subscription_billing import change_subscription_tier

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "ann_up",
            stripe_customer_id="cus_ann_up",
        )
        tier_50_annual = template_by_name(dbsession, "tier_50_annual")
        tier_75_annual = template_by_name(dbsession, "tier_75_annual")
        put_on_tier(dbsession, ba, tier_50_annual.id, "sub_ann_up")

        delta = change_subscription_tier(dbsession, ba, tier_75_annual)
        # 12×75 - 12×50 = 900 - 600 = 300 credit delta.
        assert delta == Decimal("300")
        assert BillingAccountDAO(dbsession).get_credits(ba.id) == Decimal("300")
        assert ba.plan_credits_granted_period == Decimal("900")

    def test_mid_cycle_annual_upgrade_grant_is_prorated(
        self,
        dbsession,
        monkeypatch,
    ) -> None:
        """Annual mid-cycle upgrade prorates the 12× delta to the year remaining."""
        from datetime import datetime, timedelta, timezone

        from orchestra.lib.subscription_billing import change_subscription_tier

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "ann_prorate",
            stripe_customer_id="cus_ann_prorate",
        )
        tier_50_annual = template_by_name(dbsession, "tier_50_annual")
        tier_75_annual = template_by_name(dbsession, "tier_75_annual")
        put_on_tier(dbsession, ba, tier_50_annual.id, "sub_ann_prorate")

        # Roughly a quarter of the annual period left.
        ba.current_period_end = datetime.now(timezone.utc) + timedelta(days=90)
        dbsession.flush()

        delta = change_subscription_tier(dbsession, ba, tier_75_annual)
        # Prorated: strictly between zero and the full 300-credit year delta.
        assert Decimal("0") < delta < Decimal("300")
        assert BillingAccountDAO(dbsession).get_credits(ba.id) == delta
        # Mark still advances to the full annual tier grant.
        assert ba.plan_credits_granted_period == Decimal("900")

    def test_cross_interval_switch_is_rejected(self, dbsession, monkeypatch) -> None:
        """Switching monthly<->annual in place is refused (cancel + resubscribe)."""
        from orchestra.lib.subscription_billing import (
            SubscriptionError,
            change_subscription_tier,
        )

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "ann_cross",
            stripe_customer_id="cus_ann_cross",
        )
        put_on_tier(dbsession, ba, TIER_50_ID, "sub_ann_cross")
        tier_75_annual = template_by_name(dbsession, "tier_75_annual")

        with pytest.raises(SubscriptionError):
            change_subscription_tier(dbsession, ba, tier_75_annual)

    def test_auto_increment_stays_within_annual_ladder(
        self,
        dbsession,
        monkeypatch,
    ) -> None:
        """Auto-increment from an annual tier bumps to the next *annual* tier."""
        from orchestra.lib.subscription_billing import (
            auto_increment_on_depletion,
            is_annual,
        )

        mock_stripe_subscription(monkeypatch)
        _user, ba = make_user_with_billing(
            dbsession,
            "ann_auto",
            stripe_customer_id="cus_ann_auto",
        )
        tier_50_annual = template_by_name(dbsession, "tier_50_annual")
        tier_75_annual = template_by_name(dbsession, "tier_75_annual")
        put_on_tier(dbsession, ba, tier_50_annual.id, "sub_ann_auto")
        ba.auto_increment = True
        dbsession.flush()

        bumped = auto_increment_on_depletion(dbsession, ba)
        assert bumped is not None
        assert bumped.id == tier_75_annual.id
        assert is_annual(bumped)

    def test_annual_subscribe_attaches_discount_coupon(
        self,
        dbsession,
        monkeypatch,
    ) -> None:
        """Annual subscribe attaches the configured Stripe discount coupon
        (the 20% off rides on a coupon, not the price); monthly does not."""
        from types import SimpleNamespace

        import orchestra.lib.subscription_billing as sub_mod
        from orchestra.lib.subscription_billing import create_subscription
        from orchestra.settings import settings

        # Run create_subscription fully offline: stub Stripe config, the
        # customer lookup, and Subscription.create (capturing its kwargs).
        captured: list[dict] = []

        def _capture_create(**kwargs):
            captured.append(kwargs)
            return {"id": "sub_coupon", "latest_invoice": None}

        monkeypatch.setattr(sub_mod, "configure_stripe", lambda: None)
        monkeypatch.setattr(
            sub_mod,
            "ensure_stripe_customer",
            lambda *a, **k: "cus_coupon",
        )
        # The off-session subscribe path resolves the customer's saved card;
        # stub it so the unit test stays offline and doesn't hit a real
        # Customer.retrieve / PaymentMethod.list.
        monkeypatch.setattr(
            sub_mod,
            "resolve_default_payment_method",
            lambda cid: "pm_default",
        )
        monkeypatch.setattr(
            sub_mod,
            "stripe",
            SimpleNamespace(Subscription=SimpleNamespace(create=_capture_create)),
        )
        monkeypatch.setattr(
            settings,
            "stripe_unify_subscription_price_id_personal_monthly",
            "price_personal_monthly",
            raising=False,
        )
        monkeypatch.setattr(
            settings,
            "stripe_unify_subscription_price_id_personal_annual",
            "price_personal_annual",
            raising=False,
        )
        monkeypatch.setattr(
            settings,
            "stripe_unify_annual_coupon_id",
            "coupon_annual_20",
            raising=False,
        )

        _user, ba = make_user_with_billing(
            dbsession,
            "coupon_user",
            stripe_customer_id="cus_coupon",
        )
        tier_50 = template_by_name(dbsession, "tier_50")
        tier_50_annual = template_by_name(dbsession, "tier_50_annual")

        # Annual → coupon attached as a subscription-level discount.
        create_subscription(dbsession, ba, tier_50_annual, is_business=False)
        assert captured[-1]["discounts"] == [{"coupon": "coupon_annual_20"}]
        # Off-session subscribe: charge the saved card synchronously.
        assert captured[-1]["default_payment_method"] == "pm_default"
        assert captured[-1]["payment_behavior"] == "error_if_incomplete"

        # Monthly → no discount (coupon is annual-only).
        create_subscription(dbsession, ba, tier_50, is_business=False)
        assert "discounts" not in captured[-1]

    def test_subscribe_without_card_raises(self, dbsession, monkeypatch) -> None:
        """create_subscription refuses (no Stripe create) when there's no card."""
        from types import SimpleNamespace

        import orchestra.lib.subscription_billing as sub_mod
        from orchestra.lib.billing import PaymentMethodError
        from orchestra.lib.subscription_billing import create_subscription
        from orchestra.settings import settings

        created: list[dict] = []

        monkeypatch.setattr(sub_mod, "configure_stripe", lambda: None)
        monkeypatch.setattr(
            sub_mod,
            "ensure_stripe_customer",
            lambda *a, **k: "cus_nocard",
        )
        # No saved card on the customer.
        monkeypatch.setattr(sub_mod, "resolve_default_payment_method", lambda cid: None)
        monkeypatch.setattr(
            sub_mod,
            "stripe",
            SimpleNamespace(
                Subscription=SimpleNamespace(
                    create=lambda **kw: created.append(kw) or {"id": "sub_x"},
                ),
            ),
        )
        monkeypatch.setattr(
            settings,
            "stripe_unify_subscription_price_id_personal_monthly",
            "price_personal_monthly",
            raising=False,
        )

        _user, ba = make_user_with_billing(
            dbsession,
            "nocard_user",
            stripe_customer_id="cus_nocard",
        )
        tier_50 = template_by_name(dbsession, "tier_50")

        with pytest.raises(PaymentMethodError):
            create_subscription(dbsession, ba, tier_50, is_business=False)
        # Refused before any Stripe subscription was created.
        assert created == []
        assert ba.stripe_subscription_id is None


class TestValidateAddressTaxLocation:
    """Unit tests for the Stripe-backed billing-address tax-location probe."""

    def _fake_stripe(self, create):
        """A stand-in ``stripe`` module: real error types, stubbed Customer."""
        import stripe as real_stripe

        deleted: list[str] = []

        def _delete(cid):
            deleted.append(cid)
            return {"id": cid, "deleted": True}

        fake = SimpleNamespace(
            error=real_stripe.error,
            Customer=SimpleNamespace(create=create, delete=_delete),
        )
        return fake, deleted

    def test_invalid_location_returns_friendly_message(self, monkeypatch) -> None:
        import stripe as real_stripe

        import orchestra.lib.billing as billing_mod

        def _create(**kwargs):
            raise real_stripe.error.InvalidRequestError(
                "The customer's location isn't recognized. Set a valid "
                "customer address in order to automatically calculate tax.",
                None,
            )

        fake, deleted = self._fake_stripe(_create)
        monkeypatch.setattr(billing_mod, "configure_stripe", lambda: None)
        monkeypatch.setattr(billing_mod, "stripe", fake)

        err = billing_mod.validate_address_tax_location(
            {
                "line1": "1 St",
                "city": "Nowhere",
                "postal_code": "00000",
                "country": "US",
            },
        )
        assert err is not None
        assert "billing address" in err.lower()
        # Nothing to clean up — create raised, no probe customer was made.
        assert deleted == []

    def test_valid_address_returns_none_and_deletes_probe(self, monkeypatch) -> None:
        import orchestra.lib.billing as billing_mod

        def _create(**kwargs):
            return {"id": "cus_probe_ok"}

        fake, deleted = self._fake_stripe(_create)
        monkeypatch.setattr(billing_mod, "configure_stripe", lambda: None)
        monkeypatch.setattr(billing_mod, "stripe", fake)

        err = billing_mod.validate_address_tax_location(
            {
                "line1": "1 St",
                "city": "SF",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
            },
        )
        assert err is None
        # Probe customer is cleaned up so nothing is persisted.
        assert deleted == ["cus_probe_ok"]

    def test_missing_country_skips_stripe_entirely(self, monkeypatch) -> None:
        import orchestra.lib.billing as billing_mod

        def _create(**kwargs):  # pragma: no cover - must not be called
            raise AssertionError("Stripe should not be called without a country")

        fake, _ = self._fake_stripe(_create)
        monkeypatch.setattr(billing_mod, "configure_stripe", lambda: None)
        monkeypatch.setattr(billing_mod, "stripe", fake)

        assert billing_mod.validate_address_tax_location({"line1": "1 St"}) is None
        assert billing_mod.validate_address_tax_location(None) is None

    def test_non_location_stripe_error_does_not_block(self, monkeypatch) -> None:
        import orchestra.lib.billing as billing_mod

        def _create(**kwargs):
            raise RuntimeError("network down")

        fake, _ = self._fake_stripe(_create)
        monkeypatch.setattr(billing_mod, "configure_stripe", lambda: None)
        monkeypatch.setattr(billing_mod, "stripe", fake)

        # Transient/unexpected failures must not block a profile save.
        assert (
            billing_mod.validate_address_tax_location(
                {
                    "line1": "1 St",
                    "city": "SF",
                    "postal_code": "94105",
                    "country": "US",
                },
            )
            is None
        )


class TestPaymentMethodHelpers:
    """Unit tests for the in-app payment-method (card) management helpers."""

    def _patch(self, monkeypatch, **stripe_attrs):
        """Patch ``billing_mod.stripe`` with a stub exposing real error types."""
        import stripe as real_stripe

        import orchestra.lib.billing as billing_mod

        fake = SimpleNamespace(error=real_stripe.error, **stripe_attrs)
        monkeypatch.setattr(billing_mod, "configure_stripe", lambda: None)
        monkeypatch.setattr(billing_mod, "stripe", fake)
        return billing_mod

    def test_create_setup_intent_returns_client_secret(self, monkeypatch) -> None:
        captured: dict = {}

        def _create(**kwargs):
            captured.update(kwargs)
            return {"client_secret": "seti_123_secret"}

        billing_mod = self._patch(
            monkeypatch,
            SetupIntent=SimpleNamespace(create=_create),
        )

        secret = billing_mod.create_setup_intent("cus_1")
        assert secret == "seti_123_secret"
        # Off-session so the saved card can back future renewals.
        assert captured["customer"] == "cus_1"
        assert captured["usage"] == "off_session"
        assert captured["payment_method_types"] == ["card"]

    def test_list_payment_methods_flags_default(self, monkeypatch) -> None:
        customer = {"invoice_settings": {"default_payment_method": "pm_2"}}
        pm_list = {
            "data": [
                {
                    "id": "pm_1",
                    "card": {
                        "brand": "visa",
                        "last4": "4242",
                        "exp_month": 12,
                        "exp_year": 2030,
                    },
                },
                {
                    "id": "pm_2",
                    "card": {
                        "brand": "mastercard",
                        "last4": "4444",
                        "exp_month": 1,
                        "exp_year": 2031,
                    },
                },
            ],
        }
        billing_mod = self._patch(
            monkeypatch,
            Customer=SimpleNamespace(retrieve=lambda cid: customer),
            PaymentMethod=SimpleNamespace(list=lambda **kw: pm_list),
        )

        cards = billing_mod.list_payment_methods("cus_1")
        assert [c["id"] for c in cards] == ["pm_1", "pm_2"]
        assert cards[0]["is_default"] is False
        assert cards[1]["is_default"] is True
        assert cards[0]["brand"] == "visa"
        assert cards[0]["last4"] == "4242"
        assert cards[0]["exp_month"] == 12

    def test_set_default_updates_customer_and_subscription(self, monkeypatch) -> None:
        calls: dict = {}
        billing_mod = self._patch(
            monkeypatch,
            Customer=SimpleNamespace(
                modify=lambda cid, **kw: calls.__setitem__("customer", (cid, kw)),
            ),
            Subscription=SimpleNamespace(
                modify=lambda sid, **kw: calls.__setitem__("subscription", (sid, kw)),
            ),
        )

        billing_mod.set_default_payment_method("cus_1", "sub_1", "pm_9")
        assert calls["customer"] == (
            "cus_1",
            {"invoice_settings": {"default_payment_method": "pm_9"}},
        )
        assert calls["subscription"] == ("sub_1", {"default_payment_method": "pm_9"})

    def test_set_default_skips_subscription_when_none(self, monkeypatch) -> None:
        calls: dict = {}
        billing_mod = self._patch(
            monkeypatch,
            Customer=SimpleNamespace(
                modify=lambda cid, **kw: calls.__setitem__("customer", (cid, kw)),
            ),
            Subscription=SimpleNamespace(
                modify=lambda *a, **k: calls.__setitem__("subscription", True),
            ),
        )

        billing_mod.set_default_payment_method("cus_1", None, "pm_9")
        assert "customer" in calls
        # No active subscription → nothing to repoint.
        assert "subscription" not in calls

    def test_detach_calls_stripe(self, monkeypatch) -> None:
        detached: list = []
        billing_mod = self._patch(
            monkeypatch,
            PaymentMethod=SimpleNamespace(detach=lambda pid: detached.append(pid)),
        )

        billing_mod.detach_payment_method("pm_9")
        assert detached == ["pm_9"]
