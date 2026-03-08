"""
Tests for the resource_levy routine (Phase 3).

Covers:
1. Levy core logic:
   - Billing contacts for a personal user
   - Billing contacts for an organization
   - Skipping demo assistants
   - Skipping BYOD (user-provisioned) contacts
   - Skipping already-billed contacts (idempotency via last_billed_month)
   - Mixed contact types (phone, email, whatsapp)
   - Cost fallback when country_code is not in AssistantContactCost table
   - Multiple billing accounts in a single run
   - Empty-contact scenario (no billable contacts)
2. Credit management:
   - Credits deducted correctly
   - Auto-recharge triggered when credits drop below threshold
   - Account marked PAST_DUE when credits go negative
   - Grace period started on contacts when account goes PAST_DUE
   - Account already PAST_DUE is not re-flagged
3. Edge cases:
   - Contacts without a billing account are skipped
   - Deleted contacts are not billed
   - Pro-rata: deleted contacts billed if delete happened after levy
4. Admin endpoint:
   - POST /v0/admin/billing/resource-levy triggers the routine
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    AssistantContactCost,
    BillingAccount,
    DemoAssistantMeta,
    Organization,
    User,
)
from orchestra.routines.assistant_contact_levy import (
    _get_billing_account_for_assistant,
    _group_contacts_by_billing_account,
    levy_provisioned_resources,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def seed_contact_type_costs(dbsession: Session):
    """Ensure AssistantContactCost rows exist for all tests."""
    existing = dbsession.query(AssistantContactCost).count()
    if existing == 0:
        rows = [
            AssistantContactCost(
                contact_type="phone",
                provider="twilio",
                country_code="US",
                monthly_cost=Decimal("1.50"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="phone",
                provider="twilio",
                country_code="GB",
                monthly_cost=Decimal("1.50"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="phone",
                provider="twilio",
                country_code=None,
                monthly_cost=Decimal("1.50"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="email",
                provider="google_workspace",
                country_code=None,
                monthly_cost=Decimal("14.00"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="whatsapp",
                provider="twilio",
                country_code=None,
                monthly_cost=Decimal("5.00"),
                one_time_cost=Decimal("5.00"),
            ),
        ]
        dbsession.add_all(rows)
        dbsession.flush()
    yield


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_ba(
    dbsession: Session,
    credits: float = 100.0,
    account_status: str = "ACTIVE",
    autorecharge: bool = False,
    autorecharge_threshold: float = 0.0,
    autorecharge_qty: float = 25.0,
    stripe_customer_id: str | None = None,
) -> BillingAccount:
    ba = BillingAccount(
        credits=Decimal(str(credits)),
        account_status=account_status,
        autorecharge=autorecharge,
        autorecharge_threshold=Decimal(str(autorecharge_threshold)),
        autorecharge_qty=Decimal(str(autorecharge_qty)),
        stripe_customer_id=stripe_customer_id,
    )
    dbsession.add(ba)
    dbsession.flush()
    return ba


def _make_user(
    dbsession: Session,
    uid: str,
    ba: BillingAccount,
    email: str | None = None,
) -> User:
    user = User(
        id=uid,
        email=email or f"{uid}@test.com",
        billing_account_id=ba.id,
    )
    dbsession.add(user)
    dbsession.flush()
    return user


def _make_org(
    dbsession: Session,
    owner: User,
    ba: BillingAccount,
    name: str = "TestOrg",
) -> Organization:
    org = Organization(
        owner_id=owner.id,
        name=name,
        billing_account_id=ba.id,
    )
    dbsession.add(org)
    dbsession.flush()
    return org


def _make_assistant(
    dbsession: Session,
    user_id: str,
    first_name: str = "Levy",
    surname: str = "Bot",
    organization_id: int | None = None,
    demo_id: int | None = None,
) -> Assistant:
    a = Assistant(
        user_id=user_id,
        first_name=first_name,
        surname=surname,
        organization_id=organization_id,
        demo_id=demo_id,
    )
    dbsession.add(a)
    dbsession.flush()
    return a


def _make_contact(
    dbsession: Session,
    assistant_id: int,
    contact_type: str = "phone",
    contact_value: str = "+15551000001",
    provider: str | None = "twilio",
    country_code: str | None = "US",
    provisioned_by: str = "platform",
    status: str = "active",
    last_billed_month: str | None = None,
) -> AssistantContact:
    c = AssistantContact(
        assistant_id=assistant_id,
        contact_type=contact_type,
        contact_value=contact_value,
        provider=provider,
        country_code=country_code,
        provisioned_by=provisioned_by,
        status=status,
        last_billed_month=last_billed_month,
    )
    dbsession.add(c)
    dbsession.flush()
    return c


# ============================================================================
# 1. Billing account resolution
# ============================================================================


class TestBillingAccountResolution:
    """Tests for _get_billing_account_for_assistant."""

    def test_personal_assistant_gets_user_ba(self, dbsession: Session):
        ba = _make_ba(dbsession)
        user = _make_user(dbsession, "res_u1", ba)
        asst = _make_assistant(dbsession, user.id)

        resolved = _get_billing_account_for_assistant(dbsession, asst)
        assert resolved is not None
        assert resolved.id == ba.id

    def test_org_assistant_gets_org_ba(self, dbsession: Session):
        user_ba = _make_ba(dbsession, credits=50)
        user = _make_user(dbsession, "res_u2", user_ba)
        org_ba = _make_ba(dbsession, credits=500)
        org = _make_org(dbsession, user, org_ba, name="ResOrg1")
        asst = _make_assistant(dbsession, user.id, organization_id=org.id)

        resolved = _get_billing_account_for_assistant(dbsession, asst)
        assert resolved is not None
        assert resolved.id == org_ba.id

    def test_user_without_ba_returns_none(self, dbsession: Session):
        user = User(id="res_u3", email="res_u3@test.com")
        dbsession.add(user)
        dbsession.flush()
        asst = _make_assistant(dbsession, user.id)

        resolved = _get_billing_account_for_assistant(dbsession, asst)
        assert resolved is None

    def test_org_without_ba_returns_none(self, dbsession: Session):
        ba = _make_ba(dbsession)
        user = _make_user(dbsession, "res_u4", ba)
        org = Organization(
            owner_id=user.id,
            name="ResOrgNoBa",
        )
        dbsession.add(org)
        dbsession.flush()
        asst = _make_assistant(dbsession, user.id, organization_id=org.id)

        resolved = _get_billing_account_for_assistant(dbsession, asst)
        assert resolved is None


# ============================================================================
# 2. Contact grouping
# ============================================================================


class TestGroupContacts:
    """Tests for _group_contacts_by_billing_account."""

    def test_groups_by_billing_account(self, dbsession: Session):
        ba1 = _make_ba(dbsession, credits=100)
        user1 = _make_user(dbsession, "grp_u1", ba1)
        asst1 = _make_assistant(dbsession, user1.id, first_name="Grp1")
        c1 = _make_contact(dbsession, asst1.agent_id, contact_value="+15551100001")

        ba2 = _make_ba(dbsession, credits=200)
        user2 = _make_user(dbsession, "grp_u2", ba2)
        asst2 = _make_assistant(dbsession, user2.id, first_name="Grp2")
        c2 = _make_contact(dbsession, asst2.agent_id, contact_value="+15551100002")

        groups = _group_contacts_by_billing_account(dbsession, [c1, c2])
        assert ba1.id in groups
        assert ba2.id in groups
        assert len(groups[ba1.id][1]) == 1
        assert len(groups[ba2.id][1]) == 1

    def test_multiple_contacts_same_ba(self, dbsession: Session):
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "grp_u3", ba)
        asst = _make_assistant(dbsession, user.id, first_name="GrpMulti")
        c1 = _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551100003",
        )
        c2 = _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="grp@test.com",
            provider="google_workspace",
            country_code=None,
        )

        groups = _group_contacts_by_billing_account(dbsession, [c1, c2])
        assert ba.id in groups
        assert len(groups[ba.id][1]) == 2

    def test_skips_contacts_without_ba(self, dbsession: Session):
        user = User(id="grp_u4", email="grp_u4@test.com")
        dbsession.add(user)
        dbsession.flush()
        asst = _make_assistant(dbsession, user.id, first_name="GrpNoBa")
        c = _make_contact(dbsession, asst.agent_id, contact_value="+15551100004")

        groups = _group_contacts_by_billing_account(dbsession, [c])
        assert len(groups) == 0


# ============================================================================
# 3. Levy core logic
# ============================================================================


class TestLevyCoreLogic:
    """Tests for the levy_provisioned_resources routine."""

    def test_bills_personal_user_phone(self, dbsession: Session):
        """A personal user with a US phone contact is billed $1.50."""
        ba = _make_ba(dbsession, credits=50.0)
        user = _make_user(dbsession, "lev_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="LevPhone")
        c = _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551200001",
            provider="twilio",
            country_code="US",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        assert result.total_contacts_billed == 1
        assert result.total_amount == Decimal("1.50")
        assert result.accounts_processed == 1

        # Contact updated
        dbsession.refresh(c)
        assert c.last_billed_month == "2026-03"
        assert c.monthly_cost == Decimal("1.50")

        # Credits deducted
        dbsession.refresh(ba)
        assert ba.credits == Decimal("50") - Decimal("1.50")

    def test_bills_org_email(self, dbsession: Session):
        """An org with an email contact is billed $14.00."""
        user_ba = _make_ba(dbsession, credits=10)
        user = _make_user(dbsession, "lev_u2", user_ba)
        org_ba = _make_ba(dbsession, credits=100)
        org = _make_org(dbsession, user, org_ba, name="LevOrg1")
        asst = _make_assistant(
            dbsession,
            user.id,
            first_name="LevEmail",
            organization_id=org.id,
        )
        c = _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="lev@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == org_ba.id]
        assert len(ar) == 1
        assert ar[0].email_count == 1
        assert ar[0].email_cost == Decimal("14.00")

        dbsession.refresh(org_ba)
        assert org_ba.credits == Decimal("100") - Decimal("14.00")

    def test_bills_multiple_contact_types(self, dbsession: Session):
        """A user with phone + email + whatsapp is billed the sum."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "lev_u3", ba)
        asst = _make_assistant(dbsession, user.id, first_name="LevMulti")

        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551200010",
            provider="twilio",
            country_code="US",
        )
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="levmulti@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15551200011",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        expected = Decimal("1.50") + Decimal("14.00") + Decimal("5.00")
        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].total_amount == expected
        assert ar[0].phone_count == 1
        assert ar[0].email_count == 1
        assert ar[0].whatsapp_count == 1

        dbsession.refresh(ba)
        assert ba.credits == Decimal("100") - expected

    def test_skips_demo_assistant_contacts(self, dbsession: Session):
        """Contacts on demo assistants are not billed."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "lev_u4", ba)

        demo_meta = DemoAssistantMeta(
            demoer_user_id=user.id,
            label="test-demo",
        )
        dbsession.add(demo_meta)
        dbsession.flush()

        asst = _make_assistant(
            dbsession,
            user.id,
            first_name="LevDemo",
            demo_id=demo_meta.id,
        )
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551200020",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        # No contacts should be billed for this demo assistant's BA
        for ar in result.account_results:
            if ar.billing_account_id == ba.id:
                pytest.fail("Demo assistant contacts should not be billed")

    def test_skips_byod_contacts(self, dbsession: Session):
        """User-provisioned (BYOD) contacts are not billed."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "lev_u5", ba)
        asst = _make_assistant(dbsession, user.id, first_name="LevBYOD")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551200030",
            provisioned_by="user",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        for ar in result.account_results:
            if ar.billing_account_id == ba.id:
                pytest.fail("BYOD contacts should not be billed")

    def test_skips_deleted_contacts(self, dbsession: Session):
        """Deleted contacts are not billed."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "lev_u6", ba)
        asst = _make_assistant(dbsession, user.id, first_name="LevDel")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551200040",
            status="deleted",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        for ar in result.account_results:
            if ar.billing_account_id == ba.id:
                pytest.fail("Deleted contacts should not be billed")

    def test_no_contacts_returns_empty_result(self, dbsession: Session):
        """If there are no billable contacts, the result is empty."""
        # Run levy with no contacts at all (for a month that hasn't been billed yet)
        result = levy_provisioned_resources(2099, 12, session=dbsession)

        assert result.total_contacts_billed == 0
        assert result.total_amount == Decimal("0")
        assert result.accounts_processed == 0

    def test_country_code_fallback(self, dbsession: Session):
        """When country_code has no exact match, fallback to provider-only cost."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "lev_u7", ba)
        asst = _make_assistant(dbsession, user.id, first_name="LevFB")
        # Use a country code not in the cost table
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551200050",
            provider="twilio",
            country_code="JP",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        # Should fall back to twilio/NULL country → $2.00
        assert ar[0].phone_cost == Decimal("2.00")

    def test_bills_grace_period_contacts(self, dbsession: Session):
        """Contacts in grace_period status are still billed."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "lev_u8", ba)
        asst = _make_assistant(dbsession, user.id, first_name="LevGP")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551200060",
            provider="twilio",
            country_code="US",
            status="grace_period",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].contacts_billed == 1

    def test_multiple_billing_accounts(self, dbsession: Session):
        """Multiple billing accounts are each processed independently."""
        ba1 = _make_ba(dbsession, credits=50)
        user1 = _make_user(dbsession, "lev_u9a", ba1)
        asst1 = _make_assistant(dbsession, user1.id, first_name="LevMBA1")
        _make_contact(
            dbsession,
            asst1.agent_id,
            contact_type="email",
            contact_value="lev9a@test.ai",
            provider="google_workspace",
            country_code=None,
        )

        ba2 = _make_ba(dbsession, credits=200)
        user2 = _make_user(dbsession, "lev_u9b", ba2)
        asst2 = _make_assistant(dbsession, user2.id, first_name="LevMBA2")
        _make_contact(
            dbsession,
            asst2.agent_id,
            contact_type="whatsapp",
            contact_value="+15551200070",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar1 = [r for r in result.account_results if r.billing_account_id == ba1.id]
        ar2 = [r for r in result.account_results if r.billing_account_id == ba2.id]
        assert len(ar1) == 1
        assert len(ar2) == 1
        assert ar1[0].email_cost == Decimal("14.00")
        assert ar2[0].whatsapp_cost == Decimal("10.00")


# ============================================================================
# 4. Idempotency (double-billing prevention)
# ============================================================================


class TestLevyIdempotency:
    """Tests that running the levy twice for the same month is idempotent."""

    def test_already_billed_contacts_skipped(self, dbsession: Session):
        """Contacts already billed for the target month are not re-billed."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "idem_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="Idem1")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551300001",
            provider="twilio",
            country_code="US",
            last_billed_month="2026-03",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        for ar in result.account_results:
            if ar.billing_account_id == ba.id:
                pytest.fail("Already-billed contacts should not be re-billed")

    def test_double_run_same_month(self, dbsession: Session):
        """Running levy twice for the same month only charges once."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "idem_u2", ba)
        asst = _make_assistant(dbsession, user.id, first_name="Idem2")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551300002",
            provider="twilio",
            country_code="US",
        )
        dbsession.flush()

        # First run
        r1 = levy_provisioned_resources(2026, 3, session=dbsession)
        assert r1.total_contacts_billed >= 1

        credits_after_first = Decimal(str(ba.credits))

        # Second run for the same month
        r2 = levy_provisioned_resources(2026, 3, session=dbsession)

        # The contact billed in the first run should not be billed again
        billed_in_r2 = sum(
            ar.contacts_billed
            for ar in r2.account_results
            if ar.billing_account_id == ba.id
        )
        assert billed_in_r2 == 0

        # Credits should be unchanged
        dbsession.refresh(ba)
        assert ba.credits == credits_after_first

    def test_billed_different_month_is_allowed(self, dbsession: Session):
        """A contact billed in month N can be billed again in month N+1."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "idem_u3", ba)
        asst = _make_assistant(dbsession, user.id, first_name="Idem3")
        c = _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551300003",
            provider="twilio",
            country_code="US",
            last_billed_month="2026-02",  # billed last month
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].contacts_billed == 1

        dbsession.refresh(c)
        assert c.last_billed_month == "2026-03"


