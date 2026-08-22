"""Founder interview-ask emails from dan.lenton@unify.ai with Cal.com booking.

Fully automated one-shot outreach across three cohorts:

- ``engaged_quiet`` — used the product, then went quiet
- ``never_engaged`` — signed up but never had real product activity
- ``engaged_active`` — still active after the account has aged a bit

Scheduling: ``.github/workflows/founder-interview-ask.yml`` POSTs
``/v0/admin/assistants/founder-interview-ask`` daily.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

VARIANT_ENGAGED_QUIET = "engaged_quiet"
VARIANT_NEVER_ENGAGED = "never_engaged"
VARIANT_ENGAGED_ACTIVE = "engaged_active"

INTERVIEW_SUBJECTS = {
    VARIANT_ENGAGED_QUIET: "Got 15 minutes to tell me what you think of Unify?",
    VARIANT_NEVER_ENGAGED: "Quick question about your Unify signup",
    VARIANT_ENGAGED_ACTIVE: "I'd love your honest take on Unify",
}


def _salutation(owner_first_name: Optional[str]) -> str:
    if owner_first_name and owner_first_name.strip():
        return f"Hey {owner_first_name.strip()},"
    return "Hey,"


def get_founder_interview_from_email() -> Optional[str]:
    """Return the from/impersonate address, or None when the feature is off."""
    from orchestra.settings import settings

    if not settings.founder_interview_enabled:
        return None
    address = (settings.founder_interview_from_email or "").strip()
    return address or None


def get_founder_interview_cal_url() -> str:
    from orchestra.settings import settings

    return (settings.founder_interview_cal_url or "").strip() or (
        "https://cal.com/team/unify/chat"
    )


def build_founder_interview_email(
    *,
    owner_first_name: Optional[str],
    variant: str,
    cal_url: Optional[str] = None,
) -> str:
    """Build HTML for a founder interview ask."""
    salutation = _salutation(owner_first_name)
    link = (cal_url or get_founder_interview_cal_url()).strip()
    variant = variant if variant in INTERVIEW_SUBJECTS else VARIANT_ENGAGED_QUIET

    if variant == VARIANT_NEVER_ENGAGED:
        middle = """
        <p>
            I saw you signed up for Unify but didn't get a chance to dig
            in. Totally fine — I'd love to understand what you were hoping
            it'd do, and what got in the way.
        </p>
        """
    elif variant == VARIANT_ENGAGED_ACTIVE:
        middle = """
        <p>
            You've been using Unify a bit — thank you. I'd love 15 minutes
            to hear what's working and what isn't, straight from you.
        </p>
        """
    else:
        middle = """
        <p>
            I noticed you tried Unify and then went quiet. Totally fine —
            I'd love to learn what clicked and what didn't.
        </p>
        """

    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <p>{salutation}</p>

        <p>
            I'm Dan again — one of the humans behind Unify 👋
        </p>

        {middle}

        <p>
            Would you be open to a quick 15-min chat? You can grab a time
            here:
        </p>

        <p><a href="{link}">{link}</a></p>

        <p>
            Or just reply with whatever's on your mind — I read every
            email. 🫶
        </p>

        <p>
            My the droid be with you!<br/>
            Dan
        </p>
    </body>
    </html>
    """


def founder_interview_subject(variant: str) -> str:
    return INTERVIEW_SUBJECTS.get(variant, INTERVIEW_SUBJECTS[VARIANT_ENGAGED_QUIET])


async def send_founder_interview_email(
    *,
    recipient_email: Optional[str],
    owner_first_name: Optional[str],
    variant: str,
) -> bool:
    """Best-effort interview ask from Dan's mailbox."""
    if not recipient_email:
        return False

    from_address = get_founder_interview_from_email()
    if not from_address:
        logger.info(
            "Founder interview asks disabled or from-address unset; skipping.",
        )
        return False

    from orchestra.web.api.utils.email import send_email_async_result

    subject = founder_interview_subject(variant)
    body = build_founder_interview_email(
        owner_first_name=owner_first_name,
        variant=variant,
    )
    result = await send_email_async_result(
        to_email=recipient_email,
        email_subject=subject,
        email_body=body,
        from_email=from_address,
        impersonate_email=from_address,
    )
    if result:
        logger.info(
            "Founder interview ask (%s) sent to %s from %s",
            variant,
            recipient_email,
            from_address,
        )
        return True

    logger.warning(
        "Failed to send founder interview ask (%s) to %s from %s",
        variant,
        recipient_email,
        from_address,
    )
    return False
