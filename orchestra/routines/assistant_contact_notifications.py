"""Shared email templates and helpers for contact-billing notifications.

Provides reusable email body builders, subject lines, recipient resolution,
and notification sending utilities used by both the resource levy (Day 1)
and the resource suspension routine (Day 7 / 13 / deletion).

Notification schedule:

| Day | Event                                        | Channel         |
|-----|----------------------------------------------|-----------------|
| 1   | Levy charged, credits insufficient           | Email           |
| 7   | Warning: 7 days until resource deletion      | Email           |
| 13  | Final warning: deletion tomorrow             | Email           |
| 14+ | Resources deleted                            | Email           |
"""

from __future__ import annotations

import logging
from typing import Dict, List

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import (
    AssistantContact,
    BillingAccount,
    Organization,
    User,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Notification schedule
# ---------------------------------------------------------------------------

#: Maps the grace-period day → (subject, days_remaining) for reminder-type
#: emails.  Day 1 (levy) and Day 14+ (deletion) have their own constants.
NOTIFICATION_SCHEDULE: Dict[int, dict] = {
    7: {
        "subject": "⚠️ Warning: Your assistant contacts will be deleted in 7 days",
        "days_remaining": 7,
    },
    13: {
        "subject": "🚨 Final Warning: Your assistant contacts will be deleted tomorrow",
        "days_remaining": 1,
    },
}

#: Sorted list of notification days for iteration.
NOTIFICATION_DAYS: List[int] = sorted(NOTIFICATION_SCHEDULE.keys())

LEVY_INSUFFICIENT_CREDITS_SUBJECT = (
    "⚠️ Insufficient Credits: Your assistant contact details are at risk"
)
DELETION_SUBJECT = "🗑️ Contact details deleted due to insufficient credits"


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------


def get_notification_emails_for_ba(
    session: Session,
    ba: BillingAccount,
) -> List[str]:
    """Return email addresses of account owners to notify.

    - For a personal billing account → user's email.
    - For an org billing account → org owner's email + billing_email if set.
    """
    emails: List[str] = []

    # Check if this is the billing account for a user
    user = session.query(User).filter(User.billing_account_id == ba.id).first()
    if user and user.email:
        emails.append(user.email)

    # Check if this is the billing account for an organization
    org = (
        session.query(Organization)
        .filter(Organization.billing_account_id == ba.id)
        .first()
    )
    if org:
        # Get org owner's email
        owner = session.query(User).filter(User.id == org.owner_id).first()
        if owner and owner.email and owner.email not in emails:
            emails.append(owner.email)

    # Also include billing_email if set and different
    if ba.billing_email and ba.billing_email not in emails:
        emails.append(ba.billing_email)

    return emails


# ---------------------------------------------------------------------------
# Email body builders
# ---------------------------------------------------------------------------

_BILLING_URL = "https://console.unify.ai/settings/billing"
_FOOTER = (
    '<hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">'
    '<p style="font-size: 12px; color: #888;">'
    "This is an automated notification from Unify. Please do not reply "
    "to this email."
    "</p>"
)


def build_insufficient_credits_email() -> str:
    """Day 1: sent by the levy when credits go negative."""
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <h2 style="color: #d97706;">Insufficient Credits</h2>

        <p>Your account has been charged for provisioned assistant contact
        details, but you do not have enough credits to cover the cost.</p>

        <p style="color: #d97706;">
            <strong>⚠️ Important:</strong> If you do not add credits within
            <strong>14 days</strong>, your contact details (phone numbers,
            email addresses) will be <strong>permanently deleted</strong>
            and the underlying resources will be released.
        </p>

        <p>To prevent this, please top up your credits in the
        <a href="{_BILLING_URL}">billing settings</a>.</p>

        {_FOOTER}
    </body>
    </html>
    """


def build_warning_email(days_remaining: int) -> str:
    """Build the HTML email body for grace-period warnings/reminders."""
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <h2 style="color: #d97706;">Insufficient Credits – Contact Details at Risk</h2>

        <p>Your account does not have enough credits to maintain your
        provisioned assistant contact details.</p>

        <p style="color: #d97706;">
            <strong>⚠️ Important:</strong> Your contact details will be
            <strong>permanently deleted in {days_remaining} day(s)</strong>
            unless you add credits to your account.
        </p>

        <p>Once deleted, phone numbers and email addresses will be
        released and <strong>cannot be recovered</strong>. You would need to
        provision new ones.</p>

        <p>To prevent deletion, please top up your credits in the
        <a href="{_BILLING_URL}">billing settings</a>.</p>

        {_FOOTER}
    </body>
    </html>
    """


def build_deletion_email() -> str:
    """Build the HTML email body for the deletion notification."""
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <h2 style="color: #dc2626;">Contact Details Deleted</h2>

        <p>Your provisioned assistant contact details have been deleted due
        to insufficient credits after a 14-day grace period.</p>

        <p>The underlying resources (phone numbers, email addresses) have
        been released and <strong>cannot be recovered</strong>. If you need
        new contact details, please provision them after adding credits.</p>

        <p>To add credits, visit your
        <a href="{_BILLING_URL}">billing settings</a>.</p>

        {_FOOTER}
    </body>
    </html>
    """


# ---------------------------------------------------------------------------
# Notification tracking via contact metadata
# ---------------------------------------------------------------------------


def get_last_notification_day(contact: AssistantContact) -> int:
    """Return the last notification day recorded for this contact (0 if none)."""
    if contact.metadata_ and isinstance(contact.metadata_, dict):
        return contact.metadata_.get("last_notification_day", 0)
    return 0


def set_last_notification_day(contact: AssistantContact, day: int) -> None:
    """Record the notification day on the contact's JSONB metadata.

    Reassigns the dict so SQLAlchemy detects the change without needing
    ``flag_modified``.
    """
    current = contact.metadata_ if isinstance(contact.metadata_, dict) else {}
    contact.metadata_ = {**current, "last_notification_day": day}


# ---------------------------------------------------------------------------
# Sending helpers
# ---------------------------------------------------------------------------


async def send_notification_emails(
    recipients: List[str],
    subject: str,
    body: str,
) -> None:
    """Send a notification email to all recipients (async)."""
    from orchestra.web.api.utils.email import send_email_async

    for email_addr in recipients:
        success = await send_email_async(
            to_email=email_addr,
            email_subject=subject,
            email_body=body,
            from_email="hello@unify.ai",
            impersonate_email="hello@unify.ai",
        )
        if success:
            logger.info("Notification sent to %s: %s", email_addr, subject)
        else:
            logger.warning(
                "Failed to send notification to %s: %s",
                email_addr,
                subject,
            )


def send_notification_emails_sync(
    recipients: List[str],
    subject: str,
    body: str,
) -> None:
    """Send a notification email to all recipients (synchronous).

    Used by the synchronous resource levy routine.
    """
    from orchestra.web.api.utils.email import _send_email_sync

    for email_addr in recipients:
        success = _send_email_sync(
            to_email=email_addr,
            email_subject=subject,
            email_body=body,
            sender_email="hello@unify.ai",
            impersonate_email="hello@unify.ai",
        )
        if success:
            logger.info("Notification sent to %s: %s", email_addr, subject)
        else:
            logger.warning(
                "Failed to send notification to %s: %s",
                email_addr,
                subject,
            )
