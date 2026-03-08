"""Daily grace-period enforcement for provisioned assistant contacts.

Deprovisions contacts that have been in ``grace_period`` for 14+ days on
billing accounts that still have insufficient credits.  Also clears the
grace period on contacts whose billing account has since been topped up.

Notification schedule (see ``assistant_contact_notifications.py`` for templates):

    Day  7  – Warning email
    Day 13  – Final warning email
    Day 14+ – Deprovision + deletion email

Notifications are tracked per-contact via the ``metadata`` JSONB column
(``last_notification_day`` key) to prevent duplicate sends across retries
or overlapping runs.

Scheduling:
    Cloud Scheduler: POST /v0/admin/billing/resource-suspension
    Cron: ``0 1 * * *``  (01:00 UTC daily)
    Headers: Authorization: Bearer <ORCHESTRA_ADMIN_KEY>
    Retry: 3
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.orchestra_models import (
    Assistant,
    AssistantContact,
    BillingAccount,
)
from orchestra.routines.assistant_contact_levy import _get_billing_account_for_assistant
from orchestra.routines.assistant_contact_notifications import (
    DELETION_SUBJECT,
    NOTIFICATION_DAYS,
    NOTIFICATION_SCHEDULE,
    build_deletion_email,
    build_warning_email,
    get_last_notification_day,
    get_notification_emails_for_ba,
    send_notification_emails,
    set_last_notification_day,
)
from orchestra.settings import settings
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)

GRACE_PERIOD_DAYS = 14


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SuspensionAccountResult:
    """Summary of suspension processing for a single billing account."""

    billing_account_id: int
    restored_contacts: int = 0
    deleted_contacts: int = 0
    notifications_sent: List[int] = field(default_factory=list)
    deletion_email_sent: bool = False
    errors: List[str] = field(default_factory=list)

    @property
    def reminder_sent(self) -> bool:
        """Backward-compatible: True if any reminder notification was sent."""
        return len(self.notifications_sent) > 0


@dataclass
class SuspensionResult:
    """Aggregate result of the suspension routine."""

    total_grace_contacts_found: int = 0
    accounts_processed: int = 0
    contacts_restored: int = 0
    contacts_deleted: int = 0
    reminders_sent: int = 0
    deletion_emails_sent: int = 0
    account_results: List[SuspensionAccountResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Deprovisioning
# ---------------------------------------------------------------------------


async def _deprovision_contact(contact: AssistantContact) -> None:
    """Deprovision the external resource for a contact.

    Calls the appropriate infra deletion function (Twilio / Google Workspace).
    """
    from orchestra.web.api.utils.assistant_infra import (
        delete_email,
        delete_phone_number,
    )

    if contact.contact_type == "phone":
        if contact.contact_value:
            await delete_phone_number(contact.contact_value)
            logger.info(
                "Deprovisioned phone %s (contact %d)",
                contact.contact_value,
                contact.id,
            )
    elif contact.contact_type == "email":
        if contact.contact_value:
            await delete_email(contact.contact_value)
            logger.info(
                "Deprovisioned email %s (contact %d)",
                contact.contact_value,
                contact.id,
            )
    elif contact.contact_type == "whatsapp":
        # WhatsApp senders on our shared account don't need explicit
        # deprovisioning – clearing the mapping is sufficient.
        logger.info(
            "WhatsApp contact %s cleared (contact %d, no external deprovision needed)",
            contact.contact_value,
            contact.id,
        )


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _group_grace_contacts_by_ba(
    session: Session,
    contacts: List[AssistantContact],
) -> Dict[int, Tuple[BillingAccount, List[AssistantContact]]]:
    """Group grace-period contacts by billing account.

    Reuses the ``_get_billing_account_for_assistant`` helper from the levy
    module.
    """
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
                    "Grace-period contact %d references non-existent "
                    "assistant %d – skipping",
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
                "No billing account for assistant %d (contact %d) – skipping",
                aid,
                contact.id,
            )
            continue

        if ba.id not in groups:
            groups[ba.id] = (ba, [])
        groups[ba.id][1].append(contact)

    return groups


# ---------------------------------------------------------------------------
# Core routine
# ---------------------------------------------------------------------------


async def suspend_overdue_contacts(
    session: Session | None = None,
) -> SuspensionResult:
    """Delete contacts that have been in ``grace_period`` for ≥ 14 days.

    Also:
    - Clears the grace period on contacts whose billing account now has
      sufficient credits (≥ 0).
    - Sends reminder/warning emails on Days 7 and 13.
    - Sends deletion notification emails when contacts are removed.

    Args:
        session: Optional SQLAlchemy session.  If ``None``, a fresh session
            is created.

    Returns:
        :class:`SuspensionResult` with aggregate metrics.
    """
    if session is not None:
        return await _suspend_in_session(session)

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as new_session:
        return await _suspend_in_session(new_session)


async def _suspend_in_session(session: Session) -> SuspensionResult:
    """Internal implementation of the suspension routine."""
    result = SuspensionResult()
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(days=GRACE_PERIOD_DAYS)

    try:
        # 1. Fetch all contacts currently in grace_period.
        grace_contacts: List[AssistantContact] = (
            session.query(AssistantContact)
            .filter(AssistantContact.status == "grace_period")
            .all()
        )

        if not grace_contacts:
            logger.info("Suspension routine: no contacts in grace_period.")
            return result

        result.total_grace_contacts_found = len(grace_contacts)

        # 2. Group by billing account.
        groups = _group_grace_contacts_by_ba(session, grace_contacts)

        # 3. Process each billing account.
        for ba_id, (ba, contacts) in groups.items():
            ar = await _process_ba_grace_contacts(
                session,
                ba,
                contacts,
                now,
                cutoff,
            )
            result.account_results.append(ar)
            result.accounts_processed += 1
            result.contacts_restored += ar.restored_contacts
            result.contacts_deleted += ar.deleted_contacts
            if ar.notifications_sent:
                result.reminders_sent += len(ar.notifications_sent)
            if ar.deletion_email_sent:
                result.deletion_emails_sent += 1

        session.commit()

        logger.info(
            "Suspension routine complete: %d grace contacts found, "
            "%d restored, %d deleted, %d reminders sent, "
            "%d deletion emails sent.",
            result.total_grace_contacts_found,
            result.contacts_restored,
            result.contacts_deleted,
            result.reminders_sent,
            result.deletion_emails_sent,
        )

    except Exception:
        session.rollback()
        logger.exception("Suspension routine failed – rolled back.")
        raise

    return result


async def _process_ba_grace_contacts(
    session: Session,
    ba: BillingAccount,
    contacts: List[AssistantContact],
    now: _dt.datetime,
    cutoff: _dt.datetime,
) -> SuspensionAccountResult:
    """Process grace-period contacts for a single billing account.

    If credits are ≥ 0 → restore all contacts to ``active``.
    Otherwise:
      - Contacts past the 14-day cutoff → deprovision + soft-delete.
      - Contacts on notification days (7, 13) → send appropriate email.
    """
    ar = SuspensionAccountResult(billing_account_id=ba.id)

    # If the billing account has been topped up, clear grace period.
    if ba.credits >= 0:
        assistant_ids_to_reawaken: Set[int] = set()
        for contact in contacts:
            contact.status = "active"
            contact.grace_period_started_at = None
            # Clear notification tracking
            set_last_notification_day(contact, 0)
            ar.restored_contacts += 1
            assistant_ids_to_reawaken.add(contact.assistant_id)

        # If account was PAST_DUE, set it back to ACTIVE
        if ba.account_status in ("PAST_DUE", "SUSPENDED"):
            ba.account_status = "ACTIVE"

        # Reawaken affected assistants
        for aid in assistant_ids_to_reawaken:
            try:
                from orchestra.web.api.utils.assistant_infra import reawaken_assistant

                await reawaken_assistant(
                    str(aid),
                    is_staging=settings.is_staging,
                )
            except Exception as e:
                logger.warning(
                    "Failed to reawaken assistant %d after grace period "
                    "restoration: %s",
                    aid,
                    e,
                )

        logger.info(
            "Billing account %d has credits ≥ 0: restored %d contacts.",
            ba.id,
            ar.restored_contacts,
        )
        return ar

    # Credits are still negative – process each contact individually.
    overdue_contacts: List[AssistantContact] = []
    # Maps notification_day → list of contacts that need that notification
    notification_buckets: Dict[int, List[AssistantContact]] = {}

    for contact in contacts:
        if contact.grace_period_started_at is None:
            continue

        days_elapsed = (now - contact.grace_period_started_at).days

        if days_elapsed >= GRACE_PERIOD_DAYS:
            overdue_contacts.append(contact)
        else:
            # Find the highest applicable notification day that hasn't
            # been sent yet.
            last_notified = get_last_notification_day(contact)
            for notification_day in reversed(NOTIFICATION_DAYS):
                if (
                    days_elapsed >= notification_day
                    and last_notified < notification_day
                ):
                    notification_buckets.setdefault(notification_day, []).append(
                        contact,
                    )
                    break

    # --- Deprovision overdue contacts ---
    assistant_ids_to_reawaken: Set[int] = set()
    had_deletions = False

    for contact in overdue_contacts:
        try:
            # 1. Deprovision external resource
            await _deprovision_contact(contact)

            # 2. Track assistant for reawaken
            assistant_ids_to_reawaken.add(contact.assistant_id)

            # 3. Soft-delete the contact
            contact.status = "deleted"
            contact.deleted_at = now
            ar.deleted_contacts += 1
            had_deletions = True

        except Exception as e:
            error_msg = (
                f"Failed to deprovision contact {contact.id} "
                f"({contact.contact_type} = {contact.contact_value}): {e}"
            )
            logger.error(error_msg, exc_info=True)
            ar.errors.append(error_msg)

    # 4. Reawaken affected assistants
    for aid in assistant_ids_to_reawaken:
        try:
            from orchestra.web.api.utils.assistant_infra import reawaken_assistant

            await reawaken_assistant(
                str(aid),
                is_staging=settings.is_staging,
            )
        except Exception as e:
            logger.warning(
                "Failed to reawaken assistant %d after contact deletion: %s",
                aid,
                e,
            )

    # 5. Send deletion notification
    if had_deletions:
        try:
            notification_emails = get_notification_emails_for_ba(session, ba)
            await send_notification_emails(
                notification_emails,
                DELETION_SUBJECT,
                build_deletion_email(),
            )
            ar.deletion_email_sent = True
        except Exception as e:
            logger.error(
                "Failed to send deletion notification for BA %d: %s",
                ba.id,
                e,
            )

    # --- Send scheduled reminder/warning emails ---
    for notification_day in sorted(notification_buckets.keys()):
        notif_contacts = notification_buckets[notification_day]
        schedule_entry = NOTIFICATION_SCHEDULE[notification_day]

        try:
            notification_emails = get_notification_emails_for_ba(session, ba)
            await send_notification_emails(
                notification_emails,
                schedule_entry["subject"],
                build_warning_email(schedule_entry["days_remaining"]),
            )

            # Record that this notification day was sent
            for c in notif_contacts:
                set_last_notification_day(c, notification_day)

            ar.notifications_sent.append(notification_day)

            logger.info(
                "Day-%d notification sent for BA %d (%d contacts).",
                notification_day,
                ba.id,
                len(notif_contacts),
            )
        except Exception as e:
            logger.error(
                "Failed to send Day-%d notification for BA %d: %s",
                notification_day,
                ba.id,
                e,
            )

    return ar


# Re-export for backward compatibility (moved to AssistantContactDAO class)
from orchestra.db.dao.assistant_contact_dao import (  # noqa: F401, E402
    AssistantContactDAO,
)
