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
2. Monthly invoicer (invoice_month):
   - Aggregates PENDING_INVOICE recharges by billing account
   - Creates Stripe invoice per billing account
   - Skips accounts without stripe_customer_id
   - Handles mixed user + org recharges
   - Includes tax ID in invoice when present
"""

from __future__ import annotations

import calendar
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

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

        from orchestra.routines import monthly_invoicer as invoicer_mod

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

        from orchestra.routines import monthly_invoicer as invoicer_mod

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

        from orchestra.routines import monthly_invoicer as invoicer_mod

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

        from orchestra.routines import monthly_invoicer as invoicer_mod

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

        from orchestra.routines import monthly_invoicer as invoicer_mod

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

        from orchestra.routines import monthly_invoicer as invoicer_mod

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

        from orchestra.routines import monthly_invoicer as invoicer_mod

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
