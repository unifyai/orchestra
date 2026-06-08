"""Forfeit the unconsumed remainder of expired credit grants.

Self-serve subscription model: credit *grants* carry an expiry stamped
into ``credit_transaction.detail`` (see
:mod:`orchestra.lib.credit_grants`):

* ``trial`` — the one-time signup grant; expires 1 week after signup.
* ``plan`` — the monthly subscription grant; expires at the next
  billing anniversary (and is also force-forfeited on the cycle
  boundary by the ``invoice.paid`` webhook before the fresh grant).

This sweep is the safety net / primary mechanism for *trial* expiry
(there is no per-account Stripe event to hang trial forfeiture off of)
and a backstop for any plan grant whose cycle webhook was missed. For
every CREDITS account holding an expired grant with an unconsumed
remainder it posts a single negative ``forfeit`` ledger row and
decrements the wallet — never below zero, never touching non-expiring
credits, attributing consumption soonest-expiry-first.

It is **idempotent**: the forfeit row pins the exact grant txn ids it
forfeited, so a re-run recomputes the same lots, sees them already at
zero remaining, and does nothing.

----------------------------------------------------------------------
Scheduling
----------------------------------------------------------------------

Runs **daily** via Cloud Scheduler, mirroring the other billing
routines (``orchestra-production-billing-reconciliation`` etc.):

  * Suggested job ``orchestra-production-credit-grant-expiry-sweep``
    in project ``gcp-project-saas`` / location ``us-central1``.
  * Schedule ``30 1 * * *`` UTC (01:30 daily — after the 01:00
    billing-suspension routine, before reconciliation).
  * POSTs to
    ``https://api.unify.ai/v0/admin/billing/sweep-expired-grants``
    with the static admin Bearer token.

Staging has no scheduled trigger — invoke on demand via the
``trigger_credit_grant_expiry_sweep`` admin endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.models.enums import BillingMode
from orchestra.db.dao.billing_account_dao import BillingAccountDAO
from orchestra.lib.credit_grants import forfeit_expired_grants
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


@dataclass
class GrantSweepResult:
    """Summary returned by :func:`sweep_expired_grants`."""

    started_at: str = ""
    finished_at: str = ""
    accounts_scanned: int = 0
    accounts_forfeited: int = 0
    total_forfeited: float = 0.0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "accounts_scanned": self.accounts_scanned,
            "accounts_forfeited": self.accounts_forfeited,
            "total_forfeited": self.total_forfeited,
            "errors": self.errors,
        }


def sweep_expired_grants(
    session: Optional[Session] = None,
    *,
    now: Optional[datetime] = None,
) -> GrantSweepResult:
    """Forfeit the unconsumed remainder of every expired credit grant.

    Args:
        session: DB session. A new one is created and committed if
            ``None``.
        now: Override the "current time" (for tests). Defaults to
            :func:`datetime.now` in UTC.

    Returns:
        :class:`GrantSweepResult` summary.
    """
    if session is not None:
        return _sweep_with_session(session, now=now, commit=False)

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as owned_session:
        return _sweep_with_session(owned_session, now=now, commit=True)


def _sweep_with_session(
    session: Session,
    *,
    now: Optional[datetime],
    commit: bool,
) -> GrantSweepResult:
    moment = now or datetime.now(timezone.utc)
    result = GrantSweepResult(started_at=moment.isoformat())

    # Candidate accounts: any with an *expiring* grant whose expiry has
    # already passed. The per-account compute then decides whether any
    # unconsumed remainder actually survives (idempotency handles
    # already-forfeited grants — they recompute to zero remaining).
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
                  AND (detail->>'expires_at')::timestamptz <= :moment
                """,
            ),
            {"moment": moment},
        ).all()
    ]

    ba_dao = BillingAccountDAO(session)

    for ba_id in candidate_ids:
        result.accounts_scanned += 1
        try:
            ba = ba_dao.get_for_update(ba_id)
            if ba is None:
                continue
            # CREDITS accounts only — the wallet is the thing that
            # expires. METERED enterprise accounts settle in arrears and
            # keep a zero wallet; they never hold expiring grants.
            if ba_dao.resolve_billing_mode(ba) == BillingMode.METERED:
                continue

            forfeited = forfeit_expired_grants(session, ba, now=moment)
            if forfeited > 0:
                result.accounts_forfeited += 1
                result.total_forfeited += float(forfeited)
        except Exception as e:  # noqa: BLE001
            # Per-account isolation: one account's failure is logged and
            # skipped; the forfeit ledger rows already flushed for earlier
            # accounts persist (the final commit captures them). Forfeits
            # are pure DB ops, so failures here are rare.
            msg = f"Failed to sweep expired grants for BA {ba_id}: {e}"
            logger.exception(msg)
            result.errors.append(msg)
            continue

    if commit:
        session.commit()

    result.finished_at = datetime.now(timezone.utc).isoformat()
    logger.info(
        {
            "message": "Credit-grant expiry sweep complete",
            **result.to_dict(),
        },
    )
    return result
