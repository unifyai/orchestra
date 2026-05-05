"""Monthly resource levy for provisioned assistant contact details.

Charges each billing account for its active platform-provisioned
contact details (phone numbers, WhatsApp senders, Discord bots).
Email contacts are BYOD-only and never billed by this routine.

Runs on the 1st of each month via Cloud Scheduler.

Scheduling Options:
1. Cloud Scheduler: POST /v0/admin/billing/resource-levy
   - Cron: ``0 0 1 * *``  (midnight UTC on the 1st)
   - Headers: Authorization: Bearer <ORCHESTRA_ADMIN_KEY>
   - Retry: 3

2. Manual: Call the admin endpoint directly for ad-hoc billing runs.

Behaviour:
    1. Fetch all ``active`` / ``grace_period`` platform-provisioned contacts
       that have not yet been billed for the target month and whose
       assistant is not a demo assistant.
    2. Group by billing account (via ``Assistant → User/Organization →
       BillingAccount``).
    3. For each billing account, sum costs, deduct from credit wallet,
       set ``last_billed_month`` on every contact, trigger auto-recharge
       if threshold met, and set ``grace_period_started_at`` on contacts
       if credits go negative.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO
from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    BillingAccount,
    Organization,
    User,
)
from orchestra.lib.billing import queue_auto_recharge
from orchestra.routines.assistant_contact_notifications import (
    LEVY_INSUFFICIENT_CREDITS_SUBJECT,
    build_insufficient_credits_email,
    get_notification_emails_for_ba,
    send_notification_emails_sync,
    set_last_notification_day,
)
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass (for structured reporting)
# ---------------------------------------------------------------------------


@dataclass
class LevyAccountResult:
    """Summary of the levy for a single billing account."""

    billing_account_id: int
    contacts_billed: int = 0
    total_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    phone_count: int = 0
    phone_cost: Decimal = field(default_factory=lambda: Decimal("0"))
    whatsapp_count: int = 0
    whatsapp_cost: Decimal = field(default_factory=lambda: Decimal("0"))
    discord_count: int = 0
    discord_cost: Decimal = field(default_factory=lambda: Decimal("0"))
    credits_before: Decimal = field(default_factory=lambda: Decimal("0"))
    credits_after: Decimal = field(default_factory=lambda: Decimal("0"))
    auto_recharge_triggered: bool = False
    marked_past_due: bool = False
    grace_period_contacts: int = 0
    insufficient_credits_notified: bool = False


@dataclass
class LevyResult:
    """Aggregate result of the levy routine."""

    billing_month: str
    total_contacts_billed: int = 0
    total_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    accounts_processed: int = 0
    accounts_failed: int = 0
    accounts_marked_past_due: int = 0
    auto_recharges_triggered: int = 0
    notifications_sent: int = 0
    account_results: List[LevyAccountResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Billing account resolution
# ---------------------------------------------------------------------------


def _get_billing_account_for_assistant(
    session: Session,
    assistant: Assistant,
) -> Optional[BillingAccount]:
    """Resolve the billing account responsible for an assistant's costs.

    - Org assistant (``organization_id`` set) → organisation's billing account.
    - Personal assistant (no org) → user's billing account.

    Returns ``None`` if no billing account is configured.
    """
    if assistant.organization_id is not None:
        org = (
            session.query(Organization)
            .filter(Organization.id == assistant.organization_id)
            .first()
        )
        if org and org.billing_account_id:
            return (
                session.query(BillingAccount)
                .filter(BillingAccount.id == org.billing_account_id)
                .first()
            )
        return None

    # Personal assistant
    user = session.query(User).filter(User.id == assistant.user_id).first()
    if user and user.billing_account_id:
        return (
            session.query(BillingAccount)
            .filter(BillingAccount.id == user.billing_account_id)
            .first()
        )
    return None


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _group_contacts_by_billing_account(
    session: Session,
    contacts: List[AssistantContact],
) -> Dict[int, Tuple[BillingAccount, List[AssistantContact]]]:
    """Group contacts by their billing account ID.

    Returns a dict mapping ``billing_account_id`` →
    ``(BillingAccount, [AssistantContact, ...])``.

    Contacts whose assistant has no valid billing account are logged and
    skipped.
    """
    # Cache assistant → billing account to avoid repeated queries
    assistant_ba_cache: Dict[int, Optional[BillingAccount]] = {}
    groups: Dict[int, Tuple[BillingAccount, List[AssistantContact]]] = {}

    for contact in contacts:
        aid = contact.assistant_id
        if aid not in assistant_ba_cache:
            assistant = (
                session.query(Assistant).filter(Assistant.agent_id == aid).first()
            )
            if assistant is None:
                logger.warning(
                    "AssistantContact %s references non-existent assistant %s – skipping",
                    contact.id,
                    aid,
                )
                assistant_ba_cache[aid] = None
                continue
            assistant_ba_cache[aid] = _get_billing_account_for_assistant(
                session,
                assistant,
            )

        ba = assistant_ba_cache[aid]
        if ba is None:
            logger.warning(
                "No billing account for assistant %s (contact %s) – skipping",
                aid,
                contact.id,
            )
            continue

        if ba.id not in groups:
            groups[ba.id] = (ba, [])
        groups[ba.id][1].append(contact)

    return groups


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def levy_provisioned_resources(
    year: int | None = None,
    month: int | None = None,
    session: Session | None = None,
) -> LevyResult:
    """Charge each billing account for its active provisioned contacts.

    Defaults to the **current** month if ``year``/``month`` are not given
    (the job runs on the 1st, so "current month" is the month that just
    started; contacts active *last* month but deleted before the 1st will
    already have ``last_billed_month`` set by the previous levy).

    Args:
        year: Target year (e.g. 2026).
        month: Target month (1-12).
        session: Optional SQLAlchemy session.  If ``None``, a fresh one is
            created using ``get_engine()``.

    Returns:
        :class:`LevyResult` with aggregate metrics.
    """
    if session is not None:
        return _levy_in_session(session, year, month)

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as session:
        return _levy_in_session(session, year, month)


def _levy_in_session(
    session: Session,
    year: int | None,
    month: int | None,
) -> LevyResult:
    """Internal implementation of the levy routine within a given session."""

    # Default to current month
    today = _dt.datetime.now(_dt.timezone.utc).date()
    if year is None or month is None:
        year, month = today.year, today.month

    billing_month = f"{year}-{month:02d}"

    result = LevyResult(billing_month=billing_month)

    try:
        # -----------------------------------------------------------------
        # 1. Fetch all billable contacts not yet billed for this month.
        # -----------------------------------------------------------------
        active_contacts: List[AssistantContact] = (
            session.query(AssistantContact)
            .join(Assistant, AssistantContact.assistant_id == Assistant.agent_id)
            .filter(
                AssistantContact.status.in_(["active", "grace_period"]),
                AssistantContact.provisioned_by == "platform",
                Assistant.demo_id.is_(None),  # exclude demo assistants
                or_(
                    AssistantContact.last_billed_month.is_(None),
                    AssistantContact.last_billed_month != billing_month,
                ),
            )
            .with_for_update()
            .all()
        )

        if not active_contacts:
            logger.info(
                "Resource levy for %s: no billable contacts found.",
                billing_month,
            )
            return result

        # -----------------------------------------------------------------
        # 2. Group by billing account.
        # -----------------------------------------------------------------
        groups = _group_contacts_by_billing_account(session, active_contacts)

        # -----------------------------------------------------------------
        # 3. Process each billing account independently.
        #    Each account gets its own commit so a failure on one does not
        #    roll back billing for the others.
        # -----------------------------------------------------------------
        for ba_id, (ba, contacts) in groups.items():
            try:
                account_result = _process_billing_account(
                    session,
                    ba,
                    contacts,
                    billing_month,
                )
                session.commit()

                result.account_results.append(account_result)
                result.total_contacts_billed += account_result.contacts_billed
                result.total_amount += account_result.total_amount
                result.accounts_processed += 1
                if account_result.auto_recharge_triggered:
                    result.auto_recharges_triggered += 1
                if account_result.marked_past_due:
                    result.accounts_marked_past_due += 1
                if account_result.insufficient_credits_notified:
                    result.notifications_sent += 1

                if account_result.marked_past_due:
                    _send_day1_notification(session, account_result)

            except Exception as _levy_err:
                session.rollback()
                result.accounts_failed += 1
                logger.exception(
                    {
                        "message": "Resource levy failed for billing account",
                        "billing_account_id": ba_id,
                        "billing_month": billing_month,
                    },
                )
                try:
                    from orchestra.routines.billing_notifications import (
                        notify_billing_event_failure,
                    )

                    notify_billing_event_failure(
                        "contact_levy",
                        error=str(_levy_err),
                        context_id=f"ba_{ba_id}_{billing_month}",
                        billing_account_id=ba_id,
                    )
                except Exception:
                    logger.warning(
                        "Failed to send billing event notification",
                        exc_info=True,
                    )

        logger.info(
            {
                "message": "Resource levy complete",
                "billing_month": billing_month,
                "accounts_processed": result.accounts_processed,
                "accounts_failed": result.accounts_failed,
                "total_contacts_billed": result.total_contacts_billed,
                "total_amount": float(result.total_amount),
                "accounts_marked_past_due": result.accounts_marked_past_due,
                "auto_recharges_triggered": result.auto_recharges_triggered,
                "notifications_sent": result.notifications_sent,
            },
        )

    except Exception:
        session.rollback()
        logger.exception(
            {
                "message": "Resource levy failed during setup",
                "billing_month": billing_month,
            },
        )
        raise

    return result


# ---------------------------------------------------------------------------
# Per-account processing
# ---------------------------------------------------------------------------


def _process_billing_account(
    session: Session,
    ba: BillingAccount,
    contacts: List[AssistantContact],
    billing_month: str,
) -> LevyAccountResult:
    """Deduct resource costs from a single billing account.

    For each contact:
    1. Look up its monthly cost from ``AssistantContactCost``.
    2. Set ``last_billed_month`` and ``monthly_cost`` on the contact row.
    3. Accumulate per-type totals.

    After processing all contacts:
    4. Deduct total from ``ba.credits``.
    5. Trigger auto-recharge if threshold is crossed.
    6. If credits go negative, start grace period on active contacts
       and set ``grace_period_started_at`` on all contacts in this batch.
    """
    ar = LevyAccountResult(
        billing_account_id=ba.id,
        credits_before=Decimal(str(ba.credits)),
    )

    total_levy = Decimal("0")

    contact_dao = AssistantContactDAO(session)
    for contact in contacts:
        cost = contact_dao.get_contact_monthly_cost(
            contact.contact_type,
            provider=contact.provider,
            country_code=contact.country_code,
        )
        total_levy += cost
        contact.last_billed_month = billing_month
        contact.monthly_cost = cost

        if contact.contact_type == "phone":
            ar.phone_count += 1
            ar.phone_cost += cost
        elif contact.contact_type == "whatsapp":
            ar.whatsapp_count += 1
            ar.whatsapp_cost += cost
        elif contact.contact_type == "discord":
            ar.discord_count += 1
            ar.discord_cost += cost

    ar.contacts_billed = len(contacts)
    ar.total_amount = total_levy

    if total_levy == 0:
        ar.credits_after = ar.credits_before
        return ar

    # Deduct credits via DAO (acquires FOR UPDATE lock, tracks balance events)
    from orchestra.db.dao.billing_account_dao import BillingAccountDAO

    ba_dao = BillingAccountDAO(session)
    new_balance = ba_dao.deduct_credits(
        ba.id,
        float(total_levy),
        category="resources",
        description=f"Contact levy ({billing_month})",
        detail={
            "event": "contact_levy",
            "billing_month": billing_month,
            "phone_count": ar.phone_count,
            "phone_cost": float(ar.phone_cost),
            "whatsapp_count": ar.whatsapp_count,
            "whatsapp_cost": float(ar.whatsapp_cost),
            "discord_count": ar.discord_count,
            "discord_cost": float(ar.discord_cost),
        },
    )
    if new_balance is not None:
        ar.credits_after = Decimal(str(new_balance))
    else:
        ar.credits_after = ar.credits_before

    logger.info(
        "Billing account %d: charged $%s for %d contacts (%s). " "Credits: %s → %s.",
        ba.id,
        total_levy,
        len(contacts),
        billing_month,
        ar.credits_before,
        ar.credits_after,
    )

    # Auto-recharge check (isolated so a Stripe error doesn't prevent
    # the levy deduction from being committed)
    if (
        ba.autorecharge
        and ba.stripe_customer_id
        and ba.credits <= ba.autorecharge_threshold
    ):
        try:
            ar.auto_recharge_triggered = queue_auto_recharge(
                session,
                ba,
                int(ba.autorecharge_qty),
                entity_label=f"billing_account {ba.id}",
            )
        except Exception:
            logger.exception(
                {
                    "message": "Auto-recharge failed during levy (non-fatal)",
                    "billing_account_id": ba.id,
                },
            )

    # Start grace period on active contacts if credits went negative
    if ba.credits < 0:
        now = _dt.datetime.now(_dt.timezone.utc)
        for contact in contacts:
            if contact.status == "active":
                contact.status = "grace_period"
                contact.grace_period_started_at = now
                set_last_notification_day(contact, 1)
                ar.grace_period_contacts += 1

        if ar.grace_period_contacts > 0:
            ar.marked_past_due = True
            ar.insufficient_credits_notified = True

            logger.warning(
                "Billing account %d has negative credits after levy. "
                "%d contacts entered grace period.",
                ba.id,
                ar.grace_period_contacts,
            )

    return ar


def _send_day1_notification(
    session: Session,
    account_result: LevyAccountResult,
) -> None:
    """Send the Day-1 insufficient credits email for a billing account."""
    try:
        ba = (
            session.query(BillingAccount)
            .filter(BillingAccount.id == account_result.billing_account_id)
            .first()
        )
        if ba is None:
            return

        recipients = get_notification_emails_for_ba(session, ba)

        if recipients:
            send_notification_emails_sync(
                recipients,
                LEVY_INSUFFICIENT_CREDITS_SUBJECT,
                build_insufficient_credits_email(),
            )
            logger.info(
                "Day-1 insufficient credits notification sent for BA %d " "to %s.",
                ba.id,
                recipients,
            )

    except Exception:
        logger.exception(
            "Failed to send Day-1 notification for BA %d.",
            account_result.billing_account_id,
        )
