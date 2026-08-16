"""Coordinator-voiced emails from the shared twin@ mailbox.

Two message families live here:

- **Welcome** — sent once when a personal Coordinator is provisioned at
  signup (see signup paths in ``orchestra.web.api.auth.views`` /
  ``orchestra.web.api.users.views``).
- **Inactivity re-engagement** — up to three staged check-ins per silence,
  sent by :mod:`orchestra.routines.inactivity_followup`. Soft check-in
  only; never deletion, suspension, or billing language.

Both are sent **from the shared Coordinator mailbox** (the
``UNIFY_COORDINATOR_EMAIL_ADDRESS`` setting, surfaced via
:func:`orchestra.services.universal_unity_email.get_universal_unity_email_address`)
rather than the general ``hello@unify.ai`` outbound address, so the
message lands in the user's inbox as if their Coordinator wrote it.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


WELCOME_SUBJECT = "Welcome to Unify — I'm T-W1N, your coordinator"
FOLLOWUP_SUBJECTS = {
    1: "Just checking in",
    2: "Anything I can help with?",
    3: "One last note from me",
}
FOLLOWUP_SUBJECT = FOLLOWUP_SUBJECTS[1]

_CONSOLE_URL = "https://console.unify.ai/"
_FOOTER = (
    '<hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">'
    '<p style="font-size: 12px; color: #888;">'
    "This is an automated message from your Unify coordinator. "
    "Reply to this email if you'd like to chat — or message me on the console."
    "</p>"
)


def _salutation(owner_first_name: Optional[str]) -> str:
    if owner_first_name and owner_first_name.strip():
        return f"Hi {owner_first_name.strip()},"
    return "Hi,"


def _followup_salutation(owner_first_name: Optional[str]) -> str:
    if owner_first_name and owner_first_name.strip():
        return f"Hey {owner_first_name.strip()},"
    return "Hey,"


# ---------------------------------------------------------------------------
# Email body builders
# ---------------------------------------------------------------------------


def build_coordinator_welcome_email(*, owner_first_name: Optional[str]) -> str:
    """Build the HTML body for the Coordinator's welcome email."""
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
    stage: int = 1,
    has_engaged: bool = False,
) -> str:
    """Build HTML for staged inactivity re-engagement emails.

    ``stage`` is 1-indexed within the current silence (1..3).
    ``has_engaged`` is False when the owner never had real product activity
    (so we avoid contrived "great to meet you yesterday" copy).
    """
    salutation = _followup_salutation(owner_first_name)
    stage = max(1, min(int(stage), 3))

    if stage == 1:
        if has_engaged:
            body = f"""
        <p>{salutation}</p>
        <p>
            Great to meet you yesterday — just thought I'd touch base and
            see if there's anything else I can help with?
        </p>
        <p>Let me know!</p>
        <p>
            Thanks,<br/>
            Your digital T-W1N
        </p>
            """
        else:
            body = f"""
        <p>{salutation}</p>
        <p>
            Just checking in — I'm here whenever you want to try something
            on Unify. Anything I can help with?
        </p>
        <p>
            Thanks,<br/>
            Your digital T-W1N
        </p>
            """
    elif stage == 2:
        if has_engaged:
            body = f"""
        <p>{salutation}</p>
        <p>
            Checking in again — happy to pick up where we left off, or
            help with something new.
        </p>
        <p>
            If anything didn't click last time, I'd love a quick line on
            how I could've been more useful — just reply to this email.
        </p>
        <p>
            Thanks,<br/>
            Your digital T-W1N
        </p>
            """
        else:
            body = f"""
        <p>{salutation}</p>
        <p>
            Still here if you want a hand getting started — research,
            coding, inbox triage, or coordinating specialist assistants.
        </p>
        <p>Reply anytime and I'll jump in.</p>
        <p>
            Thanks,<br/>
            Your digital T-W1N
        </p>
            """
    else:
        body = f"""
        <p>{salutation}</p>
        <p>
            One last note from me — I'll leave you be after this unless
            you reach out.
        </p>
        <p>
            If Unify wasn't useful, a quick reply on what would've helped
            more would mean a lot. Otherwise, I'm here whenever you are.
        </p>
        <p>
            Thanks,<br/>
            Your digital T-W1N
        </p>
        """

    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        {body}
        {_FOOTER}
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
) -> Tuple[bool, Optional[str]]:
    """Send a Coordinator-voiced email from the shared Coordinator mailbox.

    Returns ``(all_sent, thread_id)``. ``thread_id`` is the Gmail thread
    id from the last successful send (used to ignore check-in replies as
    product activity). No-ops with ``(False, None)`` when the coordinator
    mailbox is not configured.
    """
    from orchestra.services.universal_unity_email import (
        get_universal_unity_email_address,
    )
    from orchestra.web.api.utils.email import send_email_async_result

    from_address = get_universal_unity_email_address()
    if not from_address:
        logger.warning(
            "Coordinator mailbox (UNIFY_COORDINATOR_EMAIL_ADDRESS) not "
            "configured; skipping coordinator email %r.",
            subject,
        )
        return False, None

    all_sent = True
    thread_id: Optional[str] = None
    for email_addr in recipients:
        result = await send_email_async_result(
            to_email=email_addr,
            email_subject=subject,
            email_body=body,
            from_email=from_address,
            impersonate_email=from_address,
        )
        if result:
            thread_id = result.get("threadId") or result.get("thread_id") or thread_id
            logger.info("Coordinator email sent to %s: %s", email_addr, subject)
        else:
            all_sent = False
            logger.warning(
                "Failed to send coordinator email to %s: %s",
                email_addr,
                subject,
            )
    return all_sent, thread_id


async def send_coordinator_welcome_email(
    *,
    recipient_email: Optional[str],
    owner_first_name: Optional[str],
) -> bool:
    """Best-effort welcome send for a freshly-provisioned Coordinator."""
    if not recipient_email:
        return False
    sent, _thread_id = await send_coordinator_emails(
        [recipient_email],
        WELCOME_SUBJECT,
        build_coordinator_welcome_email(owner_first_name=owner_first_name),
    )
    return sent


async def send_coordinator_inactivity_followup_email(
    *,
    recipient_email: Optional[str],
    owner_first_name: Optional[str],
    stage: int = 1,
    has_engaged: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Best-effort inactivity re-engagement send from the shared mailbox.

    Returns ``(sent, gmail_thread_id)``. Callers must only stamp follow-up
    bookkeeping after ``sent`` is True.
    """
    if not recipient_email:
        return False, None
    stage = max(1, min(int(stage), 3))
    subject = FOLLOWUP_SUBJECTS.get(stage, FOLLOWUP_SUBJECTS[1])
    return await send_coordinator_emails(
        [recipient_email],
        subject,
        build_coordinator_inactivity_followup_email(
            owner_first_name=owner_first_name,
            stage=stage,
            has_engaged=has_engaged,
        ),
    )
