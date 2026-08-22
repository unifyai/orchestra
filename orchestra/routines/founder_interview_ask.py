"""Automated founder interview-ask routine (dan@ + Cal.com).

Daily GitHub Actions cron POSTs
``/v0/admin/assistants/founder-interview-ask``. Eligible personal
Coordinators get a one-shot email from Dan with a Cal.com booking link;
``founder_interview_asked_at`` is stamped only after a successful send.
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


@dataclass
class InterviewAskResult:
    agent_id: int
    variant: Optional[str] = None
    dispatched: bool = False
    skipped: bool = False
    error: Optional[str] = None


@dataclass
class FounderInterviewAskRunResult:
    interview_candidates_found: int = 0
    interviews_dispatched: int = 0
    interviews_failed: int = 0
    interviews_skipped: int = 0
    interview_results: List[InterviewAskResult] = field(default_factory=list)


def _stamp_interview_asked(
    agent_id: int,
    when: _dt.datetime,
    *,
    variant: str,
    thread_id: str | None = None,
    bind=None,
) -> None:
    SessionLocal = sessionmaker(
        bind=bind if bind is not None else get_engine(),
        expire_on_commit=False,
    )
    with SessionLocal() as session:
        AssistantDAO(session).mark_founder_interview_asked(
            agent_id,
            when,
            variant=variant,
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


async def run_founder_interview_ask(
    session: Session | None = None,
) -> FounderInterviewAskRunResult:
    """Send one-shot founder interview asks to eligible owners."""
    if not settings.founder_interview_enabled:
        logger.info("Founder interview asks disabled; no-op.")
        return FounderInterviewAskRunResult()

    if session is not None:
        return await _run_in_session(session)

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as new_session:
        return await _run_in_session(new_session)


async def _run_in_session(session: Session) -> FounderInterviewAskRunResult:
    result = FounderInterviewAskRunResult()
    now = _dt.datetime.now(_dt.timezone.utc)
    dao = AssistantDAO(session)
    batch_size = settings.founder_interview_batch_size
    jitter_seconds = min(2, max(0, settings.founder_interview_jitter_seconds))

    try:
        candidates = dao.find_founder_interview_candidates(
            now=now,
            min_account_age_days=settings.founder_interview_min_account_age_days,
            quiet_min_days=settings.founder_interview_quiet_min_days,
            never_engaged_min_days=settings.founder_interview_never_engaged_min_days,
            active_min_account_age_days=(
                settings.founder_interview_active_min_account_age_days
            ),
            active_recent_days=settings.founder_interview_active_recent_days,
            limit=batch_size,
        )
        snapshots = [
            (int(assistant.agent_id), assistant.user_id, variant)
            for assistant, variant in candidates
        ]
        result.interview_candidates_found = len(snapshots)
        session.commit()

        for agent_id, user_id, variant in snapshots:
            ask_result = await _dispatch_interview_ask(
                session=session,
                agent_id=agent_id,
                user_id=user_id,
                variant=variant,
                now=now,
                jitter_seconds=jitter_seconds,
            )
            result.interview_results.append(ask_result)
            if ask_result.dispatched:
                result.interviews_dispatched += 1
            elif ask_result.skipped:
                result.interviews_skipped += 1
            else:
                result.interviews_failed += 1
    except Exception:
        session.rollback()
        logger.exception("Founder interview-ask routine failed – rolled back.")
        raise

    logger.info(
        "Founder interview-ask routine complete: %d candidates "
        "(%d dispatched, %d skipped, %d failed).",
        result.interview_candidates_found,
        result.interviews_dispatched,
        result.interviews_skipped,
        result.interviews_failed,
    )
    return result


async def _dispatch_interview_ask(
    *,
    session: Session,
    agent_id: int,
    user_id: str | None,
    variant: str,
    now: _dt.datetime,
    jitter_seconds: int,
) -> InterviewAskResult:
    from orchestra.routines.founder_interview import send_founder_interview_email

    result = InterviewAskResult(agent_id=agent_id, variant=variant)
    try:
        recipient_email, owner_first_name = _owner_email_and_name(session, user_id)
        if not recipient_email:
            logger.info(
                "Skipping founder interview ask for coordinator %d: "
                "owner has no email.",
                agent_id,
            )
            result.skipped = True
            result.error = "no_owner_email"
            return result

        if jitter_seconds > 0:
            await asyncio.sleep(random.uniform(0, jitter_seconds))

        sent = await send_founder_interview_email(
            recipient_email=recipient_email,
            owner_first_name=owner_first_name,
            variant=variant,
        )
        # `sent` is the thread id, and "" is a real success: Gmail accepted the
        # message but named no thread. Only None means it never went out, so a
        # falsy check here would report a delivered email as a failure and
        # leave it eligible to send again.
        if sent is None:
            result.error = "send_failed"
            return result

        _stamp_interview_asked(
            agent_id,
            now,
            variant=variant,
            thread_id=sent or None,
            bind=session.get_bind(),
        )
        result.dispatched = True
    except Exception as exc:
        logger.exception(
            "Failed to dispatch founder interview ask for coordinator %d",
            agent_id,
        )
        result.error = str(exc)
    return result