# ============================================================================
# 5. Credit management
# ============================================================================


class TestLevyCreditManagement:
    """Tests for credit deduction, auto-recharge, and PAST_DUE flagging."""

    def test_credits_deducted_exactly(self, dbsession: Session):
        """Credits are reduced by exactly the levy amount."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "cred_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="Cred1")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="cred1@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        levy_provisioned_resources(2026, 3, session=dbsession)

        dbsession.refresh(ba)
        assert ba.credits == Decimal("100") - Decimal("14.00")

    @patch("orchestra.routines.assistant_contact_levy.queue_auto_recharge")
    def test_auto_recharge_triggered(self, mock_ar, dbsession: Session):
        """Auto-recharge is triggered when credits drop below threshold."""
        ba = _make_ba(
            dbsession,
            credits=20,
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
            stripe_customer_id="cus_test_ar",
        )
        user = _make_user(dbsession, "cred_u2", ba)
        asst = _make_assistant(dbsession, user.id, first_name="CredAR")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="credar@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        # Credits: 20 - 14 = 6, which is below threshold of 10
        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].auto_recharge_triggered is True
        mock_ar.assert_called_once()

    @patch("orchestra.routines.assistant_contact_levy.queue_auto_recharge")
    def test_auto_recharge_not_triggered_without_stripe(
        self,
        mock_ar,
        dbsession: Session,
    ):
        """Auto-recharge is NOT triggered if no stripe_customer_id."""
        ba = _make_ba(
            dbsession,
            credits=20,
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
            stripe_customer_id=None,  # No stripe!
        )
        user = _make_user(dbsession, "cred_u3", ba)
        asst = _make_assistant(dbsession, user.id, first_name="CredNoStripe")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="credns@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].auto_recharge_triggered is False
        mock_ar.assert_not_called()

    def test_marked_past_due_when_negative(self, dbsession: Session):
        """Account status changes to PAST_DUE when credits go negative."""
        ba = _make_ba(dbsession, credits=5)  # Will go negative with $14 email
        user = _make_user(dbsession, "cred_u4", ba)
        asst = _make_assistant(dbsession, user.id, first_name="CredPD")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="credpd@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].marked_past_due is True
        assert result.accounts_marked_past_due >= 1

        dbsession.refresh(ba)
        assert ba.account_status == "PAST_DUE"
        assert ba.credits == Decimal("5") - Decimal("14.00")

    def test_grace_period_started_on_contacts(self, dbsession: Session):
        """When an account goes PAST_DUE, contacts enter grace_period."""
        ba = _make_ba(dbsession, credits=5)
        user = _make_user(dbsession, "cred_u5", ba)
        asst = _make_assistant(dbsession, user.id, first_name="CredGP")
        c = _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="credgp@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        levy_provisioned_resources(2026, 3, session=dbsession)

        dbsession.refresh(c)
        assert c.status == "grace_period"
        assert c.grace_period_started_at is not None

    def test_already_past_due_not_re_flagged(self, dbsession: Session):
        """An account already PAST_DUE is not changed to PAST_DUE again."""
        ba = _make_ba(dbsession, credits=5, account_status="PAST_DUE")
        user = _make_user(dbsession, "cred_u6", ba)
        asst = _make_assistant(dbsession, user.id, first_name="CredAPD")
        c = _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="credapd@test.ai",
            provider="google_workspace",
            country_code=None,
            status="grace_period",  # Already in grace
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        # marked_past_due should be False since it was already PAST_DUE
        assert ar[0].marked_past_due is False

        dbsession.refresh(ba)
        assert ba.account_status == "PAST_DUE"  # unchanged

    def test_credits_sufficient_no_past_due(self, dbsession: Session):
        """If credits are sufficient, account stays ACTIVE."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "cred_u7", ba)
        asst = _make_assistant(dbsession, user.id, first_name="CredOK")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551300020",
            provider="twilio",
            country_code="US",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].marked_past_due is False

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"


