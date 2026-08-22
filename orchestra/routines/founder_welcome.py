"""Personal founder welcome email from daniel@unify.ai.

Sent once when a personal Coordinator is provisioned at signup (same hooks
as the twin@ Coordinator welcome). Complements the product welcome with a
human note that invites reply to a real, manually-monitored inbox.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

FOUNDER_WELCOME_SUBJECT = "Welcome to Unify — a note from Dan"


def _salutation(owner_first_name: Optional[str]) -> str:
    if owner_first_name and owner_first_name.strip():
        return f"Hey {owner_first_name.strip()},"
    return "Hey,"


def build_founder_welcome_email(*, owner_first_name: Optional[str]) -> str:
    """Build the HTML body for Dan's personal signup welcome."""
    salutation = _salutation(owner_first_name)
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <p>{salutation}</p>

        <p>
            I'm Dan, one of the humans behind Unify 👋
        </p>

        <p>
            Just wanted to welcome you personally and let you know I'm
            here to help.
        </p>

        <p>
            If you have any questions or feedback, just hit reply — this
            is my real email and I read every message. 🫶
        </p>

        <p>
            My the droid be with you!<br/>
            Dan
        </p>
    </body>
    </html>
    """


def get_founder_welcome_from_email() -> Optional[str]:
    """Return the from/impersonate address, or None when the feature is off."""
    from orchestra.settings import settings

    if not settings.founder_welcome_enabled:
        return None
    address = (settings.founder_welcome_from_email or "").strip()
    return address or None


async def send_founder_welcome_email(
    *,
    recipient_email: Optional[str],
    owner_first_name: Optional[str],
) -> bool:
    """Best-effort personal welcome from Dan's mailbox."""
    if not recipient_email:
        return False

    from_address = get_founder_welcome_from_email()
    if not from_address:
        logger.info(
            "Founder welcome email disabled or from-address unset; skipping.",
        )
        return False

    from orchestra.web.api.utils.email import send_email_async_result

    body = build_founder_welcome_email(owner_first_name=owner_first_name)
    result = await send_email_async_result(
        to_email=recipient_email,
        email_subject=FOUNDER_WELCOME_SUBJECT,
        email_body=body,
        from_email=from_address,
        impersonate_email=from_address,
    )
    if result:
        logger.info(
            "Founder welcome email sent to %s from %s",
            recipient_email,
            from_address,
        )
        return True

    logger.warning(
        "Failed to send founder welcome email to %s from %s",
        recipient_email,
        from_address,
    )
    return False


async def send_signup_welcome_emails_safe(
    *,
    recipient_email: Optional[str],
    owner_first_name: Optional[str],
    user_id: object = "?",
) -> None:
    """Best-effort twin@ + founder welcomes; never raises."""
    try:
        from orchestra.routines.inactivity_notifications import (
            send_coordinator_welcome_email,
        )

        await send_coordinator_welcome_email(
            recipient_email=recipient_email,
            owner_first_name=owner_first_name,
        )
    except Exception:
        logger.warning(
            "Failed to send Coordinator welcome email for user %s",
            user_id,
            exc_info=True,
        )

    try:
        await send_founder_welcome_email(
            recipient_email=recipient_email,
            owner_first_name=owner_first_name,
        )
    except Exception:
        logger.warning(
            "Failed to send founder welcome email for user %s",
            user_id,
            exc_info=True,
        )
