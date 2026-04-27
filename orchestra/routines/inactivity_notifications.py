"""Email templates and helpers for inactivity-followup deletion notifications.

Sent by :mod:`orchestra.routines.inactivity_followup` immediately before
the assistant row is hard-deleted, so the human who hired the assistant
finds out they were removed because of the prolonged silence.

Recipient resolution: the *creator/lifecycle owner* of the assistant —
``Assistant.user_id``. Per the model docstring, this is:

  * the owner for a personal assistant (``organization_id`` IS NULL), and
  * the org member who created the assistant for an org assistant (NOT
    the org owner).

Send transport reuses ``assistant_contact_notifications.send_notification_emails``
so we route through the same Google service-account / "general outbound"
mailbox configured by ``ONBOARDING_EMAIL`` and the SA key path.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from orchestra.db.models.orchestra_models import Assistant, User

logger = logging.getLogger(__name__)


DELETION_SUBJECT = "Your Unify assistant has been removed"

_CONSOLE_URL = "https://console.unify.ai/"
_FOOTER = (
    '<hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">'
    '<p style="font-size: 12px; color: #888;">'
    "This is an automated notification from Unify. Please do not reply "
    "to this email."
    "</p>"
)


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------


def get_owner_email_for_assistant(
    session: Session,
    assistant: Assistant,
) -> Optional[str]:
    """Return the email of the assistant's lifecycle owner, or ``None``.

    Always uses ``Assistant.user_id`` — for both personal *and* org
    assistants. The ``user_id`` column is the creator/lifecycle owner;
    org assistants intentionally notify the creating member, not the
    organization owner.
    """
    if assistant.user_id is None:
        return None
    user = session.query(User).filter(User.id == assistant.user_id).first()
    if user is None:
        return None
    email = getattr(user, "email", None)
    return email or None


def get_owner_first_name_for_assistant(
    session: Session,
    assistant: Assistant,
) -> Optional[str]:
    """Return the first name of the assistant's lifecycle owner, or ``None``.

    Reads ``User.name`` — the orchestra User model uses ``name`` for the
    salutation/first name and ``last_name`` for the surname.
    """
    if assistant.user_id is None:
        return None
    user = session.query(User).filter(User.id == assistant.user_id).first()
    if user is None:
        return None
    first_name = getattr(user, "name", None)
    return first_name or None


# ---------------------------------------------------------------------------
# Email body builder
# ---------------------------------------------------------------------------


def _assistant_display_name(assistant: Assistant) -> str:
    """Return the most human-readable label for an assistant.

    Prefers ``first_name surname`` when both exist; falls back to either
    one on its own; falls back to ``f"agent {agent_id}"`` as a last resort.
    """
    first = (assistant.first_name or "").strip()
    surname = (assistant.surname or "").strip()
    full = f"{first} {surname}".strip()
    if full:
        return full
    return f"agent {assistant.agent_id}"


def _assistant_short_name(assistant: Assistant) -> str:
    """Return just a first name (or fall-back) for the second mention."""
    first = (assistant.first_name or "").strip()
    if first:
        return first
    return _assistant_display_name(assistant)


def build_deletion_email(
    *,
    assistant: Assistant,
    owner_first_name: Optional[str],
    days: int,
) -> str:
    """Build the HTML body for the inactivity deletion notification.

    The copy follows the product brief: explicit acknowledgement that
    we previously followed up, factual statement of the inactivity
    window in days, and a path forward (hire a new assistant).
    """
    salutation = (
        f"Hi {owner_first_name.strip()},"
        if (owner_first_name and owner_first_name.strip())
        else "Hi,"
    )
    full_name = _assistant_display_name(assistant)
    short_name = _assistant_short_name(assistant)
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <h2 style="color: #444;">Your Unify assistant has been removed</h2>

        <p>{salutation}</p>

        <p>
            We previously followed up on your assistant
            <strong>{full_name}</strong> and you either didn't respond or
            chose to not keep your assistant.
        </p>

        <p>
            We're removing {short_name} now after <strong>{days} days</strong>
            of inactivity. The assistant's contact details (phone, email,
            WhatsApp) have been released and the assistant has been
            deleted from your account.
        </p>

        <p>
            If you'd like a fresh start, you can hire a new assistant any
            time at <a href="{_CONSOLE_URL}">{_CONSOLE_URL}</a>.
        </p>

        <p>— The Unify team</p>

        {_FOOTER}
    </body>
    </html>
    """
