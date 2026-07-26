"""Per-user re-engagement follow-up via Orchestra-templated email.

Multi-stage routine, run twice a day:

    For each personal Coordinator whose owner has been quiet long enough
    for the *next* email in their current series, send a soft check-in
    from the shared twin@ mailbox and advance the stage.

Cadence (``base_days=1``):

    Series 1 → days 1, 2, 3
    Series 2 → days 2, 4, 6
    Series 3 → days 3, 6, 9

After three emails in a silence, stop until real product activity.
Activity bumps the series index (capped); replies on the check-in Gmail
thread do **not** count as activity. After three series, stop permanently
unless the boss opts back in after opting out.

Delivery is Orchestra-templated (see
:mod:`orchestra.routines.inactivity_notifications`) — no GKE cold-start
and no Coordinator brain wake for the send itself. Owners without an
email are skipped without stamping so a later email add can re-arm.

Scheduling:
    GitHub Actions: ``.github/workflows/inactivity-followup.yml``
        POSTs to /v0/admin/assistants/inactivity-followup (production
        and staging) with ``Authorization: Bearer <ORCHESTRA_ADMIN_KEY>``.
    Cron: ``15 1,13 * * *``  (01:15 and 13:15 UTC — twice daily,
        staggered 15 min after the billing suspension routine at
        01:00 UTC).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import random
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session, sessionmaker

from orchestra.db.dao.assistant_dao import AssistantDAO
from orchestra.db.models.orchestra_models import User
from orchestra.settings import settings
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FollowupResult:
    """Outcome of one follow-up attempt for a single user's Coordinator."""

    agent_id: int
    dispatched: bool = False
    skipped: bool = False
    error: Optional[str] = None
    stage: Optional[int] = None
    series: Optional[int] = None


@dataclass
class InactivityFollowupResult:
    """Aggregate result of the inactivity follow-up routine."""

    followup_candidates_found: int = 0
    followups_dispatched: int = 0
    followups_failed: int = 0
    followups_skipped: int = 0
    followup_results: List[FollowupResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bookkeeping (short-lived session — never held across awaits)
# ---------------------------------------------------------------------------


def _stamp_followup_sent(
    agent_id: int,
    when: _dt.datetime,
    *,
    thread_id: str | None = None,
    bind=None,
) -> None:
    """Commit follow-up stage/thread bookkeeping in an isolated transaction."""

    SessionLocal = sessionmaker(
        bind=bind if bind is not None else get_engine(),
        expire_on_commit=False,
    )
    with SessionLocal() as session:
        AssistantDAO(session).mark_followup_sent(
            agent_id,
            when,
            thread_id=thread_id,
        )
        session.commit()


def _owner_email_and_name(
    session: Session,
    user_id: str | None,
) -> tuple[Optional[str], Optional[str]]:
    if not user_id:
        return None, None
    user = session.get(User, user_id)
    if user is None:
        return None, None
    email = (user.email or "").strip() or None
    first_name = (user.name or "").strip() or None
    return email, first_name


# ---------------------------------------------------------------------------
# Core routine
# ---------------------------------------------------------------------------


async def run_inactivity_followup(
    session: Session | None = None,
) -> InactivityFollowupResult:
    """Send re-engagement follow-up emails to users who've gone quiet."""
    if session is not None:
        return await _run_in_session(session)

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as new_session:
        return await _run_in_session(new_session)


async def _run_in_session(session: Session) -> InactivityFollowupResult:
    result = InactivityFollowupResult()
    now = _dt.datetime.now(_dt.timezone.utc)
    dao = AssistantDAO(session)

    batch_size = settings.inactivity_followup_batch_size
    jitter_seconds = min(2, max(0, settings.inactivity_followup_jitter_seconds))

    try:
        candidates = dao.find_followup_candidates(
            now=now,
            max_series=settings.inactivity_followup_max_series,
            max_emails_per_series=settings.inactivity_followup_max_emails_per_series,
            base_days=settings.inactivity_followup_base_days,
            limit=batch_size,
        )
        candidate_snapshots = [
            (
                int(c.agent_id),
                c.user_id,
                int(c.inactivity_followup_series or 1),
                int(c.inactivity_followup_stage or 0),
                bool(c.inactivity_followup_has_engaged),
            )
            for c in candidates
        ]
        result.followup_candidates_found = len(candidate_snapshots)
        session.commit()

        for agent_id, user_id, series, stage, has_engaged in candidate_snapshots:
            followup_result = await _dispatch_followup_for_coordinator(
                session=session,
                agent_id=agent_id,
                user_id=user_id,
                now=now,
                series=series,
                stage=stage,
                has_engaged=has_engaged,
                jitter_seconds=jitter_seconds,
            )
            result.followup_results.append(followup_result)
            if followup_result.dispatched:
                result.followups_dispatched += 1
            elif followup_result.skipped:
                result.followups_skipped += 1
            else:
                result.followups_failed += 1
    except Exception:
        session.rollback()
        logger.exception("Inactivity follow-up routine failed – rolled back.")
        raise

    logger.info(
        "Inactivity follow-up routine complete: %d candidates "
        "(%d dispatched, %d skipped, %d failed).",
        result.followup_candidates_found,
        result.followups_dispatched,
        result.followups_skipped,
        result.followups_failed,
    )
    return result


async def _dispatch_followup_for_coordinator(
    *,
    session: Session,
    agent_id: int,
    user_id: str | None,
    now: _dt.datetime,
    series: int,
    stage: int,
    has_engaged: bool,
    jitter_seconds: int,
) -> FollowupResult:
    """Email one owner the next staged check-in; stamp only after success."""
    from orchestra.routines.inactivity_notifications import (
        send_coordinator_inactivity_followup_email,
    )

    next_stage = stage + 1
    result = FollowupResult(agent_id=agent_id, series=series, stage=next_stage)
    try:
        recipient_email, owner_first_name = _owner_email_and_name(session, user_id)
        if not recipient_email:
            logger.info(
                "Skipping inactivity follow-up for coordinator %d: "
                "owner has no email.",
                agent_id,
            )
            result.skipped = True
            result.error = "no_owner_email"
            return result

        if jitter_seconds > 0:
            await asyncio.sleep(random.uniform(0, jitter_seconds))

        sent, thread_id = await send_coordinator_inactivity_followup_email(
            recipient_email=recipient_email,
            owner_first_name=owner_first_name,
            stage=next_stage,
            has_engaged=has_engaged,
        )
        if not sent:
            result.error = "send_failed"
            return result

        _stamp_followup_sent(
            agent_id,
            now,
            thread_id=thread_id,
            bind=session.get_bind(),
        )
        result.dispatched = True
    except Exception as exc:
        logger.exception(
            "Failed to dispatch inactivity follow-up for coordinator %d",
            agent_id,
        )
        result.error = str(exc)
    return result
