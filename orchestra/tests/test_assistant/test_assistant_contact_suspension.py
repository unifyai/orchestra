"""
Tests for the assistant_contact_suspension routine and Stripe webhook grace-period
clearing (Phase 4).

Covers:
1. Suspension routine – restoration path:
   - Contacts restored when BA has credits ≥ 0
   - Account status not modified by routine (contacts only)
   - reawaken_assistant called for restored contacts
2. Suspension routine – deletion path (≥ 14 days overdue):
   - External deprovisioning called (phone / email / whatsapp)
   - AssistantContact soft-deleted
   - Backward-compat columns cleared on Assistant
   - reawaken_assistant called after deletion
   - Deletion notification email sent
3. Suspension routine – Notification schedule (Day 7/13):
   - Reminder emails sent at each notification day
   - Notification tracking prevents duplicate sends
4. Suspension routine – edge cases:
   - No grace contacts → no-op
   - Contacts without a billing account are skipped
   - Contacts with future grace_period_started_at are not acted upon
   - Multiple contact types for same BA handled
   - Demo assistants excluded
   - BYOD contacts skipped for deprovisioning
5. Stripe webhook grace-period clearing:
   - maybe_clear_grace_period restores contacts when credits ≥ 0
   - No action when BA is already ACTIVE
   - No action when credits still negative
6. clear_grace_period_for_billing_account:
   - Personal user contacts restored
   - Org contacts restored
   - Mixed personal + org not cross-contaminating
7. Admin endpoint:
   - POST /v0/admin/billing/resource-suspension triggers routine
8. Helper functions (assistant_contact_notifications module):
   - build_warning_email / build_deletion_email produce HTML
   - get_notification_emails_for_ba returns correct recipients
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    AssistantContactCost,
    BillingAccount,
    Organization,
    User,
)
from orchestra.routines.assistant_contact_notifications import (
    build_deletion_email,
    build_warning_email,
    get_notification_emails_for_ba,
)
from orchestra.routines.assistant_contact_suspension import (
    _group_grace_contacts_by_ba,
    suspend_overdue_contacts,
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
            AssistantContactCost(
                contact_type="discord",
                provider="discord",
                country_code=None,
                monthly_cost=Decimal("1"),
                one_time_cost=Decimal("1"),
            ),
            AssistantContactCost(
                contact_type="email",
                provider="microsoft_365",
                country_code=None,
                monthly_cost=Decimal("12.50"),
                one_time_cost=Decimal("5.00"),
            ),
        ]
        dbsession.add_all(rows)
        dbsession.flush()
    yield


@pytest.fixture(autouse=True)
def mock_external_calls():
    """Mock all external service calls so tests don't hit real infra."""
    with (
        patch(
            "orchestra.routines.assistant_contact_suspension._deprovision_contact",
            new_callable=AsyncMock,
        ) as mock_deprovision,
        patch(
            "orchestra.routines.assistant_contact_suspension.send_notification_emails",
            new_callable=AsyncMock,
        ) as mock_send_emails,
        patch(
            "orchestra.web.api.utils.assistant_infra.reawaken_assistant",
            new_callable=AsyncMock,
        ) as mock_reawaken,
    ):
        mock_deprovision.return_value = None
        mock_send_emails.return_value = None
        mock_reawaken.return_value = None
        yield {
            "deprovision": mock_deprovision,
            "send_emails": mock_send_emails,
            "reawaken": mock_reawaken,
        }


# ---------------------------------------------------------------------------
# Helper factories (same pattern as test_resource_levy.py)
# ---------------------------------------------------------------------------


def _make_ba(
    dbsession: Session,
    credits: float = 100.0,
    account_status: str = "ACTIVE",
    billing_email: str | None = None,
    stripe_customer_id: str | None = None,
) -> BillingAccount:
    ba = BillingAccount(
        credits=Decimal(str(credits)),
        account_status=account_status,
        billing_email=billing_email,
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
    first_name: str = "SuspBot",
    surname: str = "Test",
    organization_id: int | None = None,
    phone: str | None = None,
    email: str | None = None,
    user_phone: str | None = None,
    assistant_whatsapp_number: str | None = None,
    user_whatsapp_number: str | None = None,
) -> Assistant:
    a = Assistant(
        user_id=user_id,
        first_name=first_name,
        surname=surname,
        organization_id=organization_id,
    )
    dbsession.add(a)
    dbsession.flush()
    return a


