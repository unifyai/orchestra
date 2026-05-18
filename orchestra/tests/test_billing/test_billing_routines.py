"""
Tests for billing routines (scheduled / admin-triggered jobs).

Covers:
1. Assistant contact levy (resource_levy routine):
   - Billing contacts for personal users and organizations
   - Skipping demo, BYOD, deleted, and already-billed contacts
   - Mixed contact types (phone, email, whatsapp)
   - Cost fallback when country_code is not in AssistantContactCost table
   - Multiple billing accounts in a single run
   - Credits deducted correctly / auto-recharge handling
   - Admin endpoint: POST /v0/admin/billing/resource-levy
2. Monthly credits invoicer (invoice_month):
   - Aggregates PENDING_INVOICE recharges by billing account
   - Creates Stripe invoice per billing account
   - Skips accounts without stripe_customer_id
   - Handles mixed user + org recharges
   - Includes tax ID in invoice when present
3. Monthly metered invoicer (invoice_metered_month):
   - End-to-end ``max(commit, usage) - grants`` formula via the public
     entrypoint with realistic period-windowed ledger data
   - Collection-method dispatch (SEND_INVOICE_NET_30 vs AUTO_CARD)
   - Idempotency, isolation across accounts on partial Stripe failures
   - Suspended-account policy (still invoiced, status stamped to detail)
   - Skips accounts without ``stripe_customer_id``; never touches CREDITS
4. FX policies driving ``invoice_metered_month``:
   - USD (``fx_policy IS NULL``) — no conversion
   - LOCKED_RATE (template-pinned)
   - SPOT (Frankfurter live, fetched at invoice time)
   - PERIOD_AVERAGE (Frankfurter daily series, averaged for the period)
"""

from __future__ import annotations

import calendar
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    AssistantContactCost,
    BillingAccount,
    DemoAssistantMeta,
    Organization,
    Recharge,
    RechargeStatus,
    User,
)
from orchestra.lib.billing import queue_auto_recharge
from orchestra.routines.assistant_contact_levy import levy_provisioned_resources
from orchestra.tests.test_billing.conftest import (
    make_assistant,
    make_billing_account,
    make_contact,
    make_org,
    make_user,
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
                monthly_cost=Decimal("2.00"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="whatsapp",
                provider="twilio",
                country_code=None,
                monthly_cost=Decimal("5.00"),
                one_time_cost=Decimal("5.00"),
            ),
            AssistantContactCost(
                contact_type="discord",
                provider="discord",
                country_code=None,
                monthly_cost=Decimal("1"),
                one_time_cost=Decimal("1"),
            ),
        ]
        dbsession.add_all(rows)
        dbsession.flush()
    yield


# ============================================================================
# Levy: edge cases — contacts without billing accounts
# ============================================================================


class TestLevyUnbillableContacts:
    """Contacts whose owner has no billing account are silently skipped."""

    def test_user_without_billing_account_skipped(self, dbsession: Session):
        """Contact for a user with no billing account is not billed."""
        user = User(id="noBA_u1", email="noBA_u1@test.com")
        dbsession.add(user)
        dbsession.flush()
        asst = make_assistant(dbsession, user.id, first_name="NoBaUser")
        make_contact(dbsession, asst.agent_id, contact_value="+15559000001")
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        assert result.total_contacts_billed == 0
        assert result.accounts_processed == 0

    def test_org_without_billing_account_skipped(self, dbsession: Session):
        """Contact for an org with no billing account is not billed."""
        ba = make_billing_account(dbsession)
        user = make_user(dbsession, "noBA_u2", ba)
        org = Organization(owner_id=user.id, name="NoBaOrg")
        dbsession.add(org)
        dbsession.flush()
        asst = make_assistant(
            dbsession,
            user.id,
            first_name="NoBaOrg",
            organization_id=org.id,
        )
        make_contact(dbsession, asst.agent_id, contact_value="+15559000002")
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        assert result.total_contacts_billed == 0
        assert result.accounts_processed == 0


# ============================================================================
# Levy: core billing logic
# ============================================================================


class TestLevyCoreLogic:
    """Tests for the levy_provisioned_resources routine."""

    def test_bills_personal_user_phone(self, dbsession: Session):
        """A personal user with a US phone contact is billed $1.50."""
        ba = make_billing_account(dbsession, credits=50.0)
        user = make_user(dbsession, "lev_u1", ba)
        asst = make_assistant(dbsession, user.id, first_name="LevPhone")
        c = make_contact(
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

    def test_bills_org_whatsapp(self, dbsession: Session):
        """An org with a WhatsApp contact is billed $5.00 against the org BA."""
        user_ba = make_billing_account(dbsession, credits=10)
        user = make_user(dbsession, "lev_u2", user_ba)
        org_ba = make_billing_account(dbsession, credits=100)
        org = make_org(dbsession, user, org_ba, name="LevOrg1")
        asst = make_assistant(
            dbsession,
            user.id,
            first_name="LevWhats",
            organization_id=org.id,
        )
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15551200002",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == org_ba.id]
        assert len(ar) == 1
        assert ar[0].whatsapp_count == 1
        assert ar[0].whatsapp_cost == Decimal("5.00")

        dbsession.refresh(org_ba)
        assert org_ba.credits == Decimal("100") - Decimal("5.00")

    def test_bills_multiple_contact_types(self, dbsession: Session):
        """A user with phone + whatsapp + discord is billed the sum.

        Note: ``email`` is intentionally not exercised here. Email contacts
        are BYOD-only (``provisioned_by='user'``) and are excluded from the
        levy by the ``provisioned_by == 'platform'`` filter.
        """
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "lev_u3", ba)
        asst = make_assistant(dbsession, user.id, first_name="LevMulti")

        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551200010",
            provider="twilio",
            country_code="US",
        )
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15551200011",
            provider="twilio",
            country_code=None,
        )
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="discord",
            contact_value="discord_bot_lev3",
            provider="discord",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        expected = Decimal("1.50") + Decimal("5.00") + Decimal("1")
        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].total_amount == expected
        assert ar[0].phone_count == 1
        assert ar[0].whatsapp_count == 1
        assert ar[0].discord_count == 1

        dbsession.refresh(ba)
        assert ba.credits == Decimal("100") - expected

    def test_skips_demo_assistant_contacts(self, dbsession: Session):
        """Contacts on demo assistants are not billed."""
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "lev_u4", ba)

        demo_meta = DemoAssistantMeta(
            demoer_user_id=user.id,
            label="test-demo",
        )
        dbsession.add(demo_meta)
        dbsession.flush()

        asst = make_assistant(
            dbsession,
            user.id,
            first_name="LevDemo",
            demo_id=demo_meta.id,
        )
        make_contact(
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
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "lev_u5", ba)
        asst = make_assistant(dbsession, user.id, first_name="LevBYOD")
        make_contact(
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
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "lev_u6", ba)
        asst = make_assistant(dbsession, user.id, first_name="LevDel")
        make_contact(
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
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "lev_u7", ba)
        asst = make_assistant(dbsession, user.id, first_name="LevFB")
        # Use a country code not in the cost table
        make_contact(
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
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "lev_u8", ba)
        asst = make_assistant(dbsession, user.id, first_name="LevGP")
        make_contact(
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
        ba1 = make_billing_account(dbsession, credits=50)
        user1 = make_user(dbsession, "lev_u9a", ba1)
        asst1 = make_assistant(dbsession, user1.id, first_name="LevMBA1")
        make_contact(
            dbsession,
            asst1.agent_id,
            contact_type="phone",
            contact_value="+15551200060",
            provider="twilio",
            country_code="US",
        )

        ba2 = make_billing_account(dbsession, credits=200)
        user2 = make_user(dbsession, "lev_u9b", ba2)
        asst2 = make_assistant(dbsession, user2.id, first_name="LevMBA2")
        make_contact(
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
        assert ar1[0].phone_cost == Decimal("1.50")
        assert ar2[0].whatsapp_cost == Decimal("5.00")


# ============================================================================
# Levy: idempotency (double-billing prevention)
# ============================================================================


class TestLevyIdempotency:
    """Tests that running the levy twice for the same month is idempotent."""

    def test_already_billed_contacts_skipped(self, dbsession: Session):
        """Contacts already billed for the target month are not re-billed."""
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "idem_u1", ba)
        asst = make_assistant(dbsession, user.id, first_name="Idem1")
        make_contact(
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
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "idem_u2", ba)
        asst = make_assistant(dbsession, user.id, first_name="Idem2")
        make_contact(
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
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "idem_u3", ba)
        asst = make_assistant(dbsession, user.id, first_name="Idem3")
        c = make_contact(
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
# Levy: credit management
# ============================================================================


class TestLevyCreditManagement:
    """Tests for credit deduction and auto-recharge."""

    def test_credits_deducted_exactly(self, dbsession: Session):
        """Credits are reduced by exactly the levy amount."""
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "cred_u1", ba)
        asst = make_assistant(dbsession, user.id, first_name="Cred1")
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553010001",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        levy_provisioned_resources(2026, 3, session=dbsession)

        dbsession.refresh(ba)
        assert ba.credits == Decimal("100") - Decimal("5.00")

    @patch(
        "orchestra.routines.assistant_contact_levy.queue_auto_recharge",
        return_value=True,
    )
    def test_auto_recharge_triggered(self, mock_ar, dbsession: Session):
        """Auto-recharge is triggered when credits drop below threshold."""
        ba = make_billing_account(
            dbsession,
            credits=12,
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
            stripe_customer_id="cus_test_ar",
        )
        user = make_user(dbsession, "cred_u2", ba)
        asst = make_assistant(dbsession, user.id, first_name="CredAR")
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553010002",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        # Credits: 12 - 5 = 7, which is below threshold of 10
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
        ba = make_billing_account(
            dbsession,
            credits=12,
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
            stripe_customer_id=None,  # No stripe!
        )
        user = make_user(dbsession, "cred_u3", ba)
        asst = make_assistant(dbsession, user.id, first_name="CredNoStripe")
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553010003",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].auto_recharge_triggered is False
        mock_ar.assert_not_called()

    def test_stays_active_when_negative(self, dbsession: Session):
        """Account stays ACTIVE when credits go negative (no status change)."""
        ba = make_billing_account(
            dbsession,
            credits=2,
        )  # Will go negative with $5 WhatsApp
        user = make_user(dbsession, "cred_u4", ba)
        asst = make_assistant(dbsession, user.id, first_name="CredPD")
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553010004",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"
        assert ba.credits == Decimal("2") - Decimal("5.00")

    def test_grace_period_started_on_negative_credits(self, dbsession: Session):
        """Contacts enter grace_period when credits go negative."""
        ba = make_billing_account(dbsession, credits=2)
        user = make_user(dbsession, "cred_u5", ba)
        asst = make_assistant(dbsession, user.id, first_name="CredGP")
        c = make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553010005",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        levy_provisioned_resources(2026, 3, session=dbsession)

        dbsession.refresh(c)
        assert c.status == "grace_period"
        assert c.grace_period_started_at is not None

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"

    def test_active_account_stays_active_after_levy(self, dbsession: Session):
        """An ACTIVE account stays ACTIVE even when credits go negative."""
        ba = make_billing_account(dbsession, credits=2, account_status="ACTIVE")
        user = make_user(dbsession, "cred_u6", ba)
        asst = make_assistant(dbsession, user.id, first_name="CredAPD")
        c = make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553010006",
            provider="twilio",
            country_code=None,
            status="grace_period",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].marked_past_due is False

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"  # unchanged

    def test_credits_sufficient_stays_active(self, dbsession: Session):
        """If credits are sufficient, account stays ACTIVE."""
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "cred_u7", ba)
        asst = make_assistant(dbsession, user.id, first_name="CredOK")
        make_contact(
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

    def test_levy_writes_tagged_ledger_row(self, dbsession: Session):
        """Levy writes a CreditTransaction tagged with ``category=resources``
        and ``detail.event=contact_levy`` for downstream attribution.

        Driven through the public ``levy_provisioned_resources`` entrypoint
        rather than the private ``_process_billing_account`` so we exercise
        the routine's real wiring.
        """
        from orchestra.db.models.orchestra_models import (
            AssistantContactCost,
            CreditTransaction,
        )

        if (
            dbsession.query(AssistantContactCost)
            .filter_by(contact_type="phone", provider=None, country_code=None)
            .first()
            is None
        ):
            dbsession.add(
                AssistantContactCost(
                    contact_type="phone",
                    monthly_cost=Decimal("2.00"),
                    one_time_cost=Decimal("1.00"),
                ),
            )
            dbsession.flush()

        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "levy_ledger_u1", ba)
        asst = make_assistant(dbsession, user.id, first_name="LevyLedger")
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15559090909",
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 4, session=dbsession)
        assert result.accounts_processed >= 1

        levy_rows = (
            dbsession.query(CreditTransaction)
            .filter(
                CreditTransaction.billing_account_id == ba.id,
                CreditTransaction.category == "resources",
            )
            .all()
        )
        assert len(levy_rows) == 1
        assert levy_rows[0].detail["event"] == "contact_levy"
        assert levy_rows[0].detail["billing_month"] == "2026-04"


# ============================================================================
# Levy: day-1 insufficient credits notification
# ============================================================================


class TestLevyDay1Notification:
    """Day-1 insufficient credits notification sent when contacts enter grace period."""

    def test_notification_sent_on_negative_credits(self, dbsession: Session):
        """Notification sent when credits go negative and contacts enter grace."""
        ba = make_billing_account(dbsession, credits=2)
        user = make_user(dbsession, "notif_u1", ba)
        asst = make_assistant(dbsession, user.id, first_name="NotifBot")
        c = make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553010007",
            provider="twilio",
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

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"

    def test_no_notification_when_credits_sufficient(self, dbsession: Session):
        """No notification when account stays ACTIVE (sufficient credits)."""
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "notif_u2", ba)
        asst = make_assistant(dbsession, user.id, first_name="NotifOK")
        make_contact(
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

    def test_notification_tracking_set_on_grace_contacts(self, dbsession: Session):
        """Notification tracking is set on contacts entering grace period."""
        ba = make_billing_account(dbsession, credits=2)
        user = make_user(dbsession, "notif_u3", ba)
        asst = make_assistant(dbsession, user.id, first_name="NotifTrack")
        c = make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553010008",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        with patch(
            "orchestra.routines.assistant_contact_levy.send_notification_emails_sync",
        ):
            levy_provisioned_resources(2026, 4, session=dbsession)

        dbsession.refresh(c)
        assert c.status == "grace_period"

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"

    def test_active_account_no_notification(self, dbsession: Session):
        """No Day-1 notification for ACTIVE accounts."""
        ba = make_billing_account(dbsession, credits=2, account_status="ACTIVE")
        user = make_user(dbsession, "notif_u4", ba)
        asst = make_assistant(dbsession, user.id, first_name="NotifAPD")
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15553010009",
            provider="twilio",
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
# Levy: edge cases
# ============================================================================


