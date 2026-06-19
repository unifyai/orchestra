"""Per-user re-engagement follow-up driven by a user's personal Coordinator.

Single-stage routine, run twice a day:

    Find users who have not interacted with **any** of their assistants
    (the Coordinator included) for ``settings.inactivity_followup_days``,
    and whose personal Coordinator has not already followed up since
    their last activity (and has not opted out). For each, wake the
    Coordinator via the communication-adapter webhook so the **brain
    composes and sends a personalised re-engagement message** (inspecting
    transcript history to pick the right variant), then stamp
    ``last_followup_sent_at`` on the Coordinator row so we don't re-fire
    on the next run (a fresh interaction re-arms the follow-up).

Orchestra only decides *who* and *when*; the message wording and
delivery live in the Coordinator brain (see
``droid.conversation_manager.domains.inactivity``). The adapter decides
whether to cold-start a pod or publish a live system event.

If the boss tells the Coordinator to stop following up, the brain opts
the Coordinator out (``inactivity_followup_opted_out``) and this routine
skips it until the user opts back in.

This routine deliberately does **not** delete or deprovision anything.
Contact lifecycle and cost are governed exclusively by the billing
suspension routine (``assistant_contact_suspension``), which releases
contacts only when an account genuinely can't pay for them. Inactivity
is purely a re-engagement signal.

Scheduling:
    GitHub Actions: ``.github/workflows/inactivity-followup.yml``
        POSTs to /v0/admin/assistants/inactivity-followup (production
        and staging) with ``Authorization: Bearer <ORCHESTRA_ADMIN_KEY>``.
    Cron: ``15 1,13 * * *``  (01:15 and 13:15 UTC — twice daily,
        staggered 15 min after the billing suspension routine at
        01:00 UTC). The schedule activates automatically once the
        workflow lands on the default branch.
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
from orchestra.db.models.orchestra_models import Assistant
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
    error: Optional[str] = None


@dataclass
class InactivityFollowupResult:
    """Aggregate result of the inactivity follow-up routine."""

    followup_candidates_found: int = 0
    followups_dispatched: int = 0
    followups_failed: int = 0
    followup_results: List[FollowupResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Adapter dispatch
# ---------------------------------------------------------------------------


async def _dispatch_inactivity_followup_event(agent_id: int) -> None:
    """Signal the Coordinator brain that this Coordinator should follow up.

    POSTs to the communication-adapter webhook
    ``/assistant/inactivity-followup``, which decides whether to wake a
    cold pod via ``dispatch_droid_start_intent`` or publish a system
    event directly to a hot pod's Pub/Sub topic. The brain then composes
    and sends the re-engagement message through its own comms primitives.

    Raises on a non-2xx response so the caller records the failure and
    leaves the Coordinator eligible for retry on the next run. No-ops
    with a warning log when the adapters URL or admin key are not
    configured (typical in local dev).
    """
    from orchestra.web.api.utils.assistant_infra import ADMIN_KEY, _adapters_url
    from orchestra.web.api.utils.http_client import get_async_client

    adapters_url = _adapters_url()
    if not adapters_url or not ADMIN_KEY:
        logger.warning(
            "Inactivity follow-up dispatch skipped for assistant %d: "
            "adapters URL or admin key not configured.",
            agent_id,
        )
        return

    url = adapters_url.rstrip("/") + "/assistant/inactivity-followup"
    client = get_async_client()
    response = await client.post(
        url,
        json={"assistant_id": str(agent_id)},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    response.raise_for_status()


# ---------------------------------------------------------------------------
# Core routine
# ---------------------------------------------------------------------------


async def run_inactivity_followup(
    session: Session | None = None,
) -> InactivityFollowupResult:
    """Send re-engagement follow-ups to users who've gone quiet.

    :param session: Optional SQLAlchemy session. If ``None``, a fresh
        session is created.
    :return: Aggregate metrics for the invocation.
    """
    if session is not None:
        return await _run_in_session(session)

    SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with SessionLocal() as new_session:
        return await _run_in_session(new_session)


async def _run_in_session(session: Session) -> InactivityFollowupResult:
    result = InactivityFollowupResult()
    now = _dt.datetime.now(_dt.timezone.utc)
    dao = AssistantDAO(session)

    followup_cutoff = now - _dt.timedelta(days=settings.inactivity_followup_days)
    batch_size = settings.inactivity_followup_batch_size
    jitter_seconds = max(0, settings.inactivity_followup_jitter_seconds)

    try:
        candidates = dao.find_followup_candidates(
            followup_cutoff=followup_cutoff,
            limit=batch_size,
        )
        result.followup_candidates_found = len(candidates)

        for coordinator in candidates:
            followup_result = await _dispatch_followup_for_coordinator(
                dao=dao,
                coordinator=coordinator,
                now=now,
                jitter_seconds=jitter_seconds,
            )
            result.followup_results.append(followup_result)
            if followup_result.dispatched:
                result.followups_dispatched += 1
            else:
                result.followups_failed += 1

        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Inactivity follow-up routine failed – rolled back.")
        raise

    logger.info(
        "Inactivity follow-up routine complete: %d candidates "
        "(%d dispatched, %d failed).",
        result.followup_candidates_found,
        result.followups_dispatched,
        result.followups_failed,
    )
    return result


async def _dispatch_followup_for_coordinator(
    dao: AssistantDAO,
    coordinator: Assistant,
    now: _dt.datetime,
    jitter_seconds: int,
) -> FollowupResult:
    """Wake one Coordinator to compose a follow-up; record the dispatch.

    ``last_followup_sent_at`` is stamped only on a successful dispatch so
    a failed dispatch remains eligible for retry on the next run. The
    actual message is composed and sent by the Coordinator's brain.
    """
    result = FollowupResult(agent_id=int(coordinator.agent_id))
    try:
        if jitter_seconds > 0:
            await asyncio.sleep(random.uniform(0, jitter_seconds))

        await _dispatch_inactivity_followup_event(int(coordinator.agent_id))
        dao.mark_followup_sent(int(coordinator.agent_id), now)
        result.dispatched = True
    except Exception as exc:
        logger.exception(
            "Failed to dispatch inactivity follow-up for coordinator %d",
            coordinator.agent_id,
        )
        result.error = str(exc)
    return result