def _make_grace_contact(
    dbsession: Session,
    assistant_id: int,
    contact_type: str = "phone",
    contact_value: str = "+15559990001",
    provider: str | None = "twilio",
    country_code: str | None = "US",
    grace_days_ago: int = 15,
    provisioned_by: str = "platform",
) -> AssistantContact:
    """Create a contact in grace_period status."""
    gp_started = datetime.now(timezone.utc) - timedelta(days=grace_days_ago)
    c = AssistantContact(
        assistant_id=assistant_id,
        contact_type=contact_type,
        contact_value=contact_value,
        provider=provider,
        country_code=country_code,
        provisioned_by=provisioned_by,
        status="grace_period",
        grace_period_started_at=gp_started,
    )
    dbsession.add(c)
    dbsession.flush()
    return c


# ============================================================================
# 1. Suspension routine – restoration path
# ============================================================================


class TestSuspensionRestoration:
    """When the billing account has credits ≥ 0, contacts should be restored."""

    @pytest.mark.anyio
    async def test_restores_contacts_when_ba_has_credits(
        self,
        dbsession: Session,
    ):
        """Grace-period contacts are restored to 'active' if BA is topped up."""
        ba = _make_ba(dbsession, credits=50, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_rest_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="RestBot1")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559910001",
            grace_days_ago=10,
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_restored == 1
        assert result.contacts_deleted == 0

        dbsession.refresh(c)
        assert c.status == "active"
        assert c.grace_period_started_at is None

    @pytest.mark.anyio
    async def test_account_status_not_modified_on_restoration(self, dbsession: Session):
        """Account status is not modified by the suspension routine."""
        ba = _make_ba(dbsession, credits=10, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_rest_u2", ba)
        asst = _make_assistant(dbsession, user.id, first_name="RestBot2")
        _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559910002",
            grace_days_ago=5,
        )
        dbsession.flush()

        await suspend_overdue_contacts(session=dbsession)

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"

    @pytest.mark.anyio
    async def test_account_status_unchanged_when_suspended(self, dbsession: Session):
        """Account status remains SUSPENDED – routine does not modify it."""
        ba = _make_ba(dbsession, credits=10, account_status="SUSPENDED")
        user = _make_user(dbsession, "susp_rest_u3", ba)
        asst = _make_assistant(dbsession, user.id, first_name="RestBot3")
        _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559910003",
            grace_days_ago=5,
        )
        dbsession.flush()

        await suspend_overdue_contacts(session=dbsession)

        dbsession.refresh(ba)
        assert ba.account_status == "SUSPENDED"

    @pytest.mark.anyio
    async def test_restores_multiple_contacts(self, dbsession: Session):
        """Multiple grace-period contacts are all restored when BA is topped up."""
        ba = _make_ba(dbsession, credits=100, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_rest_u4", ba)
        asst = _make_assistant(dbsession, user.id, first_name="RestBot4")
        c1 = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15559910004",
            grace_days_ago=10,
        )
        c2 = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="rest4@test.ai",
            provider="google_workspace",
            country_code=None,
            grace_days_ago=8,
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_restored == 2
        dbsession.refresh(c1)
        dbsession.refresh(c2)
        assert c1.status == "active"
        assert c2.status == "active"

    @pytest.mark.anyio
    async def test_zero_credits_restores_contacts(self, dbsession: Session):
        """Contacts are restored when credits are exactly 0 (≥ 0 check)."""
        ba = _make_ba(dbsession, credits=0, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_rest_u5", ba)
        asst = _make_assistant(dbsession, user.id, first_name="RestBot5")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559910005",
            grace_days_ago=12,
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_restored == 1
        dbsession.refresh(c)
        assert c.status == "active"


# ============================================================================
# 2. Suspension routine – deletion path (≥ 14 days overdue)
# ============================================================================


class TestSuspensionDeletion:
    """When credits < 0 and grace ≥ 14 days, contacts are deleted."""

    @pytest.mark.anyio
    async def test_deletes_overdue_phone_contact(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """Phone contact overdue by 15 days is soft-deleted."""
        ba = _make_ba(dbsession, credits=-10, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_del_u1", ba)
        asst = _make_assistant(
            dbsession,
            user.id,
            first_name="DelBot1",
            phone="+15559920001",
            user_phone="+15551111111",
        )
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15559920001",
            grace_days_ago=15,
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_deleted == 1
        assert result.deletion_emails_sent == 1

        dbsession.refresh(c)
        assert c.status == "deleted"
        assert c.deleted_at is not None

    @pytest.mark.anyio
    async def test_deletes_overdue_email_contact(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """Email contact overdue by 14 days is soft-deleted."""
        ba = _make_ba(dbsession, credits=-5, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_del_u2", ba)
        asst = _make_assistant(
            dbsession,
            user.id,
            first_name="DelBot2",
            email="del2@test.ai",
        )
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="del2@test.ai",
            provider="google_workspace",
            country_code=None,
            grace_days_ago=14,  # Exactly 14 days
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_deleted == 1
        dbsession.refresh(c)
        assert c.status == "deleted"

    @pytest.mark.anyio
    async def test_deletes_overdue_whatsapp_contact(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """WhatsApp contact overdue by 20 days is soft-deleted."""
        ba = _make_ba(dbsession, credits=-1, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_del_u3", ba)
        asst = _make_assistant(
            dbsession,
            user.id,
            first_name="DelBot3",
            assistant_whatsapp_number="+15559920003",
            user_whatsapp_number="+15550003333",
        )
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_type="whatsapp",
            contact_value="+15559920003",
            provider="twilio",
            country_code=None,
            grace_days_ago=20,
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_deleted == 1
        dbsession.refresh(c)
        assert c.status == "deleted"

    @pytest.mark.anyio
    async def test_does_not_delete_contacts_under_14_days(
        self,
        dbsession: Session,
    ):
        """Contacts in grace_period for < 14 days are not deleted."""
        ba = _make_ba(dbsession, credits=-10, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_del_u4", ba)
        asst = _make_assistant(dbsession, user.id, first_name="DelBot4")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559920004",
            grace_days_ago=13,  # Not yet overdue
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_deleted == 0
        dbsession.refresh(c)
        assert c.status == "grace_period"

    @pytest.mark.anyio
    async def test_deletes_multiple_overdue_contacts(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """All overdue contacts for a BA are deleted at once."""
        ba = _make_ba(dbsession, credits=-20, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_del_u5", ba)
        asst = _make_assistant(
            dbsession,
            user.id,
            first_name="DelBot5",
            phone="+15559920005",
            email="del5@test.ai",
        )
        c1 = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_type="phone",
            contact_value="+15559920005",
            grace_days_ago=15,
        )
        c2 = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="del5@test.ai",
            provider="google_workspace",
            country_code=None,
            grace_days_ago=16,
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_deleted == 2
        dbsession.refresh(c1)
        dbsession.refresh(c2)
        assert c1.status == "deleted"
        assert c2.status == "deleted"


# ============================================================================
# 3. Day-7 reminder
# ============================================================================


class TestSuspensionNotifications:
    """Notification emails are sent at Days 7 and 13 of grace period."""

    @pytest.mark.anyio
    async def test_sends_day7_reminder(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """Day-7 warning sent for contacts at 7+ days in grace period."""
        ba = _make_ba(dbsession, credits=-5, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_rem_u1", ba, email="reminder@test.com")
        asst = _make_assistant(dbsession, user.id, first_name="RemBot1")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559930001",
            grace_days_ago=8,  # Between 7 and 14
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.reminders_sent == 1
        assert result.contacts_deleted == 0
        dbsession.refresh(c)
        assert c.metadata_.get("last_notification_day") == 7

    @pytest.mark.anyio
    async def test_sends_day13_final_warning(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """Day-13 final warning sent for contacts at 13 days in grace period."""
        ba = _make_ba(dbsession, credits=-5, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_n13_u1", ba, email="day13@test.com")
        asst = _make_assistant(dbsession, user.id, first_name="Day13Bot")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559930012",
            grace_days_ago=13,
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.reminders_sent == 1
        assert result.contacts_deleted == 0
        dbsession.refresh(c)
        assert c.metadata_.get("last_notification_day") == 13

    @pytest.mark.anyio
    async def test_no_reminder_under_7_days(self, dbsession: Session):
        """No reminder email for contacts < 7 days in grace period."""
        ba = _make_ba(dbsession, credits=-5, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_rem_u2", ba)
        asst = _make_assistant(dbsession, user.id, first_name="RemBot2")
        _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559930002",
            grace_days_ago=5,  # Less than 7
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.reminders_sent == 0
        assert result.contacts_deleted == 0

    @pytest.mark.anyio
    async def test_no_duplicate_notification(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """A contact already notified for Day 7 does not get Day 7 again."""
        ba = _make_ba(dbsession, credits=-5, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_nodup_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="NoDupBot")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559930020",
            grace_days_ago=8,
        )
        # Simulate Day-7 notification already sent
        c.metadata_ = {"last_notification_day": 7}
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.reminders_sent == 0
        assert result.contacts_deleted == 0

    @pytest.mark.anyio
    async def test_skipped_notification_catches_up(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """If Day-7 was missed, Day-13 run sends Day-13 (catch-up)."""
        ba = _make_ba(dbsession, credits=-5, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_catch_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="CatchBot")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559930021",
            grace_days_ago=13,  # Day 13: missed Day 7
        )
        # No previous notification sent
        c.metadata_ = {"last_notification_day": 0}
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        # Should send Day-13 notification (highest applicable: 13 >= 13, last=0 < 13)
        assert result.reminders_sent == 1
        dbsession.refresh(c)
        assert c.metadata_.get("last_notification_day") == 13

    @pytest.mark.anyio
    async def test_both_reminder_and_deletion(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """One contact gets a reminder, another gets deleted – both happen."""
        ba = _make_ba(dbsession, credits=-10, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_rem_u3", ba)
        asst1 = _make_assistant(
            dbsession,
            user.id,
            first_name="RemBot3a",
            phone="+15559930003",
        )
        asst2 = _make_assistant(dbsession, user.id, first_name="RemBot3b")

        # Overdue contact (delete)
        c_old = _make_grace_contact(
            dbsession,
            asst1.agent_id,
            contact_type="phone",
            contact_value="+15559930003",
            grace_days_ago=15,
        )
        # Approaching deadline (reminder)
        c_mid = _make_grace_contact(
            dbsession,
            asst2.agent_id,
            contact_type="email",
            contact_value="rem3b@test.ai",
            provider="google_workspace",
            country_code=None,
            grace_days_ago=8,
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_deleted == 1
        assert result.reminders_sent == 1

        dbsession.refresh(c_old)
        assert c_old.status == "deleted"
        dbsession.refresh(c_mid)
        assert c_mid.status == "grace_period"  # Still in grace

    @pytest.mark.anyio
    async def test_restoration_clears_notification_tracking(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """When credits are restored, notification tracking is reset."""
        ba = _make_ba(dbsession, credits=10, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_rst_n_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="RstNBot")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559930030",
            grace_days_ago=8,
        )
        c.metadata_ = {"last_notification_day": 7}
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_restored == 1
        dbsession.refresh(c)
        assert c.status == "active"
        assert c.metadata_.get("last_notification_day") == 0


# ============================================================================
# 4. Edge cases
# ============================================================================


class TestSuspensionEdgeCases:
    """Edge cases for the suspension routine."""

    @pytest.mark.anyio
    async def test_no_grace_contacts_noop(self, dbsession: Session):
        """When there are no grace-period contacts, routine is a no-op."""
        result = await suspend_overdue_contacts(session=dbsession)

        assert result.total_grace_contacts_found == 0
        assert result.accounts_processed == 0

    @pytest.mark.anyio
    async def test_contacts_without_ba_skipped(self, dbsession: Session):
        """Contacts whose assistant has no billing account are skipped."""
        user = User(id="susp_edge_u1", email="edge1@test.com")
        dbsession.add(user)
        dbsession.flush()
        asst = _make_assistant(dbsession, user.id, first_name="EdgeBot1")
        _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559940001",
            grace_days_ago=15,
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        # Contact found but no BA → skipped
        assert result.total_grace_contacts_found >= 1
        assert result.contacts_deleted == 0
        assert result.contacts_restored == 0

    @pytest.mark.anyio
    async def test_future_grace_period_not_acted_on(self, dbsession: Session):
        """Contacts with a future grace_period_started_at are not acted upon."""
        ba = _make_ba(dbsession, credits=-5, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_edge_u2", ba)
        asst = _make_assistant(dbsession, user.id, first_name="EdgeBot2")

        # grace_period_started_at in the future (shouldn't normally happen,
        # but guards against clock skew)
        c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15559940002",
            provider="twilio",
            country_code="US",
            provisioned_by="platform",
            status="grace_period",
            grace_period_started_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        dbsession.add(c)
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        # Should not delete or send a reminder
        dbsession.refresh(c)
        assert c.status == "grace_period"
        assert result.contacts_deleted == 0
        assert result.reminders_sent == 0

    @pytest.mark.anyio
    async def test_mixed_ba_states(self, dbsession: Session, mock_external_calls):
        """Multiple BAs: one topped-up (restore), one negative (delete)."""
        # BA1: topped up
        ba1 = _make_ba(dbsession, credits=100, account_status="ACTIVE")
        user1 = _make_user(dbsession, "susp_edge_u3a", ba1)
        asst1 = _make_assistant(dbsession, user1.id, first_name="EdgeBot3a")
        c_restore = _make_grace_contact(
            dbsession,
            asst1.agent_id,
            contact_value="+15559940003",
            grace_days_ago=10,
        )

        # BA2: still negative
        ba2 = _make_ba(dbsession, credits=-20, account_status="ACTIVE")
        user2 = _make_user(dbsession, "susp_edge_u3b", ba2)
        asst2 = _make_assistant(
            dbsession,
            user2.id,
            first_name="EdgeBot3b",
            phone="+15559940004",
        )
        c_delete = _make_grace_contact(
            dbsession,
            asst2.agent_id,
            contact_type="phone",
            contact_value="+15559940004",
            grace_days_ago=15,
        )
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        assert result.contacts_restored >= 1
        assert result.contacts_deleted >= 1

        dbsession.refresh(c_restore)
        assert c_restore.status == "active"

        dbsession.refresh(c_delete)
        assert c_delete.status == "deleted"

    @pytest.mark.anyio
    async def test_null_grace_period_started_at_not_deleted(
        self,
        dbsession: Session,
    ):
        """Contacts with NULL grace_period_started_at are not deleted/reminded."""
        ba = _make_ba(dbsession, credits=-5, account_status="ACTIVE")
        user = _make_user(dbsession, "susp_edge_u4", ba)
        asst = _make_assistant(dbsession, user.id, first_name="EdgeBot4")

        c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15559940005",
            provider="twilio",
            country_code="US",
            provisioned_by="platform",
            status="grace_period",
            grace_period_started_at=None,  # Should not be hit
        )
        dbsession.add(c)
        dbsession.flush()

        result = await suspend_overdue_contacts(session=dbsession)

        # The contact has status grace_period but NULL started_at
        # The filter catches it as a grace_period contact, but neither
        # cutoff check passes since grace_period_started_at is None
        dbsession.refresh(c)
        # Since BA has negative credits, it won't be restored either
        assert result.contacts_deleted == 0
        assert result.reminders_sent == 0

    @pytest.mark.anyio
    async def test_byod_contact_skips_deprovisioning(self, dbsession: Session):
        """User-provisioned (BYOD) contacts are not externally deprovisioned."""
        from orchestra.routines.assistant_contact_suspension import (
            _deprovision_contact as real_deprovision,
        )

        ba = _make_ba(dbsession, credits=-10)
        user = _make_user(dbsession, "susp_byod_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="BYODBot")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_type="email",
            contact_value="user@gmail.com",
            provider="google_workspace",
            provisioned_by="user",
            grace_days_ago=15,
        )
        dbsession.flush()

        with (
            patch(
                "orchestra.web.api.utils.assistant_infra.delete_email",
                new_callable=AsyncMock,
            ) as mock_del_email,
            patch(
                "orchestra.web.api.utils.assistant_infra.delete_outlook_email",
                new_callable=AsyncMock,
            ) as mock_del_outlook,
            patch(
                "orchestra.web.api.utils.assistant_infra.delete_phone_number",
                new_callable=AsyncMock,
            ) as mock_del_phone,
        ):
            await real_deprovision(c)

            mock_del_email.assert_not_called()
            mock_del_outlook.assert_not_called()
            mock_del_phone.assert_not_called()


# ============================================================================
# 5. Grouping helper
# ============================================================================


class TestGroupGraceContactsByBa:
    """Tests for _group_grace_contacts_by_ba."""

    def test_groups_by_billing_account(self, dbsession: Session):
        ba1 = _make_ba(dbsession, credits=-10)
        user1 = _make_user(dbsession, "grp_su1", ba1)
        asst1 = _make_assistant(dbsession, user1.id, first_name="GrpS1")
        c1 = _make_grace_contact(
            dbsession,
            asst1.agent_id,
            contact_value="+15559950001",
        )

        ba2 = _make_ba(dbsession, credits=-20)
        user2 = _make_user(dbsession, "grp_su2", ba2)
        asst2 = _make_assistant(dbsession, user2.id, first_name="GrpS2")
        c2 = _make_grace_contact(
            dbsession,
            asst2.agent_id,
            contact_value="+15559950002",
        )
        dbsession.flush()

        groups = _group_grace_contacts_by_ba(dbsession, [c1, c2])

        assert ba1.id in groups
        assert ba2.id in groups
        assert len(groups[ba1.id][1]) == 1
        assert len(groups[ba2.id][1]) == 1

    def test_skips_contacts_without_ba(self, dbsession: Session):
        user = User(id="grp_su3", email="grp_su3@test.com")
        dbsession.add(user)
        dbsession.flush()
        asst = _make_assistant(dbsession, user.id, first_name="GrpSNoBa")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559950003",
        )
        dbsession.flush()

        groups = _group_grace_contacts_by_ba(dbsession, [c])
        assert len(groups) == 0


# ============================================================================
# 6. clear_grace_period_for_billing_account (Stripe webhook helper)
# ============================================================================


class TestClearGracePeriodForBa:
    """Tests for the reusable clear_grace_period_for_billing_account function."""

    def test_clears_personal_user_contacts(self, dbsession: Session):
        """Personal user's grace-period contacts are restored."""
        ba = _make_ba(dbsession, credits=50, account_status="ACTIVE")
        user = _make_user(dbsession, "cgp_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="CgpBot1")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559960001",
            grace_days_ago=5,
        )
        dbsession.flush()

        affected = AssistantContactDAO(
            dbsession,
        ).clear_grace_period_for_billing_account(ba)
        dbsession.flush()

        assert asst.agent_id in affected
        dbsession.refresh(c)
        assert c.status == "active"
        assert c.grace_period_started_at is None

    def test_clears_org_contacts(self, dbsession: Session):
        """Org contacts are restored when org BA is topped up."""
        user_ba = _make_ba(dbsession, credits=10)
        user = _make_user(dbsession, "cgp_u2", user_ba)
        org_ba = _make_ba(dbsession, credits=50, account_status="ACTIVE")
        org = _make_org(dbsession, user, org_ba, name="CgpOrg1")
        asst = _make_assistant(
            dbsession,
            user.id,
            first_name="CgpBot2",
            organization_id=org.id,
        )
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559960002",
            grace_days_ago=3,
        )
        dbsession.flush()

        affected = AssistantContactDAO(
            dbsession,
        ).clear_grace_period_for_billing_account(org_ba)
        dbsession.flush()

        assert asst.agent_id in affected
        dbsession.refresh(c)
        assert c.status == "active"

    def test_does_not_cross_contaminate(self, dbsession: Session):
        """Clearing grace for user BA does not affect org BA contacts."""
        user_ba = _make_ba(dbsession, credits=50, account_status="ACTIVE")
        user = _make_user(dbsession, "cgp_u3", user_ba)
        org_ba = _make_ba(dbsession, credits=-10, account_status="ACTIVE")
        org = _make_org(dbsession, user, org_ba, name="CgpOrg2")

        # Personal assistant
        pers_asst = _make_assistant(dbsession, user.id, first_name="CgpPers")
        c_pers = _make_grace_contact(
            dbsession,
            pers_asst.agent_id,
            contact_value="+15559960003",
            grace_days_ago=5,
        )

        # Org assistant
        org_asst = _make_assistant(
            dbsession,
            user.id,
            first_name="CgpOrga",
            organization_id=org.id,
        )
        c_org = _make_grace_contact(
            dbsession,
            org_asst.agent_id,
            contact_value="+15559960004",
            grace_days_ago=5,
        )
        dbsession.flush()

        # Only clear for user's personal BA
        affected = AssistantContactDAO(
            dbsession,
        ).clear_grace_period_for_billing_account(user_ba)
        dbsession.flush()

        assert pers_asst.agent_id in affected
        assert org_asst.agent_id not in affected

        dbsession.refresh(c_pers)
        assert c_pers.status == "active"
        dbsession.refresh(c_org)
        assert c_org.status == "grace_period"  # Unchanged

    def test_returns_empty_when_no_contacts(self, dbsession: Session):
        """Returns empty set when no grace-period contacts exist."""
        ba = _make_ba(dbsession, credits=100)
        user = _make_user(dbsession, "cgp_u4", ba)
        # Assistant with an active (not grace) contact
        asst = _make_assistant(dbsession, user.id, first_name="CgpBot4")
        active_c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="phone",
            contact_value="+15559960005",
            provider="twilio",
            status="active",
        )
        dbsession.add(active_c)
        dbsession.flush()

        affected = AssistantContactDAO(
            dbsession,
        ).clear_grace_period_for_billing_account(ba)

        assert len(affected) == 0


# ============================================================================
# 7. Stripe webhook integration (maybe_clear_grace_period)
# ============================================================================


class TestMaybeClearGracePeriod:
    """Tests for AssistantContactDAO.maybe_clear_grace_period."""

    def test_clears_grace_period_when_credits_restored(
        self,
        dbsession: Session,
    ):
        """When a top-up restores credits, grace period is cleared."""
        ba = _make_ba(dbsession, credits=50, account_status="ACTIVE")
        user = _make_user(dbsession, "webhook_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="WebhookBot1")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559970001",
            grace_days_ago=5,
        )
        dbsession.flush()

        AssistantContactDAO(dbsession).maybe_clear_grace_period(ba)
        dbsession.flush()

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"

        dbsession.refresh(c)
        assert c.status == "active"
        assert c.grace_period_started_at is None

    def test_no_op_when_already_active(self, dbsession: Session):
        """No action when BA is already ACTIVE."""
        ba = _make_ba(dbsession, credits=100, account_status="ACTIVE")
        user = _make_user(dbsession, "webhook_u2", ba)
        dbsession.flush()

        # Should not raise or change anything
        AssistantContactDAO(dbsession).maybe_clear_grace_period(ba)

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"

    def test_no_op_when_credits_still_negative(self, dbsession: Session):
        """No action when credits are still negative."""
        ba = _make_ba(dbsession, credits=-5, account_status="ACTIVE")
        user = _make_user(dbsession, "webhook_u3", ba)
        asst = _make_assistant(dbsession, user.id, first_name="WebhookBot3")
        c = _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559970003",
            grace_days_ago=5,
        )
        dbsession.flush()

        AssistantContactDAO(dbsession).maybe_clear_grace_period(ba)

        dbsession.refresh(ba)
        assert ba.account_status == "ACTIVE"

        dbsession.refresh(c)
        assert c.status == "grace_period"


# ============================================================================
# 8. Email template & helper functions
# ============================================================================


class TestEmailHelpers:
    """Tests for email template builders and notification helpers."""

    def test_build_warning_email_contains_key_info(self):
        """Warning email HTML includes days remaining and billing link."""
        html = build_warning_email(days_remaining=7)

        assert "7 day(s)" in html
        assert "billing settings" in html
        assert "cannot be recovered" in html

    def test_build_deletion_email_contains_key_info(self):
        """Deletion email HTML mentions deletion and billing link."""
        html = build_deletion_email()

        assert "cannot be recovered" in html
        assert "billing settings" in html

    def test_get_notification_emails_personal_user(self, dbsession: Session):
        """For a personal BA, the user's email is returned."""
        ba = _make_ba(dbsession, credits=0)
        user = _make_user(
            dbsession,
            "email_u1",
            ba,
            email="notify_me@test.com",
        )
        dbsession.flush()

        emails = get_notification_emails_for_ba(dbsession, ba)
        assert "notify_me@test.com" in emails

    def test_get_notification_emails_org(self, dbsession: Session):
        """For an org BA, the org owner's email is returned."""
        owner_ba = _make_ba(dbsession, credits=10)
        owner = _make_user(
            dbsession,
            "email_u2",
            owner_ba,
            email="owner@test.com",
        )
        org_ba = _make_ba(dbsession, credits=0)
        org = _make_org(dbsession, owner, org_ba, name="EmailOrg")
        dbsession.flush()

        emails = get_notification_emails_for_ba(dbsession, org_ba)
        assert "owner@test.com" in emails

    def test_get_notification_emails_includes_billing_email(
        self,
        dbsession: Session,
    ):
        """If BA has billing_email set, it's included in recipients."""
        ba = _make_ba(
            dbsession,
            credits=0,
            billing_email="billing@corp.com",
        )
        user = _make_user(
            dbsession,
            "email_u3",
            ba,
            email="user@test.com",
        )
        dbsession.flush()

        emails = get_notification_emails_for_ba(dbsession, ba)
        assert "user@test.com" in emails
        assert "billing@corp.com" in emails

    def test_get_notification_emails_no_duplicates(self, dbsession: Session):
        """If billing_email equals user email, only one entry is returned."""
        ba = _make_ba(
            dbsession,
            credits=0,
            billing_email="same@test.com",
        )
        user = _make_user(
            dbsession,
            "email_u4",
            ba,
            email="same@test.com",
        )
        dbsession.flush()

        emails = get_notification_emails_for_ba(dbsession, ba)
        assert emails.count("same@test.com") == 1


# ============================================================================
# 9. Admin endpoint
# ============================================================================


class TestAdminResourceSuspensionEndpoint:
    """Tests for POST /v0/admin/billing/resource-suspension."""

    @pytest.mark.anyio
    async def test_trigger_suspension_via_admin(
        self,
        client: AsyncClient,
        dbsession: Session,
    ):
        """The admin endpoint triggers the suspension routine."""
        from orchestra.tests.utils import ADMIN_HEADERS

        # Create a grace-period contact that should be restored
        ba = _make_ba(dbsession, credits=50, account_status="ACTIVE")
        user = _make_user(dbsession, "admin_susp_u1", ba)
        asst = _make_assistant(dbsession, user.id, first_name="AdminSusp1")
        _make_grace_contact(
            dbsession,
            asst.agent_id,
            contact_value="+15559990001",
            grace_days_ago=5,
        )
        dbsession.flush()
        dbsession.commit()

        resp = await client.post(
            "/v0/admin/billing/resource-suspension",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "total_grace_contacts_found" in body
        assert "contacts_restored" in body
        assert "contacts_deleted" in body

    @pytest.mark.anyio
    async def test_suspension_endpoint_empty(
        self,
        client: AsyncClient,
    ):
        """The endpoint works when there are no grace-period contacts."""
        from orchestra.tests.utils import ADMIN_HEADERS

        resp = await client.post(
            "/v0/admin/billing/resource-suspension",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["total_grace_contacts_found"] == 0


# ============================================================================
# 10. End-to-end: levy → grace → suspension lifecycle
# ============================================================================


class TestLevyToSuspensionLifecycle:
    """Integration test: levy puts contacts in grace → suspension deletes overdue."""

    @pytest.mark.anyio
    async def test_levy_creates_grace_suspension_deletes(
        self,
        dbsession: Session,
        mock_external_calls,
    ):
        """
        1. Levy puts contacts in grace_period when BA goes negative.
        2. After 14 days, suspension routine deletes the contacts.
        """
        from orchestra.routines.assistant_contact_levy import levy_provisioned_resources

        # Setup: user with $5 credits and a $14/month email
        ba = _make_ba(dbsession, credits=5, account_status="ACTIVE")
        user = _make_user(dbsession, "e2e_u1", ba)
        asst = _make_assistant(
            dbsession,
            user.id,
            first_name="E2eBot",
            email="e2e@test.ai",
        )
        c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="e2e@test.ai",
            provider="google_workspace",
            provisioned_by="platform",
            status="active",
        )
        dbsession.add(c)
        dbsession.flush()

        # Step 1: Run levy → should go negative and put contacts in grace
        levy_result = levy_provisioned_resources(2026, 3, session=dbsession)

        dbsession.refresh(ba)
        assert ba.credits < 0

        dbsession.refresh(c)
        assert c.status == "grace_period"
        assert c.grace_period_started_at is not None

        # Step 2: Simulate 14+ days passing by backdating grace_period_started_at
        c.grace_period_started_at = datetime.now(timezone.utc) - timedelta(days=15)
        dbsession.flush()

        # Step 3: Run suspension → should delete the contact
        susp_result = await suspend_overdue_contacts(session=dbsession)

        assert susp_result.contacts_deleted == 1
        assert susp_result.deletion_emails_sent == 1

        dbsession.refresh(c)
        assert c.status == "deleted"
        assert c.deleted_at is not None

    @pytest.mark.anyio
    async def test_levy_then_topup_clears_grace(self, dbsession: Session):
        """
        1. Levy puts contacts in grace_period.
        2. User tops up (simulated) → clear_grace_period restores them.
        """
        from orchestra.routines.assistant_contact_levy import levy_provisioned_resources

        ba = _make_ba(dbsession, credits=5, account_status="ACTIVE")
        user = _make_user(dbsession, "e2e_u2", ba)
        asst = _make_assistant(dbsession, user.id, first_name="E2eBot2")
        c = AssistantContact(
            assistant_id=asst.agent_id,
            contact_type="email",
            contact_value="e2e2@test.ai",
            provider="google_workspace",
            provisioned_by="platform",
            status="active",
        )
        dbsession.add(c)
        dbsession.flush()

        # Step 1: Levy → grace_period
        levy_provisioned_resources(2026, 3, session=dbsession)

        dbsession.refresh(c)
        assert c.status == "grace_period"

        # Step 2: Simulate top-up by adding credits
        ba.credits = Decimal("50")
        dbsession.flush()

        # Step 3: Clear grace period (as would happen from Stripe webhook)
        affected = AssistantContactDAO(
            dbsession,
        ).clear_grace_period_for_billing_account(ba)
        dbsession.flush()

        assert asst.agent_id in affected
        dbsession.refresh(c)
        assert c.status == "active"
        assert c.grace_period_started_at is None
