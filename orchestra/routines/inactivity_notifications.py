"""Coordinator-voiced emails from the shared twin@ mailbox.

Two messages live here:

- **Welcome** — sent once when a personal Coordinator is provisioned at
  signup (see signup paths in ``orchestra.web.api.auth.views`` /
  ``orchestra.web.api.users.views``).
- **Inactivity re-engagement** — sent by
  :mod:`orchestra.routines.inactivity_followup` when a user has been
  quiet for ``settings.inactivity_followup_days``. Soft check-in only;
  never deletion, suspension, or billing language.

Both are sent **from the shared Coordinator mailbox** (the
``UNITY_COORDINATOR_EMAIL_ADDRESS`` setting, surfaced via
:func:`orchestra.services.universal_unity_email.get_universal_unity_email_address`)
rather than the general ``hello@unify.ai`` outbound address, so the
message lands in the user's inbox as if their Coordinator wrote it.
"""

from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


WELCOME_SUBJECT = "Welcome to Unify — I'm T-W1N, your coordinator"
FOLLOWUP_SUBJECT = "All good?"

_CONSOLE_URL = "https://console.unify.ai/"
_FOOTER = (
    '<hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">'
    '<p style="font-size: 12px; color: #888;">'
    "This is an automated message from your Unify coordinator. Please "
    "chat with me on the console rather than replying to this email."
    "</p>"
)


def _salutation(owner_first_name: Optional[str]) -> str:
    if owner_first_name and owner_first_name.strip():
        return f"Hi {owner_first_name.strip()},"
    return "Hi,"


def _followup_salutation(owner_first_name: Optional[str]) -> str:
    if owner_first_name and owner_first_name.strip():
        return f"Helloooo {owner_first_name.strip()},"
    return "Helloooo,"


# ---------------------------------------------------------------------------
# Email body builders
# ---------------------------------------------------------------------------


def build_coordinator_welcome_email(*, owner_first_name: Optional[str]) -> str:
    """Build the HTML body for the Coordinator's welcome email.

    First-person, in the Coordinator's (Coordinator's) voice. Introduces the
    coordinator and points the user at the console to get started.
    """
    salutation = _salutation(owner_first_name)
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <p>{salutation}</p>

        <p>
            I'm T-W1N, your personal coordinator on Unify. Welcome aboard!
            I'm here to help you get things done — I can take on tasks
            directly, or bring in specialist assistants and coordinate
            their work for you.
        </p>

        <p>
            The best way to get started is to say hello and tell me what
            you're working on. You can chat with me any time on the
            console:
        </p>

        <p><a href="{_CONSOLE_URL}">{_CONSOLE_URL}</a></p>

        <p>
            Looking forward to working together,<br/>— T-W1N
        </p>

        {_FOOTER}
    </body>
    </html>
    """


def build_coordinator_inactivity_followup_email(
    *,
    owner_first_name: Optional[str],
) -> str:
    """Build the HTML body for a soft inactivity re-engagement email.

    First-person T-W1N check-in that invites a reply (inbound email
    wakes the coordinator). No console link, and no account deletion,
    suspension, or billing language — contact lifecycle stays with the
    billing suspension routine.
    """
    salutation = _followup_salutation(owner_first_name)
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <p>{salutation}</p>

        <p>Haven't heard from you in a while — all good on your end?</p>

        <p>Anything I can help with?</p>

        <p>
            Just reply to this email if there's anything on your plate
            for me to pick up!
        </p>

        <p>Your friendly neighbourhood T-W1N</p>
    </body>
    </html>
    """


# ---------------------------------------------------------------------------
# Sending helper (from the shared Coordinator mailbox)
# ---------------------------------------------------------------------------


async def send_coordinator_emails(
    recipients: List[str],
    subject: str,
    body: str,
) -> bool:
    """Send a Coordinator-voiced email from the shared Coordinator mailbox.

    Routes through the Gmail service account, sending *from* and
    impersonating the ``UNITY_COORDINATOR_EMAIL_ADDRESS`` mailbox so the
    message appears to come from the user's coordinator rather than the
    general outbound address.

    Returns ``True`` only when every recipient send succeeded. No-ops
    (returns ``False``) when the coordinator mailbox is not configured —
    typical in local dev — so callers stay safe to run there.
    """
    from orchestra.services.universal_unity_email import (
        get_universal_unity_email_address,
    )
    from orchestra.web.api.utils.email import send_email_async

    from_address = get_universal_unity_email_address()
    if not from_address:
        logger.warning(
            "Coordinator mailbox (UNITY_COORDINATOR_EMAIL_ADDRESS) not "
            "configured; skipping coordinator email %r.",
            subject,
        )
        return False

    all_sent = True
    for email_addr in recipients:
        success = await send_email_async(
            to_email=email_addr,
            email_subject=subject,
            email_body=body,
            from_email=from_address,
            impersonate_email=from_address,
        )
        if success:
            logger.info("Coordinator email sent to %s: %s", email_addr, subject)
        else:
            all_sent = False
            logger.warning(
                "Failed to send coordinator email to %s: %s",
                email_addr,
                subject,
            )
    return all_sent


async def send_coordinator_welcome_email(
    *,
    recipient_email: Optional[str],
    owner_first_name: Optional[str],
) -> bool:
    """Best-effort welcome send for a freshly-provisioned Coordinator.

    Returns ``False`` (without raising) when there's no recipient or the
    coordinator mailbox is unconfigured, so signup flows can call this
    without guarding the happy path.
    """
    if not recipient_email:
        return False
    return await send_coordinator_emails(
        [recipient_email],
        WELCOME_SUBJECT,
        build_coordinator_welcome_email(owner_first_name=owner_first_name),
    )


async def send_coordinator_inactivity_followup_email(
    *,
    recipient_email: Optional[str],
    owner_first_name: Optional[str],
) -> bool:
    """Best-effort inactivity re-engagement send from the shared mailbox.

    Returns ``False`` when there is no recipient or the mailbox is
    unconfigured. Callers must only stamp ``last_followup_sent_at`` after
    a ``True`` return.
    """
    if not recipient_email:
        return False
    return await send_coordinator_emails(
        [recipient_email],
        FOLLOWUP_SUBJECT,
        build_coordinator_inactivity_followup_email(
            owner_first_name=owner_first_name,
        ),
    )