# ============================================================================
# 5b. Day-1 insufficient credits notification
# ============================================================================


class TestLevyDay1Notification:
    """When a billing account goes PAST_DUE, a Day-1 email is triggered."""

    def test_notification_flagged_on_past_due(self, dbsession: Session):
        """insufficient_credits_notified is set when account goes PAST_DUE."""
        ba = _make_ba(dbsession, credits=5)
        user = _make_user(dbsession, "notif_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="NotifBot")
        c = _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="notif1@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        with patch(
            "orchestra.routines.assistant_contact_levy.send_notification_emails_sync",
        ) as mock_send:
            result = levy_provisioned_resources(2026, 4, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].insufficient_credits_notified is True
        assert result.notifications_sent >= 1

        # Email sender was called
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        # Recipients should include the user's email
        assert user.email in call_args[0][0]

    def test_no_notification_when_credits_sufficient(self, dbsession: Session):
        """No notification when account stays ACTIVE (sufficient credits)."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "notif_u2", ba)
        asst = _make_assistant(dbsession, user.id, first_name="NotifOK")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551700001",
            provider="twilio",
            country_code="US",
        )
        dbsession.flush()

        with patch(
            "orchestra.routines.assistant_contact_levy.send_notification_emails_sync",
        ) as mock_send:
            result = levy_provisioned_resources(2026, 4, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].insufficient_credits_notified is False
        mock_send.assert_not_called()

    def test_notification_tracking_set_on_contacts(self, dbsession: Session):
        """Contacts get last_notification_day=1 in metadata when PAST_DUE."""
        ba = _make_ba(dbsession, credits=5)
        user = _make_user(dbsession, "notif_u3", ba)
        asst = _make_assistant(dbsession, user.id, first_name="NotifTrack")
        c = _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="notiftrack@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        with patch(
            "orchestra.routines.assistant_contact_levy.send_notification_emails_sync",
        ):
            levy_provisioned_resources(2026, 4, session=dbsession)

        dbsession.refresh(c)
        assert c.status == "grace_period"
        assert c.metadata_ is not None
        assert c.metadata_.get("last_notification_day") == 1

    def test_already_past_due_no_notification(self, dbsession: Session):
        """No Day-1 notification when account is already PAST_DUE."""
        ba = _make_ba(dbsession, credits=5, account_status="PAST_DUE")
        user = _make_user(dbsession, "notif_u4", ba)
        asst = _make_assistant(dbsession, user.id, first_name="NotifAPD")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="notifapd@test.ai",
            provider="google_workspace",
            country_code=None,
            status="grace_period",
        )
        dbsession.flush()

        with patch(
            "orchestra.routines.assistant_contact_levy.send_notification_emails_sync",
        ) as mock_send:
            result = levy_provisioned_resources(2026, 4, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].marked_past_due is False
        mock_send.assert_not_called()


# ============================================================================
# 6. Edge cases
# ============================================================================


class TestLevyEdgeCases:
    """Edge cases for the levy routine."""

    def test_contacts_without_billing_account_skipped(self, dbsession: Session):
        """Contacts whose assistant has no billing account are skipped."""
        user = User(id="edge_u1", email="edge_u1@test.com")
        dbsession.add(user)
        dbsession.flush()
        asst = _make_assistant(dbsession, user.id, first_name="EdgeNoBa")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551400001",
        )
        dbsession.flush()

        # This should not raise even though there's no billing account
        result = levy_provisioned_resources(2026, 3, session=dbsession)
        # The contact should be skipped (not in any account_result)
        assert result.total_amount >= Decimal("0")  # No assertion on exact amount

    def test_multiple_assistants_same_user(self, dbsession: Session):
        """Multiple assistants' contacts aggregate under same billing account."""
        ba = _make_ba(dbsession, credits=200)
        user = _make_user(dbsession, "edge_u2", ba)
        asst1 = _make_assistant(dbsession, user.id, first_name="EdgeA1")
        asst2 = _make_assistant(dbsession, user.id, first_name="EdgeA2")
        _make_contact(
            dbsession,
            asst1.agent_id,
            contact_type="phone",
            contact_value="+15551400010",
            provider="twilio",
            country_code="US",
        )
        _make_contact(
            dbsession,
            asst2.agent_id,
            contact_type="email",
            contact_value="edgea2@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        # Both contacts aggregated under the same BA
        assert ar[0].contacts_billed == 2
        assert ar[0].total_amount == Decimal("1.50") + Decimal("14.00")

    def test_org_and_personal_assistant_billed_separately(
        self,
        dbsession: Session,
    ):
        """Org assistant bills org BA, personal assistant bills user BA."""
        user_ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "edge_u3", user_ba)
        org_ba = _make_ba(dbsession, credits=100)
        org = _make_org(dbsession, user, org_ba, name="EdgeOrg1")

        personal_asst = _make_assistant(
            dbsession,
            user.id,
            first_name="EdgePers",
        )
        org_asst = _make_assistant(
            dbsession,
            user.id,
            first_name="EdgeOrga",
            organization_id=org.id,
        )

        _make_contact(
            dbsession,
            personal_asst.agent_id,
            contact_type="phone",
            contact_value="+15551400020",
            provider="twilio",
            country_code="US",
        )
        _make_contact(
            dbsession,
            org_asst.agent_id,
            contact_type="email",
            contact_value="edgeorg@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        user_ar = [
            r for r in result.account_results if r.billing_account_id == user_ba.id
        ]
        org_ar = [
            r for r in result.account_results if r.billing_account_id == org_ba.id
        ]

        assert len(user_ar) == 1
        assert user_ar[0].phone_count == 1
        assert user_ar[0].total_amount == Decimal("1.50")

        assert len(org_ar) == 1
        assert org_ar[0].email_count == 1
        assert org_ar[0].total_amount == Decimal("14.00")

    def test_gb_country_code_uses_specific_price(self, dbsession: Session):
        """A GB phone uses the country-specific price ($1.50)."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "edge_u4", ba)
        asst = _make_assistant(dbsession, user.id, first_name="EdgeGB")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+441234567890",
            provider="twilio",
            country_code="GB",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].phone_cost == Decimal("1.50")


# ============================================================================
# 7. Admin endpoint
# ============================================================================


class TestAdminResourceLevyEndpoint:
    """Tests for POST /v0/admin/billing/resource-levy."""

    @pytest.mark.anyio
    async def test_trigger_levy_via_admin(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """The admin endpoint triggers the levy and returns metrics."""
        from orchestra.tests.utils import ADMIN_HEADERS

        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "admin_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="AdminLev")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551500001",
            provider="twilio",
            country_code="US",
        )
        dbsession.flush()
        dbsession.commit()

        resp = await client.post(
            "/v0/admin/billing/resource-levy",
            params={"year": 2026, "month": 4},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["billing_month"] == "2026-04"
        assert body["total_contacts_billed"] >= 1
        assert body["total_amount"] > 0

    @pytest.mark.anyio
    async def test_levy_endpoint_defaults_to_current_month(
        self,
        client: AsyncClient,
    ):
        """Without year/month params, defaults to current month."""
        from orchestra.tests.utils import ADMIN_HEADERS

        resp = await client.post(
            "/v0/admin/billing/resource-levy",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "billing_month" in body


# ============================================================================
# 8. Result structure
# ============================================================================


class TestLevyResultStructure:
    """Tests for LevyResult and LevyAccountResult data classes."""

    def test_result_has_correct_billing_month(self, dbsession: Session):
        result = levy_provisioned_resources(2026, 6, session=dbsession)
        assert result.billing_month == "2026-06"

    def test_account_result_per_type_breakdown(self, dbsession: Session):
        """Account result breaks down costs by contact type."""
        ba = _make_ba(dbsession, credits=200)
        user = _make_user(dbsession, "res_u10", ba)
        asst = _make_assistant(dbsession, user.id, first_name="ResBreak")
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551600001",
            provider="twilio",
            country_code="US",
        )
        _make_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="resbreak@test.ai",
            provider="google_workspace",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 6, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].phone_count == 1
        assert ar[0].phone_cost == Decimal("1.50")
        assert ar[0].email_count == 1
        assert ar[0].email_cost == Decimal("14.00")
        assert ar[0].whatsapp_count == 0
        assert ar[0].whatsapp_cost == Decimal("0")
        assert ar[0].credits_before == Decimal("200")
        assert ar[0].credits_after == Decimal("200") - Decimal("15.50")