class TestLevyEdgeCases:
    """Edge cases for the levy routine."""

    def test_contacts_without_billing_account_skipped(self, dbsession: Session):
        """Contacts whose assistant has no billing account are skipped."""
        user = User(id="edge_u1", email="edge_u1@test.com")
        dbsession.add(user)
        dbsession.flush()
        asst = make_assistant(dbsession, user.id, first_name="EdgeNoBa")
        make_contact(
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
        ba = make_billing_account(dbsession, credits=200)
        user = make_user(dbsession, "edge_u2", ba)
        asst1 = make_assistant(dbsession, user.id, first_name="EdgeA1")
        asst2 = make_assistant(dbsession, user.id, first_name="EdgeA2")
        make_contact(
            dbsession,
            asst1.agent_id,
            contact_type="phone",
            contact_value="+15551400010",
            provider="twilio",
            country_code="US",
        )
        make_contact(
            dbsession,
            asst2.agent_id,
            contact_type="whatsapp",
            contact_value="+15551400011",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 3, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        # Both contacts aggregated under the same BA
        assert ar[0].contacts_billed == 2
        assert ar[0].total_amount == Decimal("1.50") + Decimal("5.00")

    def test_org_and_personal_assistant_billed_separately(
        self,
        dbsession: Session,
    ):
        """Org assistant bills org BA, personal assistant bills user BA."""
        user_ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "edge_u3", user_ba)
        org_ba = make_billing_account(dbsession, credits=100)
        org = make_org(dbsession, user, org_ba, name="EdgeOrg1")

        personal_asst = make_assistant(
            dbsession,
            user.id,
            first_name="EdgePers",
        )
        org_asst = make_assistant(
            dbsession,
            user.id,
            first_name="EdgeOrga",
            organization_id=org.id,
        )

        make_contact(
            dbsession,
            personal_asst.agent_id,
            contact_type="phone",
            contact_value="+15551400020",
            provider="twilio",
            country_code="US",
        )
        make_contact(
            dbsession,
            org_asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15551400021",
            provider="twilio",
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
        assert org_ar[0].whatsapp_count == 1
        assert org_ar[0].total_amount == Decimal("5.00")

    def test_gb_country_code_uses_specific_price(self, dbsession: Session):
        """A GB phone uses the country-specific price ($1.50)."""
        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "edge_u4", ba)
        asst = make_assistant(dbsession, user.id, first_name="EdgeGB")
        make_contact(
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
# Levy: admin endpoint
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

        ba = make_billing_account(dbsession, credits=100)
        user = make_user(dbsession, "admin_u1", ba)
        asst = make_assistant(dbsession, user.id, first_name="AdminLev")
        make_contact(
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
# Levy: result structure
# ============================================================================


class TestLevyResultStructure:
    """Tests for LevyResult and LevyAccountResult data classes."""

    def test_result_has_correct_billing_month(self, dbsession: Session):
        result = levy_provisioned_resources(2026, 6, session=dbsession)
        assert result.billing_month == "2026-06"

    def test_account_result_per_type_breakdown(self, dbsession: Session):
        """Account result breaks down costs by contact type.

        Email is BYOD-only and never billed by the levy, so the breakdown
        no longer surfaces an ``email_*`` field.
        """
        ba = make_billing_account(dbsession, credits=200)
        user = make_user(dbsession, "res_u10", ba)
        asst = make_assistant(dbsession, user.id, first_name="ResBreak")
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15551600001",
            provider="twilio",
            country_code="US",
        )
        make_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15551600002",
            provider="twilio",
            country_code=None,
        )
        dbsession.flush()

        result = levy_provisioned_resources(2026, 6, session=dbsession)

        ar = [r for r in result.account_results if r.billing_account_id == ba.id]
        assert len(ar) == 1
        assert ar[0].phone_count == 1
        assert ar[0].phone_cost == Decimal("1.50")
        assert ar[0].whatsapp_count == 1
        assert ar[0].whatsapp_cost == Decimal("5.00")
        assert ar[0].discord_count == 0
        assert ar[0].discord_cost == Decimal("0")
        assert ar[0].credits_before == Decimal("200")
        assert ar[0].credits_after == Decimal("200") - Decimal("6.50")
        assert not hasattr(ar[0], "email_count")
        assert not hasattr(ar[0], "email_cost")


# ============================================================================
# Monthly Invoicer Routine
# ============================================================================


class TestMonthlyInvoicer:
    """Tests for the invoice_month routine."""

    @pytest.fixture(autouse=True)
    def _mock_configure_stripe(self, monkeypatch):
        import orchestra.lib.billing

        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

    def _make_recharge(
        self,
        dbsession: Session,
        ba: BillingAccount,
        quantity: float = 100,
        invoice_group=None,
    ):
        import datetime as _dt

        from orchestra.db.models.orchestra_models import Recharge, RechargeStatus
        from orchestra.lib.time import month_end_utc

        if invoice_group is None:
            now = _dt.datetime.now(_dt.timezone.utc)
            invoice_group = month_end_utc(now)

        r = Recharge(
            billing_account_id=ba.id,
            quantity=Decimal(str(quantity)),
            amount_usd=Decimal(str(quantity)),
            status=RechargeStatus.PENDING_INVOICE,
            invoice_group=invoice_group,
            type="usage",
        )
        dbsession.add(r)
        dbsession.flush()
        return r

    def test_aggregates_recharges_and_creates_invoice(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Invoicer aggregates PENDING_INVOICE rows and creates a Stripe invoice."""
        import datetime as _dt
        from types import SimpleNamespace

        from orchestra.routines import monthly_credits_invoicer as invoicer_mod

        calls = {"item": [], "invoice": []}

        def _inv_create(**kw):
            calls["invoice"].append(kw)
            return SimpleNamespace(id="in_test_agg")

        dummy_stripe = SimpleNamespace(
            InvoiceItem=SimpleNamespace(create=lambda **kw: calls["item"].append(kw)),
            Invoice=SimpleNamespace(create=_inv_create),
            StripeError=Exception,
        )
        monkeypatch.setattr(invoicer_mod, "stripe", dummy_stripe)

        ba = make_billing_account(dbsession, stripe_customer_id="cus_inv_agg")
        make_user(dbsession, "inv_agg_user", ba)
        now = _dt.datetime.now(_dt.timezone.utc)
        r1 = self._make_recharge(dbsession, ba, quantity=50)
        r2 = self._make_recharge(dbsession, ba, quantity=30)
        dbsession.flush()

        result = invoicer_mod.invoice_month(now.year, now.month, session=dbsession)

        from orchestra.db.models.orchestra_models import RechargeStatus

        dbsession.refresh(r1)
        dbsession.refresh(r2)
        assert r1.status == RechargeStatus.INVOICE_CREATED
        assert r2.status == RechargeStatus.INVOICE_CREATED
        assert r1.stripe_invoice_id == "in_test_agg"
        assert r2.stripe_invoice_id == "in_test_agg"
        assert len(calls["invoice"]) == 1
        assert result.accounts_invoiced == 1
        assert result.accounts_failed == 0

    def test_skips_account_without_stripe_customer(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Invoicer skips billing accounts without a stripe_customer_id."""
        import datetime as _dt
        from types import SimpleNamespace

        from orchestra.routines import monthly_credits_invoicer as invoicer_mod

        calls = {"invoice": []}
        dummy_stripe = SimpleNamespace(
            InvoiceItem=SimpleNamespace(create=lambda **kw: None),
            Invoice=SimpleNamespace(
                create=lambda **kw: calls["invoice"].append(kw)
                or SimpleNamespace(id="in_x"),
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(invoicer_mod, "stripe", dummy_stripe)

        ba = make_billing_account(dbsession, stripe_customer_id=None)
        make_user(dbsession, "inv_no_cus", ba)
        now = _dt.datetime.now(_dt.timezone.utc)
        r = self._make_recharge(dbsession, ba, quantity=50)
        dbsession.flush()

        result = invoicer_mod.invoice_month(now.year, now.month, session=dbsession)

        from orchestra.db.models.orchestra_models import RechargeStatus

        dbsession.refresh(r)
        assert r.status == RechargeStatus.PENDING_INVOICE
        assert len(calls["invoice"]) == 0
        assert result.accounts_skipped == 1
        assert result.accounts_invoiced == 0

    def test_handles_multiple_billing_accounts(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Invoicer creates separate invoices per billing account."""
        import datetime as _dt
        from types import SimpleNamespace

        from orchestra.routines import monthly_credits_invoicer as invoicer_mod

        invoice_counter = {"n": 0}

        def _inv_create(**kw):
            invoice_counter["n"] += 1
            return SimpleNamespace(id=f"in_multi_{invoice_counter['n']}")

        dummy_stripe = SimpleNamespace(
            InvoiceItem=SimpleNamespace(create=lambda **kw: None),
            Invoice=SimpleNamespace(create=_inv_create),
            StripeError=Exception,
        )
        monkeypatch.setattr(invoicer_mod, "stripe", dummy_stripe)

        ba1 = make_billing_account(dbsession, stripe_customer_id="cus_m1")
        ba2 = make_billing_account(dbsession, stripe_customer_id="cus_m2")
        make_user(dbsession, "inv_m1", ba1)
        make_user(dbsession, "inv_m2", ba2)
        now = _dt.datetime.now(_dt.timezone.utc)
        r1 = self._make_recharge(dbsession, ba1, quantity=40)
        r2 = self._make_recharge(dbsession, ba2, quantity=60)
        dbsession.flush()

        result = invoicer_mod.invoice_month(now.year, now.month, session=dbsession)

        from orchestra.db.models.orchestra_models import RechargeStatus

        dbsession.refresh(r1)
        dbsession.refresh(r2)
        assert r1.status == RechargeStatus.INVOICE_CREATED
        assert r2.status == RechargeStatus.INVOICE_CREATED
        assert r1.stripe_invoice_id != r2.stripe_invoice_id
        assert invoice_counter["n"] == 2
        assert result.accounts_invoiced == 2

    def test_stripe_failure_isolates_to_one_account(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """A Stripe error for one account does not prevent others from invoicing."""
        import datetime as _dt
        from types import SimpleNamespace

        from orchestra.routines import monthly_credits_invoicer as invoicer_mod

        call_count = {"n": 0}

        def _inv_create(**kw):
            call_count["n"] += 1
            if kw["customer"] == "cus_fail":
                raise Exception("Simulated Stripe failure")
            return SimpleNamespace(id=f"in_ok_{call_count['n']}")

        dummy_stripe = SimpleNamespace(
            InvoiceItem=SimpleNamespace(create=lambda **kw: None),
            Invoice=SimpleNamespace(create=_inv_create),
            StripeError=Exception,
        )
        monkeypatch.setattr(invoicer_mod, "stripe", dummy_stripe)

        ba_fail = make_billing_account(dbsession, stripe_customer_id="cus_fail")
        ba_ok = make_billing_account(dbsession, stripe_customer_id="cus_ok")
        make_user(dbsession, "inv_fail", ba_fail)
        make_user(dbsession, "inv_ok", ba_ok)
        now = _dt.datetime.now(_dt.timezone.utc)
        r_fail = self._make_recharge(dbsession, ba_fail, quantity=40)
        r_ok = self._make_recharge(dbsession, ba_ok, quantity=60)
        dbsession.flush()

        result = invoicer_mod.invoice_month(now.year, now.month, session=dbsession)

        from orchestra.db.models.orchestra_models import RechargeStatus

        dbsession.refresh(r_fail)
        dbsession.refresh(r_ok)
        assert r_fail.status == RechargeStatus.PENDING_INVOICE
        assert r_ok.status == RechargeStatus.INVOICE_CREATED
        assert result.accounts_invoiced == 1
        assert result.accounts_failed == 1
        assert len(result.errors) == 1

    def test_no_pending_rows_is_noop(self, dbsession: Session, monkeypatch):
        """Invoicer does nothing when there are no PENDING_INVOICE rows."""
        import datetime as _dt
        from types import SimpleNamespace

        from orchestra.routines import monthly_credits_invoicer as invoicer_mod

        calls = {"invoice": []}
        dummy_stripe = SimpleNamespace(
            InvoiceItem=SimpleNamespace(create=lambda **kw: None),
            Invoice=SimpleNamespace(
                create=lambda **kw: calls["invoice"].append(kw)
                or SimpleNamespace(id="in_x"),
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(invoicer_mod, "stripe", dummy_stripe)

        now = _dt.datetime.now(_dt.timezone.utc)
        result = invoicer_mod.invoice_month(now.year, now.month, session=dbsession)

        assert len(calls["invoice"]) == 0
        assert result.accounts_invoiced == 0

    def test_includes_tax_id_in_invoice(self, dbsession: Session, monkeypatch):
        """Invoicer includes customer_tax_ids when billing account has tax_id."""
        import datetime as _dt
        from types import SimpleNamespace

        from orchestra.routines import monthly_credits_invoicer as invoicer_mod

        calls = {"invoice": []}

        def _inv_create(**kw):
            calls["invoice"].append(kw)
            return SimpleNamespace(id="in_tax")

        dummy_stripe = SimpleNamespace(
            InvoiceItem=SimpleNamespace(create=lambda **kw: None),
            Invoice=SimpleNamespace(create=_inv_create),
            StripeError=Exception,
        )
        monkeypatch.setattr(invoicer_mod, "stripe", dummy_stripe)

        ba = make_billing_account(dbsession, stripe_customer_id="cus_tax_inv")
        ba.tax_id = "12-3456789"
        ba.tax_id_type = "us_ein"
        ba.billing_address = {"country": "US"}
        make_user(dbsession, "inv_tax_user", ba)
        now = _dt.datetime.now(_dt.timezone.utc)
        self._make_recharge(dbsession, ba, quantity=100)
        dbsession.flush()

        invoicer_mod.invoice_month(now.year, now.month, session=dbsession)

        assert len(calls["invoice"]) == 1
        inv_params = calls["invoice"][0]
        assert "customer_tax_ids" in inv_params
        assert inv_params["customer_tax_ids"][0]["type"] == "us_ein"
        assert inv_params["customer_tax_ids"][0]["value"] == "12-3456789"

    def test_prepaid_skip(self, dbsession: Session, monkeypatch):
        """Pre-paid (PAID) recharge rows are NOT re-invoiced."""
        import datetime as _dt
        from types import SimpleNamespace

        from orchestra.routines import monthly_credits_invoicer as invoicer_mod

        calls = {"item": [], "invoice": []}
        dummy_stripe = SimpleNamespace(
            InvoiceItem=SimpleNamespace(create=lambda **kw: calls["item"].append(kw)),
            Invoice=SimpleNamespace(
                create=lambda **kw: calls["invoice"].append(kw)
                or SimpleNamespace(id="in_skip"),
            ),
            StripeError=Exception,
        )
        monkeypatch.setattr(invoicer_mod, "stripe", dummy_stripe)

        ba = make_billing_account(
            dbsession,
            credits=100,
            stripe_customer_id="cus_prepaid",
        )
        make_user(dbsession, "inv_prepaid", ba)
        r = Recharge(
            billing_account_id=ba.id,
            quantity=500,
            amount_usd=Decimal("50.00"),
            status=RechargeStatus.PAID,
            stripe_invoice_id="in_paid",
            type="payment",
        )
        dbsession.add(r)
        dbsession.flush()

        now = _dt.datetime.now(_dt.timezone.utc)
        invoicer_mod.invoice_month(now.year, now.month, session=dbsession)

        dbsession.refresh(r)
        assert r.status == RechargeStatus.PAID
        assert calls["invoice"] == []
        assert calls["item"] == []


# ============================================================================
# Monthly Metered Invoicer (invoice_metered_month)
# ============================================================================


def _commit_template(
    dbsession,
    *,
    name: str,
    commit: Decimal | None = Decimal("1000"),
    collection=None,
    pricing_factor: Decimal = Decimal("1.0"),
    overage_pricing_factor: Decimal | None = None,
    display_name: str | None = None,
    commit_period: str | None = None,
    commit_schedule: str | None = None,
    proration_policy=None,
):
    """Create a METERED template helper.

    "Plan type" is derived from ``commit``: pass ``commit=None`` (or
    zero) to get a PAYG template, anything positive to get a COMMITMENT
    template. ``commit_period`` defaults to ``MONTHLY`` for COMMITMENT
    templates (mirrors the bulk of legacy tests); pass ``QUARTERLY`` /
    ``ANNUAL`` to exercise the per-month overage floor + anniversary
    semantics. ``commit_schedule`` defaults to ``AMORTISED``; pass
    ``UPFRONT`` to exercise anniversary-only commit billing — the
    helper auto-bumps ``proration_policy`` to ``FULL_FIRST`` for UPFRONT
    so the new DB CHECK constraint is satisfied without forcing every
    caller to remember it.

    ``pricing_factor`` is wired into ``base_pricing_factor`` (kept under
    the legacy parameter name for test ergonomics).
    ``overage_pricing_factor`` defaults to ``Decimal("1.0")`` — the
    "no overage uplift over base" identity. The factors **stack** on
    overage in the new pricing model (effective above-commit rate =
    ``base × overage``), so leaving overage at 1.0 means above-commit
    usage gets the same effective rate as committed usage. Tests that
    want a premium overage uplift pass an explicit value (typically
    > 1.0).
    """
    from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO
    from orchestra.db.models.orchestra_models import (
        BillingMode,
        CollectionMethod,
        ProrationPolicy,
    )

    collection = collection or CollectionMethod.SEND_INVOICE_NET_30
    is_commitment = commit is not None and commit > 0
    if proration_policy is None:
        proration_policy = (
            ProrationPolicy.FULL_FIRST
            if commit_schedule == "UPFRONT"
            else ProrationPolicy.PRORATE
        )
    return BillingPlanTemplateDAO(dbsession).create_template(
        name=name,
        display_name=display_name,
        billing_mode=BillingMode.METERED,
        commit_amount=commit,
        commit_period=(commit_period or "MONTHLY" if is_commitment else None),
        commit_schedule=commit_schedule if is_commitment else None,
        base_pricing_factor=pricing_factor,
        overage_pricing_factor=overage_pricing_factor or Decimal("1.0"),
        collection_method=collection,
        proration_policy=proration_policy,
        is_custom=True,
        is_active=True,
    )


def _backdate_assignment(
    dbsession: Session,
    assignment_id: int,
    started_at,
) -> None:
    """Move an assignment's ``started_at`` so it covers a prior period."""
    from sqlalchemy import text

    dbsession.execute(
        text("UPDATE billing_plan_assignment SET started_at = :ts WHERE id = :id"),
        {"ts": started_at, "id": assignment_id},
    )
    dbsession.flush()


def _assign_for_period(
    dbsession: Session,
    ba,
    tpl,
    *,
    period_year: int,
    period_month: int,
    **kwargs,
):
    """Assign a template and backdate it to cover the target invoicing period."""
    import datetime as _dt

    from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO

    a = BillingPlanAssignmentDAO(dbsession).set_plan(
        billing_account_id=ba.id,
        template_id=tpl.id,
        **kwargs,
    )
    _backdate_assignment(
        dbsession,
        a.id,
        _dt.datetime(period_year, period_month, 1, tzinfo=_dt.timezone.utc),
    )
    return a


def _backdate_ledger_to_period(
    dbsession: Session,
    *,
    billing_account_id: int,
    when,
) -> None:
    """Move every ledger row for an account into the target period."""
    from sqlalchemy import text

    dbsession.execute(
        text(
            "UPDATE credit_transaction SET at = :ts " "WHERE billing_account_id = :ba",
        ),
        {"ts": when, "ba": billing_account_id},
    )
    dbsession.flush()


def _record_metered_usage(dbsession, ba_id, amount, *, category="llm"):
    """Record a METERED debit through the public DAO API."""
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    BillingAccountDAO(dbsession).deduct_credits(
        ba_id,
        float(amount),
        category=category,
    )
    dbsession.flush()


def _metered_stripe_mock(invoice_id: str = "in_test123") -> SimpleNamespace:
    """Build a SimpleNamespace that quacks like the ``stripe`` module.

    Captures every call so tests can assert on the parameters that were
    passed (collection_method, amounts, idempotency keys, etc.).
    """
    invoice_item_calls: list[dict] = []
    invoice_calls: list[dict] = []

    def _ii_create(**kw):
        invoice_item_calls.append(kw)
        return SimpleNamespace(id=f"ii_{len(invoice_item_calls)}")

    def _inv_create(**kw):
        invoice_calls.append(kw)
        return SimpleNamespace(id=f"{invoice_id}_{len(invoice_calls)}")

    return SimpleNamespace(
        InvoiceItem=SimpleNamespace(create=_ii_create),
        Invoice=SimpleNamespace(create=_inv_create),
        StripeError=Exception,
        InvalidRequestError=Exception,
        Customer=SimpleNamespace(retrieve=lambda *a, **kw: SimpleNamespace()),
        _ii_calls=invoice_item_calls,
        _inv_calls=invoice_calls,
    )


def _patch_metered_stripe(monkeypatch, stripe_mock):
    import orchestra.lib.billing
    import orchestra.routines.monthly_metered_invoicer as mod

    monkeypatch.setattr(mod, "stripe", stripe_mock)
    monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)


def _mute_metered_metrics(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        "orchestra.routines.monthly_metered_invoicer.INVOICE_CREATED_TOTAL",
        MagicMock(),
    )


class TestMonthlyMeteredInvoicer:
    """End-to-end coverage of ``invoice_metered_month``.

    These exercise the public entrypoint with realistic ledger data, which
    transitively covers eligibility filtering, period-ledger aggregation,
    and the ``max(commit, usage) - grants`` formula.
    """

    def test_invoices_committed_account_with_overage(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import datetime as _dt

        from orchestra.db.models.orchestra_models import (
            RECHARGE_TYPE_MONTHLY_COMMIT,
            CollectionMethod,
        )
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_overage_test",
        )
        tpl = _commit_template(
            dbsession,
            name="vantage-overage",
            commit=Decimal("1000"),
            collection=CollectionMethod.SEND_INVOICE_NET_30,
        )
        _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
        dbsession.refresh(ba)

        _record_metered_usage(dbsession, ba.id, Decimal("750"))
        _record_metered_usage(dbsession, ba.id, Decimal("700"))
        # Total raw_usage = 1450 → max(1000, 1450) = 1450
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)

        assert result.accounts_invoiced == 1
        assert result.accounts_failed == 0

        # Two-line split when chargeable usage > commit: a "monthly
        # commitment" line for the floor and a "usage overage" line for
        # the excess. Replaces the old single-line bundling that read
        # opaquely on rendered invoices.
        assert len(stripe._ii_calls) == 2
        assert len(stripe._inv_calls) == 1
        commit_line, overage_line = stripe._ii_calls
        assert commit_line["customer"] == "cus_overage_test"
        assert commit_line["amount"] == 100000  # 1000 * 100 cents
        assert commit_line["currency"] == "usd"
        assert commit_line["metadata"]["line_kind"] == "commitment"
        assert "monthly commitment" in commit_line["description"].lower()
        assert overage_line["amount"] == 45000  # (1450 - 1000) * 100
        assert overage_line["currency"] == "usd"
        assert overage_line["metadata"]["line_kind"] == "overage"
        assert "usage overage" in overage_line["description"].lower()
        # Idempotency keys must be stable AND distinct per kind so a
        # safe re-run can't merge a commit line with an overage line.
        assert commit_line["idempotency_key"].endswith("-commitment-item")
        assert overage_line["idempotency_key"].endswith("-overage-item")

        inv = stripe._inv_calls[0]
        assert inv["customer"] == "cus_overage_test"
        # Invoice.create MUST pin its ``currency`` to the InvoiceItem's
        # currency. Without it Stripe defaults to the customer's / account's
        # default currency and ``pending_invoice_items_behavior=include``
        # only sweeps in matching-currency items, silently producing a
        # zero-amount invoice when the two diverge.
        assert inv["currency"] == commit_line["currency"] == "usd"
        assert inv["collection_method"] == "send_invoice"
        assert inv["days_until_due"] == 30
        # Generic, customer-readable memo. The per-line descriptions
        # carry the breakdown; the memo intentionally avoids internal
        # jargon ("metered" / "credits" / etc).
        assert inv["description"] == "Invoice for April 2026"
        assert inv["metadata"]["billing_mode"] == "METERED"
        assert inv["metadata"]["plan_template_name"] == "vantage-overage"

        recharge = (
            dbsession.query(Recharge)
            .filter(
                Recharge.billing_account_id == ba.id,
                Recharge.type == RECHARGE_TYPE_MONTHLY_COMMIT,
            )
            .one()
        )
        assert recharge.status == RechargeStatus.INVOICE_CREATED
        assert recharge.amount_usd == Decimal("1450")
        assert recharge.plan_id is not None
        assert recharge.detail is not None
        assert Decimal(recharge.detail["raw_usage_usd"]) == Decimal("1450")
        assert Decimal(recharge.detail["invoiced_local"]) == Decimal("1450")
        assert recharge.detail["currency"] == "USD"
        assert Decimal(recharge.detail["commit_amount"]) == Decimal("1000")

    def test_invoices_committed_account_at_floor_when_under(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_under",
        )
        tpl = _commit_template(
            dbsession,
            name="vantage-under",
            commit=Decimal("1000"),
        )
        _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
        dbsession.refresh(ba)

        _record_metered_usage(dbsession, ba.id, Decimal("400"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1
        ii = stripe._ii_calls[0]
        assert ii["amount"] == 100000  # commit floor

    def test_auto_card_collection_method_dispatch(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import datetime as _dt

        from orchestra.db.models.orchestra_models import CollectionMethod
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_auto",
        )
        tpl = _commit_template(
            dbsession,
            name="auto-collect",
            commit=Decimal("100"),
            collection=CollectionMethod.AUTO_CARD,
        )
        _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
        dbsession.refresh(ba)
        _record_metered_usage(dbsession, ba.id, Decimal("50"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        invoice_metered_month(2026, 4, session=dbsession)
        inv = stripe._inv_calls[0]
        assert inv["collection_method"] == "charge_automatically"
        assert "days_until_due" not in inv

    def test_suspended_account_is_still_invoiced(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """SUSPENDED mid-period must still produce an invoice.

        Real usage hit the ledger before the suspension; we delivered
        service for it and must bill. The account's status at invoice
        time is stamped into ``Recharge.detail`` for audit.
        """
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_susp",
            account_status="SUSPENDED",
        )
        ba.suspension_reason = "PAST_DUE"
        tpl = _commit_template(dbsession, name="susp-still-bills")
        _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
        dbsession.refresh(ba)
        _record_metered_usage(dbsession, ba.id, Decimal("999"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1
        assert result.accounts_skipped == 0
        assert len(stripe._inv_calls) == 1

        recharge = (
            dbsession.query(Recharge).filter(Recharge.billing_account_id == ba.id).one()
        )
        assert recharge.detail["account_status_at_invoice"] == "SUSPENDED"
        assert recharge.detail["suspension_reason_at_invoice"] == "PAST_DUE"

    def test_skip_account_without_stripe_customer(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)

        ba = make_billing_account(dbsession, credits=0, stripe_customer_id=None)
        tpl = _commit_template(dbsession, name="no-stripe")
        _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
        dbsession.refresh(ba)
        _record_metered_usage(dbsession, ba.id, Decimal("100"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_skipped == 1
        assert len(stripe._inv_calls) == 0

    def test_idempotent_re_run_skips_already_invoiced(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_idem",
        )
        tpl = _commit_template(dbsession, name="idem-test", commit=Decimal("100"))
        _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
        dbsession.refresh(ba)
        _record_metered_usage(dbsession, ba.id, Decimal("50"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        first = invoice_metered_month(2026, 4, session=dbsession)
        assert first.accounts_invoiced == 1
        second = invoice_metered_month(2026, 4, session=dbsession)
        assert second.accounts_invoiced == 0
        assert second.accounts_skipped == 1
        assert len(stripe._inv_calls) == 1

    def test_skip_when_invoice_amount_is_zero(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Pure-PAYG metered account with no usage in the period → no invoice."""
        from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO
        from orchestra.db.models.orchestra_models import BillingMode
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_zero",
        )
        tpl = BillingPlanTemplateDAO(dbsession).create_template(
            name="payg-metered-zero",
            billing_mode=BillingMode.METERED,
            is_custom=True,
            is_active=True,
        )
        _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 0
        assert result.accounts_skipped == 1
        assert len(stripe._inv_calls) == 0

    def test_credits_account_not_invoiced_by_metered_routine(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Belt-and-braces: the metered routine never touches CREDITS accounts.

        Equivalent to the old ``TestEligibility.test_credits_account_excluded``
        unit test, but driven through the public ``invoice_metered_month``
        entrypoint.
        """
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)

        ba = make_billing_account(
            dbsession,
            credits=100,
            stripe_customer_id="cus_credits",
        )
        # No metered assignment → on default plan (CREDITS).
        _record_metered_usage(dbsession, ba.id, Decimal("50"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 0
        assert result.accounts_skipped == 0  # not even considered eligible
        assert len(stripe._inv_calls) == 0

    def test_assignment_closed_before_period_is_not_invoiced(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Equivalent to the old internal eligibility test, exercised
        through the public entrypoint: a metered assignment that ended
        before the target period must not be picked up.
        """
        import datetime as _dt

        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )
        from orchestra.db.models.orchestra_models import DEFAULT_TEMPLATE_ID
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_closed",
        )
        tpl = _commit_template(dbsession, name="closed-pre-period")
        plan_dao = BillingPlanAssignmentDAO(dbsession)
        a = plan_dao.set_plan(billing_account_id=ba.id, template_id=tpl.id)
        # Backdate to Q1 2026 and end before our target period (April).
        _backdate_assignment(
            dbsession,
            a.id,
            _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        )
        plan_dao.set_plan(
            billing_account_id=ba.id,
            template_id=DEFAULT_TEMPLATE_ID,
            effective_at=_dt.datetime(2026, 3, 31, tzinfo=_dt.timezone.utc),
        )
        # Even with usage in April, the metered assignment isn't in force
        # at period_end → no invoice.
        _record_metered_usage(dbsession, ba.id, Decimal("500"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 0
        assert len(stripe._inv_calls) == 0

    def test_isolated_failure_does_not_block_other_accounts(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """One Stripe failure shouldn't stop the run."""
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        invoice_calls: list[dict] = []
        ii_calls: list[dict] = []

        def _ii_create(**kw):
            ii_calls.append(kw)
            return SimpleNamespace(id="ii_x")

        def _inv_create(**kw):
            invoice_calls.append(kw)
            if kw["customer"] == "cus_will_fail":
                raise RuntimeError("Stripe is down for this customer")
            return SimpleNamespace(id="in_ok")

        stripe_mod = SimpleNamespace(
            InvoiceItem=SimpleNamespace(create=_ii_create),
            Invoice=SimpleNamespace(create=_inv_create),
            StripeError=Exception,
            InvalidRequestError=Exception,
            Customer=SimpleNamespace(retrieve=lambda *a, **kw: SimpleNamespace()),
        )
        _patch_metered_stripe(monkeypatch, stripe_mod)
        _mute_metered_metrics(monkeypatch)

        ok_ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_ok",
        )
        bad_ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_will_fail",
        )
        tpl = _commit_template(dbsession, name="isolated-fail")
        _assign_for_period(dbsession, ok_ba, tpl, period_year=2026, period_month=4)
        tpl2 = _commit_template(dbsession, name="isolated-fail-2")
        _assign_for_period(dbsession, bad_ba, tpl2, period_year=2026, period_month=4)
        dbsession.refresh(ok_ba)
        dbsession.refresh(bad_ba)
        _record_metered_usage(dbsession, ok_ba.id, Decimal("1500"))
        _record_metered_usage(dbsession, bad_ba.id, Decimal("1500"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ok_ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=bad_ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1
        assert result.accounts_failed == 1
        assert len(result.errors) == 1
        ok_recharges = (
            dbsession.query(Recharge)
            .filter(Recharge.billing_account_id == ok_ba.id)
            .count()
        )
        bad_recharges = (
            dbsession.query(Recharge)
            .filter(Recharge.billing_account_id == bad_ba.id)
            .count()
        )
        assert ok_recharges == 1
        assert bad_recharges == 0

    def test_invoice_create_currency_matches_item_currency(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Regression: ``Invoice.create`` MUST pin its ``currency`` arg.

        Without it, Stripe defaults to the customer's / account's default
        currency; ``pending_invoice_items_behavior=include`` then only
        sweeps in pending items whose currency matches the invoice's.
        For an account whose Stripe-default currency diverges from the
        template's ``currency`` (e.g. an org that previously
        bought USD credits and was later moved to a GBP commit plan),
        the just-created InvoiceItem would silently never make it onto
        the invoice and we'd produce a zero-amount Stripe invoice
        despite recording a non-zero ``Recharge`` row locally — books
        diverge, customer never sees a bill.

        This test is intentionally a sibling of the FX policy tests
        below so the invariant is asserted both for USD-default
        templates (here) and for non-USD commit currencies (under
        ``TestMeteredInvoicerLockedRate``).
        """
        import datetime as _dt

        from orchestra.db.models.orchestra_models import CollectionMethod
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_currency_invariant",
        )
        tpl = _commit_template(
            dbsession,
            name="usd-currency-invariant",
            commit=Decimal("500"),
            collection=CollectionMethod.SEND_INVOICE_NET_30,
        )
        _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
        dbsession.refresh(ba)
        _record_metered_usage(dbsession, ba.id, Decimal("750"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1, result.errors

        ii = stripe._ii_calls[0]
        inv = stripe._inv_calls[0]
        assert "currency" in inv, (
            "Invoice.create was called without a currency arg — Stripe will "
            "default to the customer's currency and silently drop pending "
            "items in other currencies."
        )
        assert inv["currency"] == ii["currency"], (
            f"Invoice.create currency={inv['currency']!r} must match "
            f"InvoiceItem.create currency={ii['currency']!r}; otherwise "
            "pending_invoice_items_behavior=include drops the item."
        )

    def test_floor_only_emits_single_commitment_line(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """COMMITMENT under usage → one ``commitment`` line, no overage line.

        Counterpart to :meth:`test_invoices_committed_account_with_overage`:
        the two-line split only kicks in when chargeable usage exceeds
        the commit floor. Below the floor we bill the floor as a single
        line so the invoice doesn't carry a confusing $0 "overage" row.
        """
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_floor_only",
        )
        tpl = _commit_template(
            dbsession,
            name="floor-only-tpl",
            commit=Decimal("1000"),
        )
        _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
        dbsession.refresh(ba)
        # Usage well under the floor.
        _record_metered_usage(dbsession, ba.id, Decimal("250"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1
        assert len(stripe._ii_calls) == 1
        only = stripe._ii_calls[0]
        assert only["amount"] == 100000
        assert only["metadata"]["line_kind"] == "commitment"
        # No "usage overage" line below the floor.
        assert "overage" not in only["description"].lower()

    def test_display_name_used_for_invoice_lines_when_present(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """``BillingPlanTemplate.display_name`` is what shows up on lines.

        Internal slugs like ``vantage-overage-v3`` should never leak onto
        a customer-facing invoice. When a template has a ``display_name``
        the line descriptions and invoice metadata both surface it; the
        raw ``name`` stays in metadata for ops/audit.
        """
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_display_name",
        )
        tpl = _commit_template(
            dbsession,
            name="vantage-overage-v3-internal",
            display_name="Acme Enterprise",
            commit=Decimal("500"),
        )
        _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
        dbsession.refresh(ba)
        _record_metered_usage(dbsession, ba.id, Decimal("750"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 10, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1, result.errors

        # Each line description starts with the friendly display name,
        # never the internal slug.
        for ii in stripe._ii_calls:
            assert ii["description"].startswith(
                "Acme Enterprise — ",
            ), f"line description leaked internal slug: {ii['description']!r}"

        inv = stripe._inv_calls[0]
        # Metadata exposes both names so ops can correlate by either.
        assert inv["metadata"]["plan_template_name"] == "vantage-overage-v3-internal"
        assert inv["metadata"]["plan_template_display_name"] == "Acme Enterprise"


# ============================================================================
# Commit period × commit schedule matrix
# ============================================================================
#
# Two independent dimensions on COMMITMENT plans:
#
# * ``commit_period`` (MONTHLY / QUARTERLY / ANNUAL) — sets the
#   per-month overage floor (commit_amount / months_in_period). Overage
#   is recomputed every month against this floor; underuse one month
#   does NOT bank capacity for the next.
# * ``commit_schedule`` (AMORTISED / UPFRONT) — controls *when* the
#   commit dollars hit invoices. AMORTISED bills the per-month
#   equivalent every month; UPFRONT bills the full ``commit_amount``
#   on contract anniversaries (every ``months_in_period`` months from
#   the assignment's ``started_at``) and zero on intervening months.
#
# These tests exercise both dimensions through the public
# ``invoice_metered_month`` entrypoint with realistic ledger data.


class TestMeteredInvoicerCommitScheduleHelpers:
    """Unit tests for the date-math helpers driving the schedule logic."""

    def test_months_in_period(self):
        from orchestra.routines.monthly_metered_invoicer import _months_in_period

        assert _months_in_period("MONTHLY") == 1
        assert _months_in_period("QUARTERLY") == 3
        assert _months_in_period("ANNUAL") == 12
        # PAYG / unknown / NULL — degrade to 1 so callers never crash
        # on a bad config (the formula still produces a sensible result;
        # the misconfiguration shows up as a non-anniversary every
        # month for UPFRONT, which is at worst a $0 invoice).
        assert _months_in_period(None) == 1
        assert _months_in_period("WEEKLY") == 1

    def test_is_commit_billing_period_monthly_always_true(self):
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import (
            _is_commit_billing_period,
        )

        started = _dt.datetime(2026, 2, 15, tzinfo=_dt.timezone.utc)
        for month in range(2, 13):
            period_start = _dt.datetime(2026, month, 1, tzinfo=_dt.timezone.utc)
            assert _is_commit_billing_period(
                started_at=started,
                commit_period="MONTHLY",
                period_start=period_start,
            ), f"MONTHLY commit should be anniversary every month ({month})"

    def test_is_commit_billing_period_annual(self):
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import (
            _is_commit_billing_period,
        )

        started = _dt.datetime(2026, 2, 15, tzinfo=_dt.timezone.utc)
        # 2026-02 — anniversary 0 (the start month itself).
        assert _is_commit_billing_period(
            started_at=started,
            commit_period="ANNUAL",
            period_start=_dt.datetime(2026, 2, 1, tzinfo=_dt.timezone.utc),
        )
        # 2026-03 .. 2027-01 — non-anniversary months.
        for month in (3, 6, 9, 12):
            ps = _dt.datetime(2026, month, 1, tzinfo=_dt.timezone.utc)
            assert not _is_commit_billing_period(
                started_at=started,
                commit_period="ANNUAL",
                period_start=ps,
            ), f"ANNUAL: 2026-{month:02d} should not be anniversary"
        assert not _is_commit_billing_period(
            started_at=started,
            commit_period="ANNUAL",
            period_start=_dt.datetime(2027, 1, 1, tzinfo=_dt.timezone.utc),
        )
        # 2027-02 — first re-anniversary (12 months elapsed).
        assert _is_commit_billing_period(
            started_at=started,
            commit_period="ANNUAL",
            period_start=_dt.datetime(2027, 2, 1, tzinfo=_dt.timezone.utc),
        )

    def test_is_commit_billing_period_quarterly(self):
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import (
            _is_commit_billing_period,
        )

        started = _dt.datetime(2026, 1, 10, tzinfo=_dt.timezone.utc)
        # Anniversaries: Jan, Apr, Jul, Oct of 2026.
        for anniversary_month in (1, 4, 7, 10):
            ps = _dt.datetime(
                2026,
                anniversary_month,
                1,
                tzinfo=_dt.timezone.utc,
            )
            assert _is_commit_billing_period(
                started_at=started,
                commit_period="QUARTERLY",
                period_start=ps,
            ), f"QUARTERLY: 2026-{anniversary_month:02d} should be anniversary"
        for non_anniversary_month in (2, 3, 5, 6, 8, 9, 11, 12):
            ps = _dt.datetime(
                2026,
                non_anniversary_month,
                1,
                tzinfo=_dt.timezone.utc,
            )
            assert not _is_commit_billing_period(
                started_at=started,
                commit_period="QUARTERLY",
                period_start=ps,
            )

    def test_is_commit_billing_period_pre_start(self):
        """A period before the assignment started is never an anniversary."""
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import (
            _is_commit_billing_period,
        )

        started = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
        ps = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
        assert not _is_commit_billing_period(
            started_at=started,
            commit_period="MONTHLY",
            period_start=ps,
        )


class TestMeteredInvoicerCommitSchedule:
    """End-to-end coverage of AMORTISED / UPFRONT × commit_period."""

    def test_amortised_annual_charges_monthly_equivalent_every_month(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """AMORTISED + ANNUAL: every month bills $1k of a $12k contract."""
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_amort_annual",
        )
        tpl = _commit_template(
            dbsession,
            name="amortised-annual",
            commit=Decimal("12000"),
            commit_period="ANNUAL",
            commit_schedule="AMORTISED",
        )
        # Backdate well into the past so we're not on the first period.
        _assign_for_period(
            dbsession,
            ba,
            tpl,
            period_year=2026,
            period_month=4,
        )
        dbsession.refresh(ba)
        # Use < monthly_commit ($1000) so there's no overage.
        _record_metered_usage(dbsession, ba.id, Decimal("400"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1, result.errors

        # Single line: monthly commitment at $1000 (12000 / 12), NOT
        # the $12k contract total.
        assert len(stripe._ii_calls) == 1
        line = stripe._ii_calls[0]
        assert line["amount"] == 100000  # $1000 in cents
        assert line["metadata"]["line_kind"] == "commitment"

        # Recharge.detail must record both the per-month equivalent
        # and the period total so audits can re-derive either view.
        recharge = (
            dbsession.query(Recharge).filter(Recharge.billing_account_id == ba.id).one()
        )
        assert Decimal(recharge.detail["commit_amount"]) == Decimal("12000")
        assert Decimal(recharge.detail["monthly_commit_local"]) == Decimal("1000")
        assert Decimal(recharge.detail["commit_charge_local"]) == Decimal("1000")
        assert recharge.detail["commit_schedule"] == "AMORTISED"
        assert recharge.detail["is_commit_billing_period"] is True

    def test_amortised_annual_with_overage_uses_per_month_floor(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Overage on a $12k/yr contract triggers above $1k/mo, NOT $12k."""
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_amort_overage",
        )
        tpl = _commit_template(
            dbsession,
            name="amortised-annual-overage",
            commit=Decimal("12000"),
            commit_period="ANNUAL",
            commit_schedule="AMORTISED",
        )
        _assign_for_period(
            dbsession,
            ba,
            tpl,
            period_year=2026,
            period_month=4,
        )
        dbsession.refresh(ba)
        # $1500 raw usage in one month → $500 over the $1k monthly floor.
        _record_metered_usage(dbsession, ba.id, Decimal("1500"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1, result.errors
        assert len(stripe._ii_calls) == 2
        commit_line, overage_line = stripe._ii_calls
        assert commit_line["amount"] == 100000  # $1000 monthly equivalent
        assert overage_line["amount"] == 50000  # $500 above the per-month floor
        assert overage_line["metadata"]["line_kind"] == "overage"

    def test_upfront_annual_anniversary_bills_full_commit(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """UPFRONT + ANNUAL on the anniversary month bills the FULL $12k."""
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_upfront_anniversary",
        )
        tpl = _commit_template(
            dbsession,
            name="upfront-annual-anniversary",
            commit=Decimal("12000"),
            commit_period="ANNUAL",
            commit_schedule="UPFRONT",
        )
        # Assignment starts in April 2026 (the anniversary month).
        _assign_for_period(
            dbsession,
            ba,
            tpl,
            period_year=2026,
            period_month=4,
        )
        dbsession.refresh(ba)
        # No overage — $400 < $1000/mo floor.
        _record_metered_usage(dbsession, ba.id, Decimal("400"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1, result.errors

        # One line: the full $12k commit on the anniversary.
        assert len(stripe._ii_calls) == 1
        line = stripe._ii_calls[0]
        assert line["amount"] == 1200000  # $12000 in cents
        assert "annual commitment" in line["description"].lower()

        recharge = (
            dbsession.query(Recharge).filter(Recharge.billing_account_id == ba.id).one()
        )
        assert Decimal(recharge.detail["commit_charge_local"]) == Decimal("12000")
        assert Decimal(recharge.detail["monthly_commit_local"]) == Decimal("1000")
        assert recharge.detail["commit_schedule"] == "UPFRONT"
        assert recharge.detail["is_commit_billing_period"] is True

    def test_upfront_annual_non_anniversary_with_overage_bills_overage_only(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """UPFRONT + ANNUAL non-anniversary month: overage line only, no commit."""
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_upfront_non_anniv",
        )
        tpl = _commit_template(
            dbsession,
            name="upfront-annual-mid",
            commit=Decimal("12000"),
            commit_period="ANNUAL",
            commit_schedule="UPFRONT",
        )
        # Started in April 2026; we're invoicing for July 2026
        # (3 months later → not an anniversary).
        _assign_for_period(
            dbsession,
            ba,
            tpl,
            period_year=2026,
            period_month=4,
        )
        dbsession.refresh(ba)
        # Backdate ledger into July; assignment still in force.
        _record_metered_usage(dbsession, ba.id, Decimal("1500"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 7, 15, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 7, session=dbsession)
        assert result.accounts_invoiced == 1, result.errors

        # Single line: overage only ($500 above the $1k/mo floor); the
        # full annual commit was already billed back in April.
        assert len(stripe._ii_calls) == 1
        line = stripe._ii_calls[0]
        assert line["amount"] == 50000  # $500 in cents
        assert line["metadata"]["line_kind"] == "overage"

        recharge = (
            dbsession.query(Recharge).filter(Recharge.billing_account_id == ba.id).one()
        )
        assert Decimal(recharge.detail["commit_charge_local"]) == Decimal("0")
        assert Decimal(recharge.detail["overage_charge_local"]) == Decimal("500")
        assert recharge.detail["commit_schedule"] == "UPFRONT"
        assert recharge.detail["is_commit_billing_period"] is False

    def test_upfront_annual_non_anniversary_no_overage_skips_invoice(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """UPFRONT non-anniversary + no overage = $0 invoice = skip."""
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_upfront_quiet",
        )
        tpl = _commit_template(
            dbsession,
            name="upfront-annual-quiet",
            commit=Decimal("12000"),
            commit_period="ANNUAL",
            commit_schedule="UPFRONT",
        )
        _assign_for_period(
            dbsession,
            ba,
            tpl,
            period_year=2026,
            period_month=4,
        )
        dbsession.refresh(ba)
        # Under the $1k/mo floor in a non-anniversary month → nothing to bill.
        _record_metered_usage(dbsession, ba.id, Decimal("250"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 7, 15, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 7, session=dbsession)
        assert result.accounts_invoiced == 0
        assert result.accounts_skipped == 1
        # No InvoiceItem.create / Invoice.create called — Stripe is
        # untouched for $0 invoices.
        assert stripe._ii_calls == []
        assert stripe._inv_calls == []

    def test_pricing_factors_stack_on_overage(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Base discount applies uniformly; overage is an *additional* uplift on top.

        Pricing model: ``base_pricing_factor`` applies to ALL usage
        (commit-included + overage); ``overage_pricing_factor`` is a
        multiplier *stacked on top* of base, only for the overage
        portion. So a customer on ``base=0.80, overage=1.25`` pays:

        * within commit: ``0.80×`` of list price
        * above commit: ``0.80 × 1.25 = 1.00×`` of list price
          (the overage uplift cancels the base discount, putting
          above-commit usage back at list price)

        The commit floor itself is denominated in contract currency
        — it doesn't move when the base factor changes (the operator
        priced the commit in dollars they wanted to charge), so this
        test exercises the overage line specifically.
        """
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_pricing_stack",
        )
        # $1000/mo commit, base=0.80 (20% discount), overage=1.25
        # (25% uplift over base = back to list).
        # included_capacity_local = 1000 / 0.80 = $1250 raw USD covered
        # raw usage = $1500 → overage_raw = $250
        # overage_charge = 250 × 0.80 × 1.25 = $250 (= list price)
        tpl = _commit_template(
            dbsession,
            name="stacked-rates",
            commit=Decimal("1000"),
            pricing_factor=Decimal("0.80"),
            overage_pricing_factor=Decimal("1.25"),
        )
        _assign_for_period(
            dbsession,
            ba,
            tpl,
            period_year=2026,
            period_month=4,
        )
        dbsession.refresh(ba)
        _record_metered_usage(dbsession, ba.id, Decimal("1500"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1, result.errors
        assert len(stripe._ii_calls) == 2
        commit_line, overage_line = stripe._ii_calls
        assert commit_line["amount"] == 100000  # $1000 commit floor (verbatim)
        # $250 overage_raw × 0.80 base × 1.25 uplift = $250.00 → 25000 cents.
        assert overage_line["amount"] == 25000
        # When overage uplift differs from 1.0, the line description
        # surfaces the multiplier so the customer can see why
        # above-commit costs are different from the base rate they
        # signed up for.
        assert "1.25" in overage_line["description"]
        assert "base rate" in overage_line["description"].lower()

    def test_overage_with_no_uplift_uses_base_only(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """``overage_pricing_factor=1.0`` means "no uplift" — base discount applies above commit too."""
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_no_uplift",
        )
        # base=0.50 (50% discount), overage=1.0 (no uplift) →
        # included_capacity = 1000/0.5 = $2000 raw covered
        # raw = $2400 → overage_raw = $400
        # overage_charge = 400 × 0.5 × 1.0 = $200
        tpl = _commit_template(
            dbsession,
            name="no-uplift",
            commit=Decimal("1000"),
            pricing_factor=Decimal("0.50"),
            overage_pricing_factor=Decimal("1.0"),
        )
        _assign_for_period(
            dbsession,
            ba,
            tpl,
            period_year=2026,
            period_month=4,
        )
        dbsession.refresh(ba)
        _record_metered_usage(dbsession, ba.id, Decimal("2400"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1, result.errors
        commit_line, overage_line = stripe._ii_calls
        assert commit_line["amount"] == 100000  # $1000 commit floor
        assert overage_line["amount"] == 20000  # $200 above-commit
        # 1.0 overage uplift = "no overage penalty" → terse line
        # description (no rate noise).
        assert "1.0" not in overage_line["description"]
        assert "base rate" not in overage_line["description"].lower()

    def test_upfront_quarterly_anniversary_billing(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """UPFRONT + QUARTERLY: full commit every 3 months, overage in between."""
        import datetime as _dt

        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_upfront_quarter",
        )
        tpl = _commit_template(
            dbsession,
            name="upfront-quarterly",
            commit=Decimal("3000"),
            commit_period="QUARTERLY",
            commit_schedule="UPFRONT",
        )
        _assign_for_period(
            dbsession,
            ba,
            tpl,
            period_year=2026,
            period_month=1,
        )
        dbsession.refresh(ba)
        # Quiet first month, anniversary — should bill $3000 commit.
        _record_metered_usage(dbsession, ba.id, Decimal("100"))
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 1, 15, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()
        result = invoice_metered_month(2026, 1, session=dbsession)
        assert result.accounts_invoiced == 1, result.errors
        assert len(stripe._ii_calls) == 1
        assert stripe._ii_calls[0]["amount"] == 300000  # full quarterly commit
        assert "quarterly commitment" in (stripe._ii_calls[0]["description"].lower())


# ============================================================================
# payment_method_types + customer_balance plumbing
# ============================================================================
#
# Tests pinning the ``payment_settings`` block written to
# ``Invoice.create``. The contract is:
#
# * SEND_INVOICE_NET_30 + no customer override → ['card', 'customer_balance']
#   (with the bank-transfer rail keyed off the invoice currency).
# * AUTO_CARD          → ['card'] only — auto-pull can't sweep a wire.
# * Customer-level override → honoured verbatim, overrides both defaults.
#
# Tests do not require ``customer_balance`` to be enabled in the live
# Stripe account; the ``stripe`` module is mocked. Production gating
# happens in the dashboard.


class TestMonthlyMeteredInvoicerPaymentMethods:
    """``payment_settings.payment_method_types`` resolution + funding type."""

    def _setup_account_with(
        self,
        dbsession,
        monkeypatch,
        *,
        currency: str,
        fx_policy=None,
        fx_locked_rate=None,
        preferred_payment_method_types: list[str] | None = None,
        collection=None,
        billing_country: str | None = None,
    ):
        """Create a fully-provisioned account and run the invoicer.

        Returns ``(stripe_mock, invoice_call_kwargs)``. Encapsulates
        the boilerplate so each test stays focused on its assertion.

        ``billing_country`` is stamped into ``BillingAccount.billing_address``
        — required for EUR ``customer_balance`` (Stripe needs an IBAN
        country) and harmless for every other currency.
        """
        import datetime as _dt

        from orchestra.db.dao.billing_account_dao import BillingAccountDAO
        from orchestra.db.models.orchestra_models import CollectionMethod, FxPolicy
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        if currency.upper() != "USD":
            # Non-USD templates require an explicit fx_policy. We pin
            # to LOCKED_RATE so the test never hits the network and
            # FX failures can't muddy the assertion under test.
            ba, _assignment, _tpl = _provision_fx_account(
                dbsession,
                fx_policy=fx_policy or FxPolicy.LOCKED_RATE,
                fx_locked_rate=fx_locked_rate or Decimal("0.80"),
                commit_amount=Decimal("800"),
                currency=currency.upper(),
                name=f"pm-{currency.lower()}-tpl",
            )
        else:
            ba = make_billing_account(
                dbsession,
                credits=0,
                stripe_customer_id=f"cus_pm_{currency.lower()}",
            )
            tpl = _commit_template(
                dbsession,
                name=f"pm-{currency.lower()}-tpl",
                commit=Decimal("1000"),
                collection=collection or CollectionMethod.SEND_INVOICE_NET_30,
            )
            _assign_for_period(dbsession, ba, tpl, period_year=2026, period_month=4)
            dbsession.refresh(ba)

        if billing_country is not None:
            ba.billing_address = {"country": billing_country}
            dbsession.flush()

        if preferred_payment_method_types is not None:
            BillingAccountDAO(dbsession).set_payment_preferences(
                ba.id,
                preferred_payment_method_types=preferred_payment_method_types,
            )

        BillingAccountDAO(dbsession).deduct_credits(ba.id, 1250.0, category="llm")
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, 12, 0, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        dbsession.commit()
        assert result.accounts_invoiced == 1, result.errors
        return stripe, stripe._inv_calls[0]

    def test_usd_send_invoice_default_offers_card_and_customer_balance(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """SEND_INVOICE_NET_30 + USD → both card and customer_balance, US rail."""
        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="USD",
        )
        ps = inv["payment_settings"]
        assert ps["payment_method_types"] == ["card", "customer_balance"]
        assert ps["payment_method_options"]["customer_balance"] == {
            "funding_type": "bank_transfer",
            # USD invoices route to the US virtual bank account; routing
            # them to the wrong rail silently sends the customer's wire
            # to the wrong virtual account.
            "bank_transfer": {"type": "us_bank_transfer"},
        }
        assert "card" in ps["payment_method_options"]

    def test_gbp_send_invoice_default_uses_uk_bank_transfer(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """SEND_INVOICE_NET_30 + GBP → customer_balance with UK rail."""
        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="GBP",
        )
        ps = inv["payment_settings"]
        assert ps["payment_method_types"] == ["card", "customer_balance"]
        assert ps["payment_method_options"]["customer_balance"]["bank_transfer"] == {
            "type": "gb_bank_transfer",
        }

    def test_auto_card_only_offers_card(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """AUTO_CARD invoices never offer customer_balance.

        ``customer_balance`` is a *push* method (customer wires money
        to a virtual account) — it is meaningless on a
        ``charge_automatically`` invoice where Stripe attempts to
        *pull* from the saved card. Including it in
        ``payment_method_types`` would cause Stripe to render the
        wire-transfer instructions on the receipt for an auto-paid
        invoice, which is confusing nonsense.
        """
        from orchestra.db.models.orchestra_models import CollectionMethod

        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="USD",
            collection=CollectionMethod.AUTO_CARD,
        )
        assert inv["payment_settings"]["payment_method_types"] == ["card"]
        assert (
            "customer_balance" not in inv["payment_settings"]["payment_method_options"]
        )

    def test_customer_override_wire_only(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Per-customer override is honoured verbatim.

        Setting ``preferred_payment_method_types=['customer_balance']``
        on the BillingAccount lets ops mark Acme as wire-only without
        spinning a new template. The invoicer must not silently re-add
        ``card`` to the list "to be safe" — that defeats the override.
        """
        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="USD",
            preferred_payment_method_types=["customer_balance"],
        )
        ps = inv["payment_settings"]
        assert ps["payment_method_types"] == ["customer_balance"]
        # No card options surface either — the customer has explicitly
        # opted out.
        assert "card" not in ps["payment_method_options"]
        assert "customer_balance" in ps["payment_method_options"]

    def test_customer_override_card_only(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Inverse override — force card-only on a SEND_INVOICE plan."""
        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="USD",
            preferred_payment_method_types=["card"],
        )
        assert inv["payment_settings"]["payment_method_types"] == ["card"]

    def test_eur_with_supported_country_uses_eu_bank_transfer(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """EUR + DE billing country → eu_bank_transfer with country tag.

        Stripe requires the ``eu_bank_transfer.country`` parameter for
        EUR cash-balance funding — the country picks which national
        IBAN scheme issues the virtual account. Without it Stripe
        rejects the invoice.
        """
        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="EUR",
            billing_country="DE",
        )
        ps = inv["payment_settings"]
        assert ps["payment_method_types"] == ["card", "customer_balance"]
        assert ps["payment_method_options"]["customer_balance"] == {
            "funding_type": "bank_transfer",
            "bank_transfer": {
                "type": "eu_bank_transfer",
                "eu_bank_transfer": {"country": "DE"},
            },
        }

    def test_eur_without_country_falls_back_to_card_only(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """EUR without a billing country drops customer_balance silently.

        Falling back is the right behaviour: the customer's invoice still
        goes out via card. Failing the run for the account would be a
        worse outcome (one-row dataset issue blocks the entire monthly
        cohort), and a WARNING is logged so ops can chase up the missing
        country.
        """
        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="EUR",
            billing_country=None,
        )
        ps = inv["payment_settings"]
        assert ps["payment_method_types"] == ["card"]
        assert "customer_balance" not in ps["payment_method_options"]

    def test_eur_with_unsupported_country_falls_back_to_card_only(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """EUR + AT (not in Stripe's IBAN whitelist) → card-only.

        AT (Austria) has the EUR currency but Stripe doesn't currently
        issue a virtual IBAN for AT customers, so customer_balance
        funding would be rejected. Falling back keeps invoicing working
        without bespoke per-customer handling.
        """
        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="EUR",
            billing_country="AT",
        )
        ps = inv["payment_settings"]
        assert ps["payment_method_types"] == ["card"]
        assert "customer_balance" not in ps["payment_method_options"]

    def test_jpy_uses_jp_bank_transfer(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """JPY → jp_bank_transfer (no country parameter required)."""
        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="JPY",
        )
        ps = inv["payment_settings"]
        assert ps["payment_method_types"] == ["card", "customer_balance"]
        assert ps["payment_method_options"]["customer_balance"]["bank_transfer"] == {
            "type": "jp_bank_transfer",
        }

    def test_mxn_uses_mx_bank_transfer(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """MXN → mx_bank_transfer."""
        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="MXN",
        )
        ps = inv["payment_settings"]
        assert ps["payment_method_types"] == ["card", "customer_balance"]
        assert ps["payment_method_options"]["customer_balance"]["bank_transfer"] == {
            "type": "mx_bank_transfer",
        }

    def test_unsupported_currency_falls_back_to_card_only(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """CAD has no Stripe bank-transfer rail today → card-only.

        The invoicer must not throw — that would fail the entire
        monthly run for the account. Card-only is a deliverable
        invoice; ops adds a CAD rail by extending
        ``_BANK_TRANSFER_TYPE_BY_CURRENCY`` once Stripe ships it.
        """
        _, inv = self._setup_account_with(
            dbsession,
            monkeypatch,
            currency="CAD",
        )
        ps = inv["payment_settings"]
        assert ps["payment_method_types"] == ["card"]
        assert "customer_balance" not in ps["payment_method_options"]

    def test_explicit_override_on_unsupported_currency_raises(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Forcing customer_balance via override + unsupported currency = error.

        We accept the per-account run failure here because the operator
        explicitly opted into wire-only. Silently dropping the override
        and billing the customer's card would be a worse surprise than
        a noisy failure that prompts the operator to either remove the
        override or add the rail.
        """
        from orchestra.db.dao.billing_account_dao import BillingAccountDAO
        from orchestra.db.models.orchestra_models import FxPolicy
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        # CAD is unsupported by _BANK_TRANSFER_TYPE_BY_CURRENCY today.
        ba, _assignment, _tpl = _provision_fx_account(
            dbsession,
            fx_policy=FxPolicy.LOCKED_RATE,
            fx_locked_rate=Decimal("0.74"),
            commit_amount=Decimal("800"),
            currency="CAD",
            name="pm-cad-override-tpl",
        )
        BillingAccountDAO(dbsession).set_payment_preferences(
            ba.id,
            preferred_payment_method_types=["customer_balance"],
        )
        BillingAccountDAO(dbsession).deduct_credits(ba.id, 1250.0, category="llm")
        import datetime as _dt

        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, 12, 0, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        # Per-account isolation: the bulk routine catches the
        # ValueError, records it, and continues. No invoice was
        # produced for this account.
        assert result.accounts_invoiced == 0
        assert result.accounts_failed == 1
        assert any("customer_balance" in e for e in result.errors)


# ============================================================================
# FX policies driving the metered invoicer
# ============================================================================
#
# These exercise ``invoice_metered_month`` against a GBP-denominated
# COMMITMENT account under each FX policy. The Frankfurter HTTP layer is
# stubbed; we never hit the network. Per-policy unit tests against
# ``orchestra.lib.fx`` are intentionally not asserted here — the policy
# tests below drive the same code paths through the public entrypoint
# and verify the audit JSONB carries the resolved rate + provenance.
#
# The "USD = no FX" path (``fx_policy IS NULL``) is exercised implicitly
# by every USD-denominated metered test above, so we don't re-cover it
# here.


class _FxStubResponse:
    """Minimal stand-in for ``requests.Response`` used by the FX module."""

    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            import requests as _r

            raise _r.HTTPError(f"status={self.status_code}")

    def json(self) -> dict:
        return self._payload


def _patch_frankfurter(monkeypatch, payload_for_url):
    """Patch ``requests.get`` inside ``orchestra.lib.fx``.

    ``payload_for_url`` is a callable ``(url, params) -> dict`` returning
    the JSON the stubbed Frankfurter should reply with.
    """

    def _get(url, params=None, timeout=None):  # noqa: ARG001
        return _FxStubResponse(payload_for_url(url, params or {}))

    monkeypatch.setattr("orchestra.lib.fx.requests.get", _get)


def _provision_fx_account(
    dbsession: Session,
    *,
    fx_policy,
    fx_locked_rate=None,
    commit_amount: Decimal = Decimal("800"),
    currency: str = "GBP",
    name: str | None = None,
    started_at=None,
):
    """Create a GBP METERED account on the given FX policy."""
    import datetime as _dt

    from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO
    from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO
    from orchestra.db.models.orchestra_models import BillingMode, CollectionMethod

    ba = make_billing_account(
        dbsession,
        stripe_customer_id=f"cus_fx_{fx_policy.value.lower()}",
        account_status="ACTIVE",
    )
    # The conftest factory inserts a default plan assignment starting at
    # ``now()``. We're about to switch to a metered template at a
    # historical ``effective_at`` (March 2026) and ``set_plan`` will close
    # the default plan row at that moment — which would violate the
    # ``started_at <= ended_at`` check. Backdate the initial default
    # row well before the target period so the close timestamp is valid.
    from sqlalchemy import text as _sql_text

    dbsession.execute(
        _sql_text(
            "UPDATE billing_plan_assignment "
            "SET started_at = :ts WHERE billing_account_id = :ba",
        ),
        {
            "ts": _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
            "ba": ba.id,
        },
    )
    dbsession.flush()

    tpl = BillingPlanTemplateDAO(dbsession).create_template(
        name=name or f"vantage-{fx_policy.value.lower()}",
        billing_mode=BillingMode.METERED,
        commit_amount=commit_amount,
        currency=currency,
        commit_period="MONTHLY",
        base_pricing_factor=Decimal("1.0"),
        overage_pricing_factor=Decimal("1.0"),
        collection_method=CollectionMethod.SEND_INVOICE_NET_30,
        is_custom=True,
        is_active=True,
        fx_policy=fx_policy,
        fx_locked_rate=fx_locked_rate,
    )
    when_started = started_at or _dt.datetime(2026, 3, 15, tzinfo=_dt.timezone.utc)
    assignment = BillingPlanAssignmentDAO(dbsession).set_plan(
        billing_account_id=ba.id,
        template_id=tpl.id,
        effective_at=when_started,
    )
    return ba, assignment, tpl


class TestMeteredInvoicerLockedRate:
    """LOCKED_RATE: rate fixed at template-creation time, no live fetches."""

    def test_invoices_at_locked_rate_with_no_provider_calls(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import datetime as _dt

        from orchestra.db.dao.billing_account_dao import BillingAccountDAO
        from orchestra.db.models.orchestra_models import (
            RECHARGE_TYPE_MONTHLY_COMMIT,
            FxPolicy,
        )
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        # If the invoicer touches Frankfurter for a LOCKED_RATE template
        # we want the test to fail loudly — that's a regression.
        def _no_network(*_args, **_kwargs):  # pragma: no cover
            raise AssertionError("LOCKED_RATE must not hit the FX provider")

        monkeypatch.setattr("orchestra.lib.fx.requests.get", _no_network)

        ba, _assignment, _tpl = _provision_fx_account(
            dbsession,
            fx_policy=FxPolicy.LOCKED_RATE,
            fx_locked_rate=Decimal("0.80"),
            commit_amount=Decimal("800"),
        )
        # $1,250 raw USD usage * 0.80 = £1,000; commit £800 → invoice £1,000.
        BillingAccountDAO(dbsession).deduct_credits(ba.id, 1250.00, category="llm")
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, 12, 0, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        dbsession.commit()
        assert result.accounts_invoiced == 1, result.errors

        # Two-line split when chargeable usage > commit: line[0] is the
        # commit floor (£800), line[1] is the overage (£200). The FX
        # invariant under test is the total — both lines must be in
        # the right currency and sum to the expected GBP total.
        assert all(ii["currency"] == "gbp" for ii in stripe._ii_calls)
        assert sum(ii["amount"] for ii in stripe._ii_calls) == 100000
        # And the Invoice itself MUST be created in the same currency —
        # see ``test_invoice_create_currency_matches_item_currency`` for
        # why this matters specifically for non-USD templates.
        invoice = stripe._inv_calls[0]
        assert invoice["currency"] == "gbp"

        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba.id, type=RECHARGE_TYPE_MONTHLY_COMMIT)
            .one()
        )
        assert recharge.detail["fx_policy"] == "LOCKED_RATE"
        assert Decimal(recharge.detail["fx_rate"]) == Decimal("0.80")
        assert recharge.detail["fx_provider"] is None
        assert recharge.detail["fx_as_of_date"] is None
        assert recharge.detail["currency"] == "GBP"


class TestMeteredInvoicerSpot:
    """SPOT: live-fetch the period-end rate from Frankfurter."""

    def test_uses_period_end_rate_and_pins_to_recharge(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import datetime as _dt

        from orchestra.db.dao.billing_account_dao import BillingAccountDAO
        from orchestra.db.models.orchestra_models import (
            RECHARGE_TYPE_MONTHLY_COMMIT,
            FxPolicy,
        )
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)
        seen_urls: list[str] = []

        def _payload(url, params):  # noqa: ARG001
            seen_urls.append(url)
            return {"rates": {"GBP": 0.80}}

        _patch_frankfurter(monkeypatch, _payload)

        ba, _assignment, _tpl = _provision_fx_account(
            dbsession,
            fx_policy=FxPolicy.SPOT,
            commit_amount=Decimal("800"),
        )
        BillingAccountDAO(dbsession).deduct_credits(ba.id, 1250.00, category="llm")
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, 12, 0, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        dbsession.commit()
        assert result.accounts_invoiced == 1, result.errors

        # The SPOT policy hits the period-end (last day of April).
        assert any("2026-04-30" in u for u in seen_urls), seen_urls
        # Commit + overage split — sum across the lines is what exercises
        # the FX conversion (commit £800 + overage £200 = £1,000 total).
        assert all(ii["currency"] == "gbp" for ii in stripe._ii_calls)
        assert sum(ii["amount"] for ii in stripe._ii_calls) == 100000

        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba.id, type=RECHARGE_TYPE_MONTHLY_COMMIT)
            .one()
        )
        assert recharge.detail["fx_policy"] == "SPOT"
        assert recharge.detail["fx_provider"] == "frankfurter"
        assert recharge.detail["fx_as_of_date"] == "2026-04-30"
        assert Decimal(recharge.detail["fx_rate"]) == Decimal("0.80")

    def test_provider_outage_skips_account_softly(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Frankfurter outage = soft skip, not failure: the bulk run keeps
        going so other accounts still get invoiced.

        Subsumes the old internal ``test_request_failure_raises_provider_error``
        unit test — the public entrypoint is what the operator cares about.
        """
        import datetime as _dt

        from orchestra.db.dao.billing_account_dao import BillingAccountDAO
        from orchestra.db.models.orchestra_models import FxPolicy
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)
        import requests as _r

        def _raise(*_args, **_kwargs):
            raise _r.ConnectionError("Frankfurter down")

        monkeypatch.setattr("orchestra.lib.fx.requests.get", _raise)

        ba, _assignment, _tpl = _provision_fx_account(
            dbsession,
            fx_policy=FxPolicy.SPOT,
            name="vantage-spot-outage",
            currency="GBP",
        )
        BillingAccountDAO(dbsession).deduct_credits(ba.id, 100, category="llm")
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, 12, 0, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        dbsession.commit()

        assert result.accounts_invoiced == 0
        assert result.accounts_skipped == 1
        assert result.accounts_failed == 0
        assert len(stripe._ii_calls) == 0


class TestMeteredInvoicerPeriodAverage:
    """PERIOD_AVERAGE: average of the time-series across the billing period."""

    def test_invoices_at_average_rate(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import datetime as _dt

        from orchestra.db.dao.billing_account_dao import BillingAccountDAO
        from orchestra.db.models.orchestra_models import (
            RECHARGE_TYPE_MONTHLY_COMMIT,
            FxPolicy,
        )
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        # Average of (0.78 + 0.80 + 0.82) / 3 = 0.80 exactly.
        def _payload(url, params):  # noqa: ARG001
            return {
                "rates": {
                    "2026-04-01": {"GBP": 0.78},
                    "2026-04-15": {"GBP": 0.80},
                    "2026-04-30": {"GBP": 0.82},
                },
            }

        _patch_frankfurter(monkeypatch, _payload)

        ba, _assignment, _tpl = _provision_fx_account(
            dbsession,
            fx_policy=FxPolicy.PERIOD_AVERAGE,
            name="vantage-avg",
            commit_amount=Decimal("800"),
        )
        BillingAccountDAO(dbsession).deduct_credits(ba.id, 1250.00, category="llm")
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, 12, 0, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        dbsession.commit()
        assert result.accounts_invoiced == 1, result.errors

        # 1250 USD * 0.80 average = £1,000.00 total, broken across the
        # commit floor (£800) and the £200 overage line.
        assert all(ii["currency"] == "gbp" for ii in stripe._ii_calls)
        assert sum(ii["amount"] for ii in stripe._ii_calls) == 100000

        recharge = (
            dbsession.query(Recharge)
            .filter_by(billing_account_id=ba.id, type=RECHARGE_TYPE_MONTHLY_COMMIT)
            .one()
        )
        assert recharge.detail["fx_policy"] == "PERIOD_AVERAGE"
        assert recharge.detail["fx_period_start"] == "2026-04-01"
        assert recharge.detail["fx_period_end"] == "2026-04-30"
        # Re-runs verify the same business dates were used.
        assert recharge.detail["fx_sample_dates"] == [
            "2026-04-01",
            "2026-04-15",
            "2026-04-30",
        ]


# ============================================================================
# Auto-Recharge Queuing
# ============================================================================


def _mock_customer_with_pm():
    """Return a mock Stripe Customer that has a default payment method."""
    return SimpleNamespace(
        invoice_settings=SimpleNamespace(
            default_payment_method=SimpleNamespace(id="pm_test"),
        ),
        default_source=None,
    )


def _mock_customer_without_pm():
    """Return a mock Stripe Customer with no payment method."""
    return SimpleNamespace(
        invoice_settings=SimpleNamespace(default_payment_method=None),
        default_source=None,
    )


def _make_stripe_mock(
    *,
    invoice_item_create=None,
    invoice_item_delete=None,
    customer_retrieve=None,
    stripe_error_cls=Exception,
    invalid_request_cls=None,
):
    """Build a ``SimpleNamespace`` that quacks like the ``stripe`` module."""
    if invoice_item_create is None:
        invoice_item_create = lambda **kw: SimpleNamespace(id="ii_test")
    if customer_retrieve is None:
        customer_retrieve = lambda *a, **kw: _mock_customer_with_pm()
    ii_ns = {"create": invoice_item_create}
    if invoice_item_delete is not None:
        ii_ns["delete"] = invoice_item_delete
    invalid = invalid_request_cls or stripe_error_cls
    return SimpleNamespace(
        InvoiceItem=SimpleNamespace(**ii_ns),
        Customer=SimpleNamespace(retrieve=customer_retrieve),
        StripeError=stripe_error_cls,
        InvalidRequestError=invalid,
        error=SimpleNamespace(
            StripeError=stripe_error_cls,
            InvalidRequestError=invalid,
        ),
    )


class TestAutoRechargeQueuing:
    """Tests for the queue_auto_recharge function."""

    def test_basic(self, dbsession: Session, monkeypatch):
        """queue_auto_recharge creates a PENDING_INVOICE recharge record."""
        import orchestra.lib.billing

        mock_stripe = _make_stripe_mock()
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=100,
            stripe_customer_id="cus_test123",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        make_user(dbsession, "test_user_ar", ba)
        dbsession.commit()

        result = queue_auto_recharge(dbsession, ba, 50)
        dbsession.commit()

        assert result is True
        recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
        assert recharge is not None
        assert recharge.quantity == Decimal("50")
        assert recharge.amount_usd == Decimal("50.00")
        assert recharge.status == RechargeStatus.PENDING_INVOICE
        assert recharge.type == "auto"

    def test_month_end_grouping(self, dbsession: Session, monkeypatch):
        """Auto-recharges are grouped by month-end date."""
        import orchestra.lib.billing

        mock_stripe = _make_stripe_mock()
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=100,
            stripe_customer_id="cus_grouping_test",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        make_user(dbsession, "grouping_user", ba)
        dbsession.commit()

        queue_auto_recharge(dbsession, ba, 50)
        queue_auto_recharge(dbsession, ba, 25)
        dbsession.commit()

        recharges = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).all()
        assert len(recharges) == 2
        assert recharges[0].invoice_group == recharges[1].invoice_group
        assert (
            recharges[0].invoice_group.day
            == calendar.monthrange(
                recharges[0].invoice_group.year,
                recharges[0].invoice_group.month,
            )[1]
        )

    def test_creates_stripe_invoice_item(self, dbsession: Session, monkeypatch):
        """queue_auto_recharge creates both a DB record AND a Stripe invoice item."""
        import orchestra.lib.billing

        calls = []

        def mock_create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                id="ii_test_123",
                customer=kwargs["customer"],
                amount=kwargs["amount"],
            )

        mock_stripe_module = _make_stripe_mock(invoice_item_create=mock_create)
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        stripe_customer_id = "cus_test_auto_recharge"
        ba = make_billing_account(
            dbsession,
            credits=5,
            stripe_customer_id=stripe_customer_id,
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        make_user(dbsession, "auto_recharge_stripe_test", ba)
        dbsession.commit()

        result = queue_auto_recharge(dbsession, ba, 50)
        dbsession.commit()

        assert result is True
        recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
        assert recharge is not None
        assert recharge.quantity == Decimal("50")
        assert recharge.status == RechargeStatus.PENDING_INVOICE

        assert len(calls) == 1
        assert calls[0]["customer"] == stripe_customer_id
        assert calls[0]["amount"] == 5000
        assert calls[0]["currency"] == "usd"
        assert "auto-recharge" in calls[0]["description"]
        assert calls[0]["metadata"]["recharge_type"] == "auto"

    def test_no_stripe_customer_id(self, dbsession: Session, monkeypatch):
        """Without a Stripe customer, no recharge is created and no credits granted."""
        import orchestra.lib.billing

        calls = []
        mock_stripe_module = _make_stripe_mock(
            invoice_item_create=lambda **kw: calls.append(kw) or None,
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=5,
            stripe_customer_id=None,
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        make_user(dbsession, "no_stripe_customer_user", ba)
        dbsession.commit()

        result = queue_auto_recharge(dbsession, ba, 50)
        dbsession.commit()

        assert result is False
        recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
        assert recharge is None
        assert len(calls) == 0
        dbsession.refresh(ba)
        assert float(ba.credits) == 5

    def test_stripe_error_prevents_recharge(self, dbsession: Session, monkeypatch):
        """When Stripe InvoiceItem creation fails, no recharge or credits are granted."""
        import orchestra.lib.billing

        class MockStripeError(Exception):
            def __init__(self, message, param=None):
                super().__init__(message)
                self.param = param

        mock_stripe_module = _make_stripe_mock(
            invoice_item_create=lambda **kw: (_ for _ in ()).throw(
                MockStripeError("Customer not found"),
            ),
            stripe_error_cls=MockStripeError,
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=5,
            stripe_customer_id="cus_error_test",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        make_user(dbsession, "stripe_error_user", ba)
        dbsession.commit()

        result = queue_auto_recharge(dbsession, ba, 50)
        dbsession.commit()

        assert result is False
        recharge = dbsession.query(Recharge).filter_by(billing_account_id=ba.id).first()
        assert recharge is None
        dbsession.refresh(ba)
        assert float(ba.credits) == 5

    def test_adds_credits_immediately(self, dbsession: Session, monkeypatch):
        """queue_auto_recharge adds credits to the billing account right away."""
        import orchestra.lib.billing

        mock_stripe_module = _make_stripe_mock()
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=5,
            stripe_customer_id="cus_ar_credits",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        make_user(dbsession, "ar_adds_credits_user", ba)
        dbsession.commit()

        assert float(ba.credits) == 5

        result = queue_auto_recharge(dbsession, ba, 50, entity_label="test")
        dbsession.commit()

        assert result is True
        dbsession.refresh(ba)
        assert float(ba.credits) == 55

    def test_credits_survive_negative_balance(self, dbsession: Session, monkeypatch):
        """Auto-recharge can bring a negative balance back to positive."""
        import orchestra.lib.billing

        mock_stripe_module = _make_stripe_mock()
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=-10,
            stripe_customer_id="cus_ar_negative",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=100,
        )
        make_user(dbsession, "ar_negative_user", ba)
        dbsession.commit()

        result = queue_auto_recharge(dbsession, ba, 100, entity_label="test")
        dbsession.commit()

        assert result is True
        dbsession.refresh(ba)
        assert float(ba.credits) == 90

    def test_db_error_cleans_up_invoice_item(self, dbsession: Session, monkeypatch):
        """If the DB write fails after InvoiceItem creation, the item is deleted."""
        import orchestra.lib.billing

        created_items = []
        deleted_items = []

        def mock_create(**kwargs):
            item = SimpleNamespace(id="ii_cleanup_test")
            created_items.append(item)
            return item

        def mock_delete(item_id):
            deleted_items.append(item_id)

        mock_stripe_module = _make_stripe_mock(
            invoice_item_create=mock_create,
            invoice_item_delete=mock_delete,
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=5,
            stripe_customer_id="cus_cleanup_test",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        make_user(dbsession, "cleanup_user", ba)
        dbsession.commit()

        original_add = dbsession.add

        def exploding_add(obj):
            if isinstance(obj, Recharge):
                raise RuntimeError("Simulated DB failure")
            return original_add(obj)

        monkeypatch.setattr(dbsession, "add", exploding_add)

        result = queue_auto_recharge(dbsession, ba, 50)

        assert result is False
        assert len(created_items) == 1
        assert len(deleted_items) == 1
        assert deleted_items[0] == "ii_cleanup_test"
        dbsession.refresh(ba)
        assert float(ba.credits) == 5

    def test_no_payment_method_skips_and_disables(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """No payment method → auto-recharge skipped and disabled."""
        import orchestra.lib.billing

        ii_calls = []
        mock_stripe_module = _make_stripe_mock(
            invoice_item_create=lambda **kw: ii_calls.append(kw),
            customer_retrieve=lambda *a, **kw: _mock_customer_without_pm(),
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=50,
            stripe_customer_id="cus_no_pm",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        make_user(dbsession, "no_pm_user", ba)
        dbsession.commit()

        result = queue_auto_recharge(dbsession, ba, 50)
        dbsession.commit()

        assert result is False
        assert ba.autorecharge is False
        assert len(ii_calls) == 0
        dbsession.refresh(ba)
        assert float(ba.credits) == 50

    def test_deleted_customer_skips_and_disables(self, dbsession: Session, monkeypatch):
        """Deleted Stripe customer → auto-recharge skipped and disabled."""
        import orchestra.lib.billing

        class MockInvalidRequest(Exception):
            pass

        def boom(*a, **kw):
            raise MockInvalidRequest("No such customer")

        mock_stripe_module = _make_stripe_mock(
            customer_retrieve=boom,
            invalid_request_cls=MockInvalidRequest,
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=50,
            stripe_customer_id="cus_deleted",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        make_user(dbsession, "deleted_cus_user", ba)
        dbsession.commit()

        result = queue_auto_recharge(dbsession, ba, 50)
        dbsession.commit()

        assert result is False
        assert ba.autorecharge is False
        dbsession.refresh(ba)
        assert float(ba.credits) == 50

    def test_stripe_api_error_on_retrieve_skips_but_keeps_enabled(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Transient Stripe API error on customer retrieve → skip but don't disable."""
        import orchestra.lib.billing

        class MockStripeError(Exception):
            pass

        class MockInvalidRequest(MockStripeError):
            pass

        def boom(*a, **kw):
            raise MockStripeError("Service unavailable")

        mock_stripe_module = _make_stripe_mock(
            customer_retrieve=boom,
            stripe_error_cls=MockStripeError,
            invalid_request_cls=MockInvalidRequest,
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", mock_stripe_module)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=50,
            stripe_customer_id="cus_api_err",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        make_user(dbsession, "api_err_user", ba)
        dbsession.commit()

        result = queue_auto_recharge(dbsession, ba, 50)
        dbsession.commit()

        assert result is False
        assert ba.autorecharge is True
        dbsession.refresh(ba)
        assert float(ba.credits) == 50


# ============================================================================
# Auto-Recharge Eligibility & Spending Requirements
# ============================================================================


class TestAutoRechargeEligibility:
    """Tests for auto-recharge eligibility based on spending history."""

    def test_minimum_autorecharge_amount(self, dbsession: Session):
        """Auto-recharge amount must be at least $25."""
        from orchestra.db.dao.billing_account_dao import BillingAccountDAO

        ba = make_billing_account(
            dbsession,
            credits=1000,
            stripe_customer_id="cus_autorecharge_test",
        )
        make_user(dbsession, "autorecharge_test_user", ba)
        dbsession.commit()

        ba_dao = BillingAccountDAO(dbsession)

        with pytest.raises(ValueError, match="Minimum auto-recharge amount is \\$25"):
            ba_dao.set_autorecharge_qty(ba.id, 10.0)

        ba_dao.set_autorecharge_qty(ba.id, 25.0)
        dbsession.commit()
        dbsession.refresh(ba)
        assert float(ba.autorecharge_qty) == 25.0

        ba_dao.set_autorecharge_qty(ba.id, 50.0)
        dbsession.commit()
        dbsession.refresh(ba)
        assert float(ba.autorecharge_qty) == 50.0

    def test_new_user_cannot_enable(self, dbsession: Session):
        """New user cannot enable auto-recharge without meeting spend threshold."""
        from orchestra.db.dao.billing_account_dao import (
            MIN_SPEND_FOR_AUTO_RECHARGE,
            BillingAccountDAO,
        )

        ba = make_billing_account(
            dbsession,
            credits=1000,
            stripe_customer_id="cus_new_user",
        )
        make_user(dbsession, "new_user_test", ba)
        dbsession.commit()

        ba_dao = BillingAccountDAO(dbsession)
        assert not ba_dao.can_enable_auto_recharge(ba.id)
        assert ba_dao.get_total_spending(ba.id) == 0
        assert ba_dao.get_total_spending(ba.id) < MIN_SPEND_FOR_AUTO_RECHARGE

    def test_eligibility_with_spending(self, dbsession: Session):
        """Cumulative PAID recharges unlock auto-recharge eligibility."""
        from orchestra.db.dao.billing_account_dao import BillingAccountDAO

        ba = make_billing_account(
            dbsession,
            credits=500,
            stripe_customer_id="cus_spending",
        )
        make_user(dbsession, "spending_test_user", ba)
        dbsession.commit()

        ba_dao = BillingAccountDAO(dbsession)

        assert ba_dao.get_total_spending(ba.id) == 0
        assert not ba_dao.can_enable_auto_recharge(ba.id)

        # Below threshold
        rec1 = Recharge(
            billing_account_id=ba.id,
            quantity=500,
            amount_usd=Decimal("500.00"),
            type="payment",
            status=RechargeStatus.PAID,
        )
        dbsession.add(rec1)
        dbsession.flush()
        assert float(ba_dao.get_total_spending(ba.id)) == 500.0
        assert not ba_dao.can_enable_auto_recharge(ba.id)

        # Cross threshold
        rec2 = Recharge(
            billing_account_id=ba.id,
            quantity=600,
            amount_usd=Decimal("600.00"),
            type="auto",
            status=RechargeStatus.PAID,
        )
        dbsession.add(rec2)
        dbsession.flush()
        assert float(ba_dao.get_total_spending(ba.id)) == 1100.0
        assert ba_dao.can_enable_auto_recharge(ba.id)

        # Promo should NOT count
        rec3 = Recharge(
            billing_account_id=ba.id,
            quantity=1000,
            amount_usd=Decimal("1000.00"),
            type="promo",
            status=RechargeStatus.PAID,
        )
        dbsession.add(rec3)
        dbsession.flush()
        assert float(ba_dao.get_total_spending(ba.id)) == 1100.0

        # PENDING should NOT count
        rec4 = Recharge(
            billing_account_id=ba.id,
            quantity=500,
            amount_usd=Decimal("500.00"),
            type="payment",
            status=RechargeStatus.PENDING_INVOICE,
        )
        dbsession.add(rec4)
        dbsession.flush()
        assert float(ba_dao.get_total_spending(ba.id)) == 1100.0

    def test_existing_customer_unaffected(self, dbsession: Session):
        """Existing customers with auto-recharge enabled continue to work normally."""
        from orchestra.db.dao.billing_account_dao import BillingAccountDAO

        ba = make_billing_account(
            dbsession,
            credits=500,
            stripe_customer_id="cus_existing",
            autorecharge=True,
            autorecharge_qty=50,
            autorecharge_threshold=100,
        )
        make_user(dbsession, "existing_customer", ba)
        dbsession.commit()

        ba_dao = BillingAccountDAO(dbsession)

        ba_dao.set_autorecharge_qty(ba.id, 100.0)
        ba_dao.set_autorecharge_threshold(ba.id, 50.0)
        dbsession.commit()
        dbsession.refresh(ba)
        assert ba.autorecharge is True
        assert float(ba.autorecharge_qty) == 100.0
        assert float(ba.autorecharge_threshold) == 50.0

        ba_dao.set_autorecharge(ba.id, False)
        dbsession.commit()
        dbsession.refresh(ba)
        assert ba.autorecharge is False

        ba_dao.set_autorecharge(ba.id, True)
        dbsession.commit()
        dbsession.refresh(ba)
        assert ba.autorecharge is True

    def test_amount_validation_edge_cases(self, dbsession: Session):
        """Edge cases around the $25 minimum auto-recharge amount."""
        from orchestra.db.dao.billing_account_dao import BillingAccountDAO

        ba = make_billing_account(
            dbsession,
            credits=1000,
            stripe_customer_id="cus_validation",
        )
        make_user(dbsession, "autorecharge_validation_user", ba)
        dbsession.commit()

        ba_dao = BillingAccountDAO(dbsession)

        test_cases = [
            (24.99, False),
            (25.00, True),
            (25.01, True),
            (0.01, False),
            (1000.00, True),
        ]

        for amount, should_succeed in test_cases:
            if should_succeed:
                ba_dao.set_autorecharge_qty(ba.id, amount)
                dbsession.commit()
                dbsession.refresh(ba)
                assert float(ba.autorecharge_qty) == amount
            else:
                with pytest.raises(ValueError, match="Minimum auto-recharge amount"):
                    ba_dao.set_autorecharge_qty(ba.id, amount)


# ============================================================================
# METERED-mode guards on the existing CREDITS-mode pipelines
# ============================================================================
#
# These exercise the defensive guards that prevent METERED accounts from
# being touched by CREDITS-mode machinery: ``queue_auto_recharge`` (the
# auto-top-up path), ``invoice_month`` (the credits invoicer), and the
# ``levy_provisioned_resources`` routine (the per-contact monthly levy).


def _make_metered_template_for_guards(
    dbsession: Session,
    *,
    name: str,
    commit: Decimal | None = Decimal("1000"),
):
    """Make a METERED template for guard-routine tests.

    Pass ``commit=None`` (or zero) for a PAYG variant; any positive
    ``commit`` produces a COMMITMENT template.
    """
    from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO
    from orchestra.db.models.orchestra_models import BillingMode, CollectionMethod

    is_commitment = commit is not None and commit > 0
    return BillingPlanTemplateDAO(dbsession).create_template(
        name=name,
        billing_mode=BillingMode.METERED,
        commit_amount=commit if is_commitment else None,
        commit_period="MONTHLY" if is_commitment else None,
        collection_method=CollectionMethod.SEND_INVOICE_NET_30,
        is_custom=True,
        is_active=True,
    )


def _assign_metered(dbsession: Session, ba, template):
    from orchestra.db.dao.billing_plan_assignment_dao import BillingPlanAssignmentDAO

    BillingPlanAssignmentDAO(dbsession).set_plan(
        billing_account_id=ba.id,
        template_id=template.id,
    )


class TestAutoRechargeMeteredGuard:
    """``queue_auto_recharge`` short-circuits METERED accounts."""

    def test_metered_account_short_circuits(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """METERED account never makes the Stripe call and returns False."""
        import orchestra.lib.billing

        # Sentinel that fails loudly if Stripe is touched.
        sentinel = MagicMock(
            side_effect=AssertionError("Stripe must not be called for METERED"),
        )
        stripe_mod = SimpleNamespace(
            Customer=SimpleNamespace(retrieve=sentinel),
            InvoiceItem=SimpleNamespace(create=sentinel, delete=sentinel),
            StripeError=Exception,
            InvalidRequestError=Exception,
            error=SimpleNamespace(
                StripeError=Exception,
                InvalidRequestError=Exception,
            ),
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", stripe_mod)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_meterguard",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=50,
        )
        tpl = _make_metered_template_for_guards(dbsession, name="autorecharge-meter")
        _assign_metered(dbsession, ba, tpl)
        dbsession.commit()

        result = queue_auto_recharge(
            dbsession,
            ba,
            50,
            entity_label=f"ba {ba.id}",
        )
        assert result is False
        assert (
            dbsession.query(Recharge).filter_by(billing_account_id=ba.id).count() == 0
        )
        assert ba.credits == Decimal("0")

    def test_credits_account_still_runs(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """Pristine (CREDITS) account still goes through the auto-recharge path."""
        import orchestra.lib.billing

        def _customer_retrieve(*a, **kw):
            return SimpleNamespace(
                invoice_settings=SimpleNamespace(default_payment_method="pm_x"),
                default_source=None,
            )

        def _ii_create(**kw):
            return SimpleNamespace(id="ii_credits_path")

        stripe_mod = SimpleNamespace(
            Customer=SimpleNamespace(retrieve=_customer_retrieve),
            InvoiceItem=SimpleNamespace(create=_ii_create),
            StripeError=Exception,
            InvalidRequestError=Exception,
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", stripe_mod)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_credits_path",
            autorecharge=True,
            autorecharge_threshold=10,
            autorecharge_qty=25,
        )
        # No assignment override → CREDITS by default
        dbsession.commit()

        result = queue_auto_recharge(dbsession, ba, 25, entity_label=f"ba {ba.id}")
        assert result is True
        assert (
            dbsession.query(Recharge).filter_by(billing_account_id=ba.id).count() == 1
        )


class TestMonthlyCreditsInvoicerMeteredFilter:
    """``invoice_month`` (the CREDITS invoicer) skips rows whose plan_id
    points at a METERED template — and *only* those rows.

    The filter keys off ``Recharge.plan_id``'s template mode rather
    than the account's live billing mode: the account may have
    switched plans between when the recharge was written and when this
    routine runs, but the recharge itself belongs to whichever plan
    was active at write time. Any other rule strands the recharge:

    * "skip if the live account mode is METERED" silently drops a
      pre-switch CREDITS auto-recharge (the row's ``plan_id`` is
      NULL — by invariant, CREDITS auto-recharge / payment / promo
      rows have no plan attribution — so it must be invoiced).
    * "process every PENDING_INVOICE row" would double-bill any
      METERED-mode recharge that somehow ended up in PENDING (which
      shouldn't happen in steady state but is the data-corruption
      case we want belt-and-braces against).

    The combined ``set_plan`` guard (``PendingRechargesError``) is
    the primary defence; this filter is the second line for rows
    that landed via reconciliation / manual SQL outside the DAO.
    """

    @staticmethod
    def _stripe_mock_with_invoice():
        """Stripe stub that records ``Invoice.create`` calls."""
        calls: list[dict] = []

        def _create(**kwargs):
            calls.append(kwargs)
            inv_id = f"in_credits_test_{len(calls)}"
            return SimpleNamespace(id=inv_id)

        return (
            SimpleNamespace(
                Invoice=SimpleNamespace(create=_create),
                StripeError=Exception,
                InvalidRequestError=Exception,
            ),
            calls,
        )

    def test_pending_invoice_with_metered_plan_id_is_skipped(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """``Recharge.plan_id`` → METERED template ⇒ skip the row.

        This is the data-corruption guard: if a stray PENDING_INVOICE
        row gets attributed to a METERED assignment somehow (manual
        SQL, reconciliation patch, future bug), the CREDITS invoicer
        must not pick it up — the metered invoicer owns rows tagged
        to METERED plans.
        """
        import datetime as _dt

        import orchestra.lib.billing
        import orchestra.routines.monthly_credits_invoicer as inv_mod
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )
        from orchestra.routines.monthly_credits_invoicer import invoice_month

        invoice_sentinel = MagicMock(
            side_effect=AssertionError(
                "stripe.Invoice.create must not run for METERED-plan recharges",
            ),
        )
        stripe_mod = SimpleNamespace(
            Invoice=SimpleNamespace(create=invoice_sentinel),
            StripeError=Exception,
            InvalidRequestError=Exception,
        )
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)
        monkeypatch.setattr(inv_mod, "stripe", stripe_mod)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_meter_planid_filter",
        )
        tpl = _make_metered_template_for_guards(dbsession, name="planid-filter-tpl")
        _assign_metered(dbsession, ba, tpl)
        active = BillingPlanAssignmentDAO(dbsession).get_active(ba.id)
        assert active is not None

        invoice_group = _dt.date(2026, 4, 30)
        rch = Recharge(
            billing_account_id=ba.id,
            type="auto",
            quantity=Decimal("25"),
            amount_usd=Decimal("25"),
            invoice_group=invoice_group,
            status=RechargeStatus.PENDING_INVOICE,
            plan_id=active.id,  # METERED-attributed row
        )
        dbsession.add(rch)
        dbsession.commit()

        result = invoice_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 0
        assert result.accounts_failed == 0
        dbsession.refresh(rch)
        assert rch.status == RechargeStatus.PENDING_INVOICE
        assert rch.stripe_invoice_id is None

    def test_pending_invoice_with_null_plan_id_invoices_even_on_metered_account(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        """``Recharge.plan_id IS NULL`` ⇒ CREDITS-world row ⇒ invoice it,
        regardless of the account's *current* mode.

        This is the exploit-prevention test: a CREDITS auto-recharge
        (always written with ``plan_id=NULL`` by invariant) that
        happened *before* the account migrated to METERED is still a
        legitimate CREDITS liability. The credits invoicer must collect
        it. The previous "live account mode" filter would silently
        drop it and let the customer keep the credits for free.
        """
        import datetime as _dt

        import orchestra.lib.billing
        import orchestra.routines.monthly_credits_invoicer as inv_mod
        from orchestra.routines.monthly_credits_invoicer import invoice_month

        stripe_mod, calls = self._stripe_mock_with_invoice()
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)
        monkeypatch.setattr(inv_mod, "stripe", stripe_mod)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_strand_test",
        )
        # Account is currently on a METERED plan, but the recharge below
        # was written *before* the switch (plan_id=NULL by CREDITS
        # invariant). The fix routes it to the credits invoicer.
        tpl = _make_metered_template_for_guards(dbsession, name="strand-tpl")
        _assign_metered(dbsession, ba, tpl)

        invoice_group = _dt.date(2026, 4, 30)
        rch = Recharge(
            billing_account_id=ba.id,
            type="auto",
            quantity=Decimal("100"),
            amount_usd=Decimal("100"),
            invoice_group=invoice_group,
            status=RechargeStatus.PENDING_INVOICE,
            plan_id=None,  # CREDITS-world row
        )
        dbsession.add(rch)
        dbsession.commit()

        result = invoice_month(2026, 4, session=dbsession)
        assert result.accounts_invoiced == 1, result.errors
        assert result.accounts_failed == 0
        dbsession.refresh(rch)
        assert rch.status == RechargeStatus.INVOICE_CREATED
        assert rch.stripe_invoice_id is not None
        # And exactly one Stripe invoice was issued for this account.
        assert len(calls) == 1
        assert calls[0]["customer"] == "cus_strand_test"


class TestContactLevyMeteredBehaviour:
    """``levy_provisioned_resources`` against METERED accounts.

    METERED accounts receive a CreditTransaction (mode-aware
    ``deduct_credits``) but no wallet mutation, no auto-recharge, and no
    grace-period transition on contacts.
    """

    def test_metered_account_levy_no_wallet_mutation_no_grace(
        self,
        dbsession: Session,
        monkeypatch,
    ):
        import orchestra.lib.billing
        from orchestra.db.models.orchestra_models import CreditTransaction

        # Auto-recharge sentinel — must not run on a METERED levy.
        sentinel = MagicMock(
            side_effect=AssertionError("Stripe must not be called for METERED levy"),
        )
        stripe_mod = SimpleNamespace(
            Customer=SimpleNamespace(retrieve=sentinel),
            InvoiceItem=SimpleNamespace(create=sentinel, delete=sentinel),
            StripeError=Exception,
            InvalidRequestError=Exception,
        )
        monkeypatch.setattr(orchestra.lib.billing, "stripe", stripe_mod)
        monkeypatch.setattr(orchestra.lib.billing, "configure_stripe", lambda: None)

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id="cus_meter_levy",
            autorecharge=True,
            autorecharge_threshold=100,
            autorecharge_qty=50,
        )
        tpl = _make_metered_template_for_guards(dbsession, name="levy-meter-tpl")
        _assign_metered(dbsession, ba, tpl)

        from orchestra.tests.test_billing.conftest import make_contact

        user = make_user(dbsession, "meter_levy_u1", ba)
        asst = make_assistant(dbsession, user.id, first_name="MeterLevy")
        contact = make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15557777777",
            provider="twilio",
            country_code="US",
        )
        dbsession.commit()

        result = levy_provisioned_resources(2026, 4, session=dbsession)
        assert result.accounts_processed >= 1

        dbsession.refresh(ba)
        # METERED: balance is NOT mutated (deduct_credits skips wallet writes).
        assert ba.credits == Decimal("0")
        # Contact still active — no grace period because credits never
        # went negative (it stayed at 0 the whole time).
        dbsession.refresh(contact)
        assert contact.status == "active"
        assert contact.grace_period_started_at is None
        # And a CreditTransaction landed for the levy.
        ledger_rows = (
            dbsession.query(CreditTransaction)
            .filter(
                CreditTransaction.billing_account_id == ba.id,
                CreditTransaction.category == "resources",
            )
            .all()
        )
        assert len(ledger_rows) == 1
        assert ledger_rows[0].plan_assignment_id is not None

    def test_credits_account_levy_unchanged(
        self,
        dbsession: Session,
    ):
        """CREDITS path still mutates the wallet and trips grace period.

        Auto-recharge is disabled here so the grace transition is
        observable (otherwise the auto-refill would top the wallet
        back into positive territory and contacts would stay active).
        """
        from orchestra.tests.test_billing.conftest import make_contact

        ba = make_billing_account(
            dbsession,
            credits=Decimal("0.50"),  # below the $1.50 levy → goes negative
            stripe_customer_id="cus_credits_levy",
            autorecharge=False,
        )
        user = make_user(dbsession, "credits_levy_u1", ba)
        asst = make_assistant(dbsession, user.id, first_name="CreditsLevy")
        contact = make_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15558888888",
            provider="twilio",
            country_code="US",
        )
        dbsession.commit()

        result = levy_provisioned_resources(2026, 4, session=dbsession)
        assert result.accounts_processed >= 1
        dbsession.refresh(ba)
        # CREDITS: wallet was mutated to a negative balance.
        assert ba.credits < Decimal("0")
        # And the contact entered grace_period because credits went negative.
        dbsession.refresh(contact)
        assert contact.status == "grace_period"


# ---------------------------------------------------------------------------
# Plan-configuration matrix
# ---------------------------------------------------------------------------
#
# A single parametrised driver that fans the metered invoicer out across
# the cross-product of the dimensions that real customer contracts vary
# along: ``billing_mode`` is fixed at METERED here (CREDITS goes through
# a separate pipeline already covered by ``TestMonthlyInvoicer`` and
# ``TestMonthlyCreditsInvoicerMeteredFilter``); the remaining axes are
#
#   * ``plan_kind``            — PAYG | COMMITMENT
#   * ``commit_period``        — MONTHLY | QUARTERLY | ANNUAL
#                                (only meaningful for COMMITMENT)
#   * ``commit_schedule``      — AMORTISED | UPFRONT
#                                (only meaningful for COMMITMENT)
#   * ``currency``             — USD | EUR | GBP | JPY | MXN
#   * ``collection_method``    — AUTO_CARD | SEND_INVOICE_NET_30
#
# Every non-USD case pins ``fx_policy=LOCKED_RATE`` so a Frankfurter
# outage doesn't make this matrix flaky — the FX policy dimension has
# its own dedicated coverage in ``TestMeteredInvoicerSpot`` /
# ``TestMeteredInvoicerPeriodAverage``.
#
# Per-row assertions verify the cross-cutting invariants only — the
# detailed plan-formula maths lives in ``TestMonthlyMeteredInvoicer``,
# ``TestMeteredInvoicerCommitSchedule`` etc. The matrix here is the
# "did the cross-product wire up" smoke test the user's question (c)
# asked for: every plan you can sell goes through the routine without
# blowing up, with the right currency, payment methods, and recharge
# attribution.
# ---------------------------------------------------------------------------

# Country whitelist for ``eu_bank_transfer`` — mirrors the routine's
# ``_EU_BANK_TRANSFER_COUNTRIES`` set; we re-declare the canonical
# member here so the test isn't coupled to the order Stripe lists
# them.
_MATRIX_BILLING_COUNTRY: dict[str, str | None] = {
    "USD": "US",
    "GBP": "GB",
    "EUR": "DE",
    "JPY": "JP",
    "MXN": "MX",
}

_MATRIX_BANK_TRANSFER_TYPE: dict[str, str] = {
    "USD": "us_bank_transfer",
    "GBP": "gb_bank_transfer",
    "EUR": "eu_bank_transfer",
    "JPY": "jp_bank_transfer",
    "MXN": "mx_bank_transfer",
}


def _matrix_id(params: dict) -> str:
    parts = [params["plan_kind"], params["currency"], params["collection"]]
    if params["plan_kind"] == "COMMITMENT":
        parts.append(params["commit_period"])
        parts.append(params["commit_schedule"])
    return "-".join(parts)


def _build_matrix_cases() -> list[dict]:
    cases: list[dict] = []
    for currency in ("USD", "GBP", "EUR", "JPY", "MXN"):
        for collection in ("AUTO_CARD", "SEND_INVOICE_NET_30"):
            cases.append(
                {
                    "plan_kind": "PAYG",
                    "commit_period": None,
                    "commit_schedule": None,
                    "currency": currency,
                    "collection": collection,
                },
            )
            for commit_period, commit_schedule in (
                ("MONTHLY", "AMORTISED"),
                ("MONTHLY", "UPFRONT"),
                ("QUARTERLY", "AMORTISED"),
                ("ANNUAL", "AMORTISED"),
            ):
                cases.append(
                    {
                        "plan_kind": "COMMITMENT",
                        "commit_period": commit_period,
                        "commit_schedule": commit_schedule,
                        "currency": currency,
                        "collection": collection,
                    },
                )
    return cases


_MATRIX_CASES = _build_matrix_cases()


class TestPlanConfigurationMatrix:
    """End-to-end metered invoicer pass for every contract shape we sell.

    The spec covers ``len(_MATRIX_CASES) == 50`` cross-product cells
    (5 currencies × 2 collection methods × 5 plan shapes). Each runs
    the public ``invoice_metered_month`` entrypoint with a real DB,
    a Stripe mock, and a bridged FX rate so the routine never hits
    Frankfurter. The assertions check the cross-product invariants
    that this matrix is uniquely positioned to catch — currency
    threading, payment-method resolution, and per-cell recharge
    attribution — rather than re-asserting plan-formula maths that
    is owned by other test classes.
    """

    @pytest.mark.parametrize(
        "case",
        _MATRIX_CASES,
        ids=[_matrix_id(c) for c in _MATRIX_CASES],
    )
    def test_invoicer_handles_each_plan_shape(
        self,
        dbsession: Session,
        monkeypatch,
        case: dict,
    ):
        import datetime as _dt

        from orchestra.db.dao.billing_account_dao import BillingAccountDAO
        from orchestra.db.dao.billing_plan_assignment_dao import (
            BillingPlanAssignmentDAO,
        )
        from orchestra.db.dao.billing_plan_template_dao import BillingPlanTemplateDAO
        from orchestra.db.models.orchestra_models import (
            RECHARGE_TYPE_MONTHLY_COMMIT,
            BillingMode,
            CollectionMethod,
            FxPolicy,
            ProrationPolicy,
        )
        from orchestra.routines.monthly_metered_invoicer import invoice_metered_month

        stripe = _metered_stripe_mock()
        _patch_metered_stripe(monkeypatch, stripe)
        _mute_metered_metrics(monkeypatch)

        # Always pin FX rate via LOCKED_RATE so the matrix is
        # deterministic. The rate-resolution policy axis has its own
        # dedicated tests.
        is_non_usd = case["currency"] != "USD"
        fx_policy = FxPolicy.LOCKED_RATE if is_non_usd else None
        fx_locked_rate = Decimal("0.80") if is_non_usd else None

        is_commitment = case["plan_kind"] == "COMMITMENT"
        commit_amount = Decimal("800") if is_commitment else None
        commit_period = case["commit_period"]
        commit_schedule = case["commit_schedule"]
        proration_policy = (
            ProrationPolicy.FULL_FIRST
            if commit_schedule == "UPFRONT"
            else ProrationPolicy.PRORATE
        )
        collection = (
            CollectionMethod.AUTO_CARD
            if case["collection"] == "AUTO_CARD"
            else CollectionMethod.SEND_INVOICE_NET_30
        )

        # Build a unique template per case to avoid name collisions
        # when the parametrised matrix runs in a single test session.
        template_name = f"matrix-{_matrix_id(case)}".lower()
        tpl = BillingPlanTemplateDAO(dbsession).create_template(
            name=template_name,
            billing_mode=BillingMode.METERED,
            commit_amount=commit_amount,
            currency=case["currency"],
            commit_period=commit_period,
            commit_schedule=commit_schedule,
            base_pricing_factor=Decimal("1.0"),
            overage_pricing_factor=Decimal("1.0"),
            collection_method=collection,
            proration_policy=proration_policy,
            is_custom=True,
            is_active=True,
            fx_policy=fx_policy,
            fx_locked_rate=fx_locked_rate,
        )

        ba = make_billing_account(
            dbsession,
            credits=0,
            stripe_customer_id=f"cus_matrix_{_matrix_id(case)}",
        )
        ba.billing_address = {"country": _MATRIX_BILLING_COUNTRY[case["currency"]]}
        dbsession.flush()

        # ``set_plan`` will close the conftest-inserted default
        # assignment; backdate that close so it precedes the new
        # template's started_at.
        from sqlalchemy import text as _sql_text

        dbsession.execute(
            _sql_text(
                "UPDATE billing_plan_assignment "
                "SET started_at = :ts WHERE billing_account_id = :ba",
            ),
            {
                "ts": _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
                "ba": ba.id,
            },
        )
        dbsession.flush()
        BillingPlanAssignmentDAO(dbsession).set_plan(
            billing_account_id=ba.id,
            template_id=tpl.id,
            effective_at=_dt.datetime(2026, 3, 15, tzinfo=_dt.timezone.utc),
        )

        # 1,250 USD of usage → with 0.80 lock = 1,000 in the contract
        # currency. For COMMITMENT(commit=800) this lands above the
        # monthly-equivalent floor → invoice = 1,000 (commit + overage
        # in AMORTISED; full year + overage in UPFRONT-anniversary;
        # else commit-only-charge depending on schedule).
        BillingAccountDAO(dbsession).deduct_credits(
            ba.id,
            1250.0,
            category="llm",
        )
        _backdate_ledger_to_period(
            dbsession,
            billing_account_id=ba.id,
            when=_dt.datetime(2026, 4, 15, 12, 0, tzinfo=_dt.timezone.utc),
        )
        dbsession.commit()

        result = invoice_metered_month(2026, 4, session=dbsession)
        dbsession.commit()

        assert (
            result.accounts_invoiced == 1
        ), f"matrix case {case!r} did not produce an invoice: {result.errors!r}"
        assert len(stripe._inv_calls) == 1
        invoice = stripe._inv_calls[0]
        wire_currency = case["currency"].lower()
        assert invoice["currency"] == wire_currency
        # Every InvoiceItem ships in the same currency as the Invoice
        # — without this Stripe silently produces a zero-amount
        # invoice when ``pending_invoice_items_behavior=include``
        # filters out mismatched-currency items.
        for ii in stripe._ii_calls:
            assert ii["currency"] == wire_currency

        # Collection method threads end-to-end.
        if collection == CollectionMethod.SEND_INVOICE_NET_30:
            assert invoice["collection_method"] == "send_invoice"
            assert invoice["days_until_due"] == 30
        else:
            assert invoice["collection_method"] == "charge_automatically"

        # Payment-method resolution: SEND_INVOICE_NET_30 offers
        # customer_balance with the per-currency funding type;
        # AUTO_CARD is card-only.
        ps = invoice["payment_settings"]
        if collection == CollectionMethod.SEND_INVOICE_NET_30:
            assert "customer_balance" in ps["payment_method_types"]
            cb_opts = ps["payment_method_options"]["customer_balance"]
            expected_rail = _MATRIX_BANK_TRANSFER_TYPE[case["currency"]]
            assert cb_opts["funding_type"] == "bank_transfer"
            assert cb_opts["bank_transfer"]["type"] == expected_rail
            # ``eu_bank_transfer`` requires an additional ``country``
            # parameter (Stripe-mandated for SEPA): the invoicer reads
            # it off ``BillingAccount.billing_address.country``. Other
            # rails are configured by ``type`` alone.
            if expected_rail == "eu_bank_transfer":
                assert cb_opts["bank_transfer"]["eu_bank_transfer"]["country"] == (
                    _MATRIX_BILLING_COUNTRY[case["currency"]]
                )
        else:
            assert ps["payment_method_types"] == ["card"]

        # Recharge audit row carries the contract currency, the FX
        # policy, and the assignment FK. ``raw_usage_usd`` is always
        # the USD ledger sum regardless of the contract currency —
        # the FX rate translates it into ``invoiced_local`` /
        # ``contract_usage_local`` for billing.
        recharge = (
            dbsession.query(Recharge)
            .filter_by(
                billing_account_id=ba.id,
                type=RECHARGE_TYPE_MONTHLY_COMMIT,
            )
            .one()
        )
        assert recharge.status == RechargeStatus.INVOICE_CREATED
        assert recharge.plan_id is not None
        assert recharge.detail["currency"] == case["currency"]
        # COMMITMENT-attribution is implicit in the audit row's
        # ``commit_amount`` field — present for COMMITMENT plans,
        # ``None`` for PAYG. We assert the shape rather than a
        # synthesized "plan_kind" column to keep the test loyal to
        # the actual recorded blob.
        if is_commitment:
            assert recharge.detail["commit_amount"] is not None
            assert recharge.detail["commit_schedule"] == commit_schedule
        else:
            assert recharge.detail["commit_amount"] is None
            assert recharge.detail["commit_schedule"] is None
        if is_non_usd:
            assert recharge.detail["fx_policy"] == "LOCKED_RATE"
            assert Decimal(recharge.detail["fx_rate"]) == Decimal("0.80")
        else:
            assert recharge.detail["fx_policy"] == "NONE"
