"""Tests for the ``/admin/metrics/export/*`` fact exports."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO
from orchestra.db.models.enums import (
    RECHARGE_TYPE_AUTO,
    RECHARGE_TYPE_MONTHLY_COMMIT,
    RECHARGE_TYPE_PROMO,
    RechargeStatus,
)
from orchestra.db.models.orchestra_models import (
    Account,
    BillingPlanTemplate,
    CreditGrantLinkClaim,
    CreditTransaction,
    EmailAccount,
    OnboardingStatus,
    OneTimeCreditGrantLink,
    OrganizationMember,
    Recharge,
    ReferralAttribution,
    ReferralCode,
    User,
)
from orchestra.routines.kpi_export import encode_cursor
from orchestra.settings import settings
from orchestra.tests.test_billing.conftest import (
    TIER_75_ID,
    make_assistant,
    make_billing_account,
    make_org,
    make_user_with_billing,
)
from orchestra.tests.utils import ADMIN_HEADERS, create_test_user

EXPORTS = ("users", "payments", "activity", "plans")

# A window no seeded row and no other fixture can land in.
T0 = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None)


async def _walk(
    client: AsyncClient,
    export: str,
    params: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    """Pull every page at ``limit`` rows, checking the envelope on each."""
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        query = {**params, "limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        resp = await client.get(
            f"/v0/admin/metrics/export/{export}",
            params=query,
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"rows", "next_cursor", "generated_at"}
        assert body["generated_at"].endswith("Z")
        assert len(body["rows"]) <= limit
        rows.extend(body["rows"])
        cursor = body["next_cursor"]
        if cursor is None:
            return rows


# ---------------------------------------------------------------------------
# Auth, envelope, cursor hygiene
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("export", EXPORTS)
async def test_non_admin_key_is_refused(client: AsyncClient, export: str) -> None:
    user = await create_test_user(client, "kpi_plain_user@example.com")
    resp = await client.get(
        f"/v0/admin/metrics/export/{export}",
        headers=user["headers"],
    )
    assert resp.status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize("export", EXPORTS)
@pytest.mark.parametrize(
    "cursor",
    ["not-a-cursor", encode_cursor("yesterday", 1)],
)
async def test_garbage_cursor_is_400(
    client: AsyncClient,
    export: str,
    cursor: str,
) -> None:
    resp = await client.get(
        f"/v0/admin/metrics/export/{export}",
        params={"cursor": cursor},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.anyio
@pytest.mark.parametrize("limit", [0, 1001])
async def test_limit_is_bounded(client: AsyncClient, limit: int) -> None:
    resp = await client.get(
        "/v0/admin/metrics/export/users",
        params={"limit": limit},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_users_export_derives_provenance(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    owner_role_id = dbsession.execute(
        text("SELECT id FROM role WHERE name = 'Owner' AND is_system_role = true"),
    ).scalar()
    alice_ba = make_billing_account(dbsession)
    alice = User(
        id="kpi_alice",
        email="kpi_alice@example.com",
        billing_account_id=alice_ba.id,
        created_at=_naive(T0),
        signup_ip="203.0.113.7",
    )
    bob = User(
        id="kpi_bob",
        email="kpi_bob@example.com",
        created_at=_naive(T0 + timedelta(seconds=1)),
    )
    carol = User(
        id="kpi_carol",
        email="kpi_carol@example.com",
        created_at=_naive(T0 + timedelta(seconds=2)),
    )
    dbsession.add_all([alice, bob, carol])
    dbsession.flush()

    # Alice signed in with Google and also set a password: OAuth wins.
    dbsession.add_all(
        [
            Account(
                user_id=alice.id,
                provider="google",
                provider_type="oauth",
                provider_account_id="g-1",
            ),
            EmailAccount(user_id=alice.id, password_hash="x"),
            EmailAccount(user_id=bob.id, password_hash="x"),
        ],
    )
    # Alice owns one org and joined another *earlier*: organization_id is
    # the earliest membership, is_org_owner comes from ownership.
    owned = make_org(
        dbsession,
        alice,
        make_billing_account(dbsession),
        name="kpi owned",
    )
    joined = make_org(
        dbsession,
        bob,
        make_billing_account(dbsession),
        name="kpi joined",
    )
    dbsession.add_all(
        [
            OrganizationMember(
                organization_id=owned.id,
                user_id=alice.id,
                role_id=owner_role_id,
                created_at=_naive(T0 + timedelta(hours=2)),
            ),
            OrganizationMember(
                organization_id=joined.id,
                user_id=alice.id,
                role_id=owner_role_id,
                created_at=_naive(T0 + timedelta(hours=1)),
            ),
            OnboardingStatus(
                user_id=alice.id,
                current_step="completed",
                step_data={
                    "heard_about": "twitter",
                    "heard_about_detail": "a thread",
                    "utm_source": "x",
                    "landing_url": "https://unify.ai/",
                },
            ),
            ReferralCode(code="KPI-REF", referrer_user_id=bob.id, label="launch"),
            ReferralAttribution(
                code="KPI-REF",
                referrer_user_id=bob.id,
                referee_user_id=alice.id,
                status="rewarded",
            ),
        ],
    )
    early_link = OneTimeCreditGrantLink(
        token="kpi-early",
        name="hn-launch",
        expires_at=T0 + timedelta(days=30),
        credit_amount=5,
    )
    late_link = OneTimeCreditGrantLink(
        token="kpi-late",
        name="newsletter",
        expires_at=T0 + timedelta(days=30),
        credit_amount=5,
    )
    dbsession.add_all([early_link, late_link])
    dbsession.flush()
    # The later link is inserted first; the export must pick by claimed_at.
    dbsession.add_all(
        [
            CreditGrantLinkClaim(
                link_id=late_link.id,
                user_id=alice.id,
                claimed_at=T0 + timedelta(days=2),
            ),
            CreditGrantLinkClaim(
                link_id=early_link.id,
                user_id=alice.id,
                claimed_at=T0 + timedelta(days=1),
            ),
        ],
    )
    first_assistant = make_assistant(dbsession, alice.id)
    later_assistant = make_assistant(dbsession, alice.id)
    first_assistant.created_at = _naive(T0 + timedelta(hours=3))
    later_assistant.created_at = _naive(T0 + timedelta(hours=4))
    dbsession.flush()

    window = {"since": "2099-01-01", "until": "2099-01-02"}
    rows = await _walk(client, "users", window, limit=2)
    assert [row["user_id"] for row in rows] == ["kpi_alice", "kpi_bob", "kpi_carol"]

    assert rows[0] == {
        "user_id": "kpi_alice",
        "email": "kpi_alice@example.com",
        "created_at": "2099-01-01T00:00:00Z",
        "auth_provider": "google",
        "billing_account_id": alice_ba.id,
        "organization_id": joined.id,
        "is_org_owner": True,
        "onboarding_step": "completed",
        "heard_about": "twitter",
        "heard_about_detail": "a thread",
        "attribution": {
            "utm_source": "x",
            "utm_medium": None,
            "utm_campaign": None,
            "utm_content": None,
            "utm_term": None,
            "referrer": None,
            "landing_url": "https://unify.ai/",
        },
        "referral_code": "KPI-REF",
        "referral_label": "launch",
        "referral_status": "rewarded",
        "grant_link_name": "hn-launch",
        "first_assistant_created_at": "2099-01-01T03:00:00Z",
        "signup_ip_hash": hashlib.sha256(b"203.0.113.7").hexdigest(),
    }
    assert rows[1] == {
        "user_id": "kpi_bob",
        "email": "kpi_bob@example.com",
        "created_at": "2099-01-01T00:00:01Z",
        "auth_provider": "email",
        "billing_account_id": None,
        "organization_id": None,
        "is_org_owner": True,
        "onboarding_step": None,
        "heard_about": None,
        "heard_about_detail": None,
        "attribution": {
            "utm_source": None,
            "utm_medium": None,
            "utm_campaign": None,
            "utm_content": None,
            "utm_term": None,
            "referrer": None,
            "landing_url": None,
        },
        "referral_code": None,
        "referral_label": None,
        "referral_status": None,
        "grant_link_name": None,
        "first_assistant_created_at": None,
        "signup_ip_hash": None,
    }
    assert rows[2]["auth_provider"] == "unknown"
    assert rows[2]["is_org_owner"] is False

    # The raw address never leaves the database.
    resp = await client.get(
        "/v0/admin/metrics/export/users",
        params=window,
        headers=ADMIN_HEADERS,
    )
    assert "203.0.113.7" not in resp.text

    # since is inclusive, until is exclusive, both on created_at.
    only_alice = await _walk(
        client,
        "users",
        {"since": "2099-01-01T00:00:00Z", "until": "2099-01-01T00:00:01Z"},
        limit=10,
    )
    assert [row["user_id"] for row in only_alice] == ["kpi_alice"]
    from_bob = await _walk(
        client,
        "users",
        {"since": "2099-01-01T00:00:01", "until": "2099-01-02"},
        limit=10,
    )
    assert [row["user_id"] for row in from_bob] == ["kpi_bob", "kpi_carol"]


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_payments_export_resolves_accounts_and_plans(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    payer, payer_ba = make_user_with_billing(dbsession, "kpi_payer")
    owner, _ = make_user_with_billing(dbsession, "kpi_org_owner")
    org_ba = make_billing_account(dbsession)
    org = make_org(dbsession, owner, org_ba, name="kpi payer org")
    orphan_ba = make_billing_account(dbsession)

    tier = BillingPlanAssignmentDAO(dbsession).set_plan(
        billing_account_id=payer_ba.id,
        template_id=TIER_75_ID,
        effective_at=T0 + timedelta(days=10),
    )
    assert tier is not None

    def recharge(**fields: Any) -> Recharge:
        row = Recharge(**fields)
        dbsession.add(row)
        return row

    on_default = recharge(
        at=_naive(T0 + timedelta(days=5)),
        billing_account_id=payer_ba.id,
        quantity=Decimal("10"),
        amount_usd=Decimal("10"),
        type=RECHARGE_TYPE_AUTO,
        status=RechargeStatus.PAID.value,
        stripe_invoice_id="in_kpi_1",
    )
    on_tier = recharge(
        at=_naive(T0 + timedelta(days=15)),
        billing_account_id=payer_ba.id,
        quantity=Decimal("75"),
        amount_usd=Decimal("75"),
        type=RECHARGE_TYPE_AUTO,
        status=RechargeStatus.DISPUTED.value,
    )
    # A metered row names its assignment, which beats the org's own
    # (default) plan that is active at the time.
    org_metered = recharge(
        at=_naive(T0 + timedelta(days=16)),
        billing_account_id=org_ba.id,
        quantity=Decimal("0"),
        amount_usd=Decimal("1200.50"),
        type=RECHARGE_TYPE_MONTHLY_COMMIT,
        status=RechargeStatus.PAID.value,
        plan_id=tier.id,
    )
    orphan = recharge(
        at=_naive(T0 + timedelta(days=17)),
        billing_account_id=orphan_ba.id,
        quantity=Decimal("3"),
        amount_usd=Decimal("3"),
        type=RECHARGE_TYPE_AUTO,
        status=RechargeStatus.PAID.value,
    )
    recharge(
        at=_naive(T0 + timedelta(days=18)),
        billing_account_id=payer_ba.id,
        quantity=Decimal("100"),
        amount_usd=Decimal("0"),
        type=RECHARGE_TYPE_PROMO,
        status=RechargeStatus.PAID.value,
    )
    failed = recharge(
        at=_naive(T0 + timedelta(days=19)),
        billing_account_id=payer_ba.id,
        quantity=Decimal("1"),
        amount_usd=Decimal("1"),
        type=RECHARGE_TYPE_AUTO,
        status=RechargeStatus.FAILED.value,
    )
    dbsession.flush()

    window = {"since": "2099-01-01", "until": "2099-02-01"}
    rows = await _walk(client, "payments", window, limit=2)
    assert [row["recharge_id"] for row in rows] == [
        on_default.id,
        on_tier.id,
        org_metered.id,
        orphan.id,
    ]
    assert rows[0] == {
        "recharge_id": on_default.id,
        "at": "2099-01-06T00:00:00Z",
        "billing_account_id": payer_ba.id,
        "account_kind": "user",
        "user_id": payer.id,
        "organization_id": None,
        "type": "auto",
        "status": "PAID",
        "amount_usd": 10.0,
        "credits": 10.0,
        "stripe_invoice_id": "in_kpi_1",
        "plan_template_name": "default",
    }
    assert rows[1]["status"] == "DISPUTED"
    assert rows[1]["plan_template_name"] == "tier_75"
    assert rows[2]["account_kind"] == "organization"
    assert rows[2]["user_id"] == owner.id
    assert rows[2]["organization_id"] == org.id
    assert rows[2]["amount_usd"] == 1200.5
    assert rows[2]["plan_template_name"] == "tier_75"
    assert rows[3]["account_kind"] == "unknown"
    assert rows[3]["user_id"] is None
    assert rows[3]["organization_id"] is None

    failed_rows = await _walk(
        client,
        "payments",
        {**window, "statuses": "failed"},
        limit=10,
    )
    assert [row["recharge_id"] for row in failed_rows] == [failed.id]

    resp = await client.get(
        "/v0/admin/metrics/export/payments",
        params={"statuses": "PAID,BOGUS"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_activity_export_buckets_billable_debits_by_utc_day(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    user, ba = make_user_with_billing(dbsession, "kpi_active")
    other, other_ba = make_user_with_billing(dbsession, "kpi_other")
    day1 = datetime(2099, 3, 1, tzinfo=timezone.utc)

    def txn(
        at: datetime,
        amount: str,
        category: str,
        assistant_id: int | None = None,
        *,
        user_id: str | None = user.id,
        billing_account_id: int = ba.id,
    ) -> None:
        dbsession.add(
            CreditTransaction(
                billing_account_id=billing_account_id,
                at=at,
                amount=Decimal(amount),
                category=category,
                assistant_id=assistant_id,
                user_id=user_id,
            ),
        )

    txn(day1 + timedelta(hours=23, minutes=30), "-1.5", "llm", 11)
    # 01:00 at UTC+3 is still 22:00 UTC the day before.
    txn(
        datetime(2099, 3, 2, 1, 0, tzinfo=timezone(timedelta(hours=3))),
        "-0.25",
        "media",
        12,
    )
    txn(day1 + timedelta(days=1, minutes=30), "-2", "llm", 11)
    txn(day1 + timedelta(days=1, hours=5), "-0.5", "hire", 12)
    txn(day1 + timedelta(days=1, hours=6), "-0.75", "resources", None)
    txn(day1 + timedelta(days=1, hours=7), "-3", "seat", 11)
    txn(day1 + timedelta(days=1, hours=8), "10", "recharge")
    txn(day1 + timedelta(days=1, hours=9), "-1", "llm", 11, user_id=None)
    txn(
        day1 + timedelta(days=2),
        "-4",
        "llm",
        13,
        user_id=other.id,
        billing_account_id=other_ba.id,
    )
    txn(datetime(2000, 1, 1, tzinfo=timezone.utc), "-9", "llm", 11)
    dbsession.flush()

    rows = await _walk(
        client,
        "activity",
        {"since": "2099-03-01", "until": "2099-03-04"},
        limit=1,
    )
    assert rows == [
        {
            "user_id": user.id,
            "day": "2099-03-01",
            "credits_spent": 1.75,
            "llm_credits": 1.5,
            "hire_credits": 0.0,
            "resources_credits": 0.0,
            "media_credits": 0.25,
            "n_debits": 2,
            "n_assistants": 2,
        },
        {
            "user_id": user.id,
            "day": "2099-03-02",
            "credits_spent": 3.25,
            "llm_credits": 2.0,
            "hire_credits": 0.5,
            "resources_credits": 0.75,
            "media_credits": 0.0,
            "n_debits": 3,
            "n_assistants": 2,
        },
        {
            "user_id": other.id,
            "day": "2099-03-03",
            "credits_spent": 4.0,
            "llm_credits": 4.0,
            "hire_credits": 0.0,
            "resources_credits": 0.0,
            "media_credits": 0.0,
            "n_debits": 1,
            "n_assistants": 1,
        },
    ]

    until_exclusive = await _walk(
        client,
        "activity",
        {"since": "2099-03-02", "until": "2099-03-03"},
        limit=10,
    )
    assert [(row["user_id"], row["day"]) for row in until_exclusive] == [
        (user.id, "2099-03-02"),
    ]

    # No since: the default window reaches back 45 days, not to 2000.
    default_window = await _walk(client, "activity", {}, limit=10)
    assert [row["day"] for row in default_window] == [
        "2099-03-01",
        "2099-03-02",
        "2099-03-03",
    ]


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_plans_export_lists_assignment_history(
    client: AsyncClient,
    dbsession: Session,
) -> None:
    user, ba = make_user_with_billing(dbsession, "kpi_planned")
    owner, _ = make_user_with_billing(dbsession, "kpi_plan_owner")
    org_ba = make_billing_account(dbsession)
    org = make_org(dbsession, owner, org_ba, name="kpi plan org")
    ba.stripe_subscription_id = "sub_kpi"
    user_default_id = ba.plan_assignment_id
    org_default_id = org_ba.plan_assignment_id
    trial_template_id = dbsession.execute(
        select(BillingPlanTemplate.id).where(
            BillingPlanTemplate.name == settings.trial_tier_template_name,
        ),
    ).scalar_one()

    dao = BillingPlanAssignmentDAO(dbsession)
    trial = dao.set_plan(
        billing_account_id=ba.id,
        template_id=trial_template_id,
        effective_at=T0 + timedelta(days=1),
    )
    org_plan = dao.set_plan(
        billing_account_id=org_ba.id,
        template_id=TIER_75_ID,
        effective_at=T0 + timedelta(days=2),
    )
    assert trial is not None and org_plan is not None
    dbsession.flush()

    rows = await _walk(client, "plans", {"since": "2099-01-01"}, limit=1)
    assert [row["assignment_id"] for row in rows] == [
        user_default_id,
        org_default_id,
        trial.id,
        org_plan.id,
    ]

    closed_default = rows[0]
    assert closed_default["billing_account_id"] == ba.id
    assert closed_default["account_kind"] == "user"
    assert closed_default["user_id"] == user.id
    assert closed_default["organization_id"] is None
    assert closed_default["template_name"] == "default"
    assert closed_default["display_name"] == "Default"
    assert closed_default["billing_mode"] == "CREDITS"
    assert closed_default["commit_amount"] is None
    assert closed_default["commit_period"] is None
    assert closed_default["commit_schedule"] is None
    assert closed_default["currency"] == "USD"
    assert closed_default["ended_at"] == "2099-01-02T00:00:00Z"
    assert closed_default["is_trial"] is False
    assert closed_default["account_status"] == "ACTIVE"
    assert closed_default["stripe_subscription_id"] == "sub_kpi"

    assert rows[2]["template_name"] == settings.trial_tier_template_name
    assert rows[2]["is_trial"] is True
    assert rows[2]["started_at"] == "2099-01-02T00:00:00Z"
    assert rows[2]["ended_at"] is None

    assert rows[3] == {
        "assignment_id": org_plan.id,
        "billing_account_id": org_ba.id,
        "account_kind": "organization",
        "user_id": owner.id,
        "organization_id": org.id,
        "template_name": "tier_75",
        "display_name": "$75 / mo",
        "billing_mode": "CREDITS",
        "commit_amount": 75.0,
        "commit_period": "MONTHLY",
        "commit_schedule": "AMORTISED",
        "currency": "USD",
        "started_at": "2099-01-03T00:00:00Z",
        "ended_at": None,
        "is_trial": False,
        "account_status": "ACTIVE",
        "stripe_subscription_id": None,
    }

    # since keys on the latest lifecycle event: rows that neither started
    # nor ended after it drop out.
    moved = await _walk(client, "plans", {"since": "2099-01-02T12:00:00Z"}, limit=10)
    assert [row["assignment_id"] for row in moved] == [org_default_id, org_plan.id]
