"""Email account holders before their unconsumed credit grants expire.

Self-serve subscription model: credit *grants* carry an expiry stamped
into ``credit_transaction.detail`` (see
:mod:`orchestra.lib.credit_grants`):

* ``trial`` — the one-time signup grant; expires 1 week after signup.
  Once gone there is nothing to replace it unless the account subscribes,
  so the reminder is a genuine "subscribe before you lose these" nudge.
* ``plan`` — the monthly/annual subscription grant; unused credits do not
  roll over (they reset on the next cycle), so the reminder is a
  "use-it-or-lose-it" nudge to spend the remainder before renewal.

This routine is the counterpart to ``credit_grant_expiry_sweep`` (which
*forfeits* the remainder once expired): a few days **before** the expiry
it emails the holder once, telling them how many credits are about to
lapse and when.

Idempotency
-----------
Each account carries ``billing_account.credit_expiry_reminded_at`` — the
``expires_at`` of the soonest grant we last reminded about. The routine
skips an account whose soonest upcoming expiry still equals that stamp,
so a daily run sends **at most one** email per distinct expiry. When the
next cycle's grant introduces a new (later) expiry, the stamp no longer
matches and the account becomes eligible again.

----------------------------------------------------------------------
Scheduling
----------------------------------------------------------------------

Runs **daily** via Cloud Scheduler, mirroring the other billing routines:

  * Suggested job ``orchestra-production-credit-expiry-reminder`` in
    project ``gcp-project-saas`` / location ``us-central1``.
  * Schedule ``0 9 * * *`` UTC (a friendly morning hour, well clear of
    the 01:00–01:30 suspension/forfeit window).
  * POSTs to
    ``https://api.unify.ai/v0/admin/billing/send-expiry-reminders`` with
    the static admin Bearer token.

Staging has no scheduled trigger — invoke on demand via the
``trigger_credit_expiry_reminder`` admin endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.db.models.enums import BillingMode
from orchestra.db.models.orchestra_models import BillingAccount, Organization, User
from orchestra.lib.credit_grants import (
    GRANT_KIND_TRIAL,
    GrantLot,
    compute_grant_lots,
    format_display_credits,
)
from orchestra.settings import settings
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


@dataclass
class ReminderResult:
    """Summary returned by :func:`send_credit_expiry_reminders`."""

    started_at: str = ""
    finished_at: str = ""
    window_days: int = 0
    accounts_scanned: int = 0
    reminders_sent: int = 0
    skipped_already_reminded: int = 0
    skipped_no_recipient: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "window_days": self.window_days,
            "accounts_scanned": self.accounts_scanned,
            "reminders_sent": self.reminders_sent,
            "skipped_already_reminded": self.skipped_already_reminded,
            "skipped_no_recipient": self.skipped_no_recipient,
            "errors": self.errors,
        }


def build_reminder_email(
    *,
    expiring_credits: Decimal,
    expires_at: datetime,
    has_trial: bool,
) -> tuple[str, str]:
    """Subject + HTML body for the pre-expiry credit reminder.

    Credit amounts are framed with the display multiplier
    (``format_display_credits``) so the numbers match the console.
    """
    credits = format_display_credits(expiring_credits)
    when = expires_at.strftime("%B %d, %Y")
    subject = "Your Unify credits expire soon"
    if has_trial:
        tail = (
            "<p>These are your <strong>free trial credits</strong> — once "
            "they expire they're gone. Subscribe to a plan before then to "
            "keep building without interruption.</p>"
        )
    else:
        tail = (
            "<p>Unused plan credits don't roll over — they reset when your "
            "plan renews. Put them to work before they expire so none go to "
            "waste.</p>"
        )
    body = (
        f"<p>Heads up — you have <strong>{credits} credits</strong> that "
        f"will expire on <strong>{when}</strong>.</p>"
        f"{tail}"
        "<p>You can review your balance and plan any time from your billing "
        "settings.</p>"
    )
    return subject, body


def _resolve_recipient(session: Session, ba: BillingAccount) -> Optional[str]:
    """Best-effort email for the account holder.

    A billing account is owned by *either* a personal user *or* an
    organization, so resolve both:

    - personal BA → the linked user's login email;
    - org BA → the org owner's login email.

    The billing email is no longer stored locally (it lives on the Stripe
    Customer), so the account-holder's login email is the reliable local
    signal in both cases.
    """
    user = (
        session.execute(
            select(User).where(User.billing_account_id == ba.id).limit(1),
        )
        .scalars()
        .first()
    )
    if user and user.email:
        return user.email

    org = (
        session.execute(
            select(Organization).where(Organization.billing_account_id == ba.id).limit(1),
        )
        .scalars()
        .first()
    )
    if org is not None:
        owner = session.get(User, org.owner_id)
        if owner and owner.email:
            return owner.email
    return None


def _deliver(recipient: str, subject: str, body: str) -> bool:
    """Send one reminder email (best-effort, synchronous).

    Isolated in a tiny module-level helper so the Gmail dependency is
    imported lazily (keeps the routine importable without mail config) and
    so tests can monkeypatch delivery without hitting the network.
    """
    from orchestra.web.api.utils.email import _send_email_sync

    # Send from the shared hello@ role mailbox rather than the personal
    # ONBOARDING_EMAIL mailbox. ONBOARDING_EMAIL is still required as the
    # impersonation fallback / mail config presence check.
    if not settings.google_service_account_key_path or not settings.google_service_sender_email:
        logger.warning(
            "Email not configured; skipping credit-expiry reminder to %s",
            recipient,
        )
        return False
    return _send_email_sync(
        recipient,
        subject,
        body,
        sender_email="hello@unify.ai",
        impersonate_email="hello@unify.ai",
    )


def send_credit_expiry_reminders(
    session: Optional[Session] = None,
    *,
    now: Optional[datetime] = None,
) -> ReminderResult:
    """Email holders whose credit grant expires within the reminder window.

    Args:
        session: DB session. A new one is created and committed if ``None``.
        now: Override the "current time" (for tests). Defaults to
            :func:`datetime.now` in UTC.
    """
    if session is not None:
        return _run_with_session(session, now=now, commit=False)

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as owned_session:
        return _run_with_session(owned_session, now=now, commit=True)


def _run_with_session(
    session: Session,
    *,
    now: Optional[datetime],
    commit: bool,
) -> ReminderResult:
    moment = now or datetime.now(timezone.utc)
    window_days = max(1, int(settings.credit_expiry_reminder_days))
    horizon = moment + timedelta(days=window_days)
    result = ReminderResult(
        started_at=moment.isoformat(),
        window_days=window_days,
    )

    # Candidate accounts: any with an *expiring* grant whose expiry falls
    # inside the reminder window (future, but within ``window_days``). The
    # per-account compute then decides whether an unconsumed remainder
    # actually survives and whether we already reminded for this expiry.
    candidate_ids = [
        row[0]
        for row in session.execute(
            text(
                """
                SELECT DISTINCT billing_account_id
                FROM credit_transaction
                WHERE amount > 0
                  AND detail ? 'expires_at'
                  AND detail ? 'grant_kind'
                  AND (detail->>'expires_at')::timestamptz > :moment
                  AND (detail->>'expires_at')::timestamptz <= :horizon
                """,
            ),
            {"moment": moment, "horizon": horizon},
        ).all()
    ]

    ba_dao = BillingAccountDAO(session)

    for ba_id in candidate_ids:
        result.accounts_scanned += 1
        try:
            ba = ba_dao.get_for_update(ba_id)
            if ba is None:
                continue
            # CREDITS accounts only — METERED enterprise accounts settle in
            # arrears and never hold expiring wallet grants.
            if ba_dao.resolve_billing_mode(ba) == BillingMode.METERED:
                continue

            lots: List[GrantLot] = compute_grant_lots(session, ba.id)
            upcoming = [
                lot
                for lot in lots
                if lot.remaining > 0 and moment < lot.expires_at <= horizon
            ]
            if not upcoming:
                continue

            soonest = min(lot.expires_at for lot in upcoming)
            # Only remind about the soonest-expiring batch (a later grant
            # gets its own reminder when it enters the window next cycle).
            batch = [lot for lot in upcoming if lot.expires_at == soonest]
            expiring_credits = sum(
                (lot.remaining for lot in batch),
                Decimal("0"),
            )

            # Idempotency: one reminder per distinct expiry.
            if ba.credit_expiry_reminded_at is not None and _same_instant(
                ba.credit_expiry_reminded_at,
                soonest,
            ):
                result.skipped_already_reminded += 1
                continue

            recipient = _resolve_recipient(session, ba)
            if not recipient:
                result.skipped_no_recipient += 1
                continue

            subject, body = build_reminder_email(
                expiring_credits=expiring_credits,
                expires_at=soonest,
                has_trial=any(
                    lot.grant_kind == GRANT_KIND_TRIAL for lot in batch
                ),
            )
            if not _deliver(recipient, subject, body):
                # Delivery failed — don't stamp, so the next run retries.
                msg = f"Reminder delivery failed for BA {ba_id}"
                logger.warning(msg)
                result.errors.append(msg)
                continue

            ba.credit_expiry_reminded_at = soonest
            session.flush()
            result.reminders_sent += 1
        except Exception as e:  # noqa: BLE001
            # Per-account isolation: one account's failure is logged and
            # skipped so the rest of the batch still goes out.
            msg = f"Failed to send expiry reminder for BA {ba_id}: {e}"
            logger.exception(msg)
            result.errors.append(msg)
            continue

    if commit:
        session.commit()

    result.finished_at = datetime.now(timezone.utc).isoformat()
    logger.info(
        {
            "message": "Credit-expiry reminder run complete",
            **result.to_dict(),
        },
    )
    return result


def _same_instant(a: datetime, b: datetime) -> bool:
    """Compare two (possibly naive) timestamps as the same UTC instant."""
    aa = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
    bb = b if b.tzinfo else b.replace(tzinfo=timezone.utc)
    return aa == bb
