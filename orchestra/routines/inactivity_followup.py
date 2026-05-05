"""Re-engagement follow-up + auto-cleanup for inactive assistants.

Two-stage routine, run twice a day:

    Stage 1 (follow-up dispatch)
        Find assistants whose most recent correspondence predates
        ``settings.inactivity_followup_days``, have no follow-up in
        flight, and have not been marked for termination. Per
        candidate, branch on whether the assistant has a provisioned
        email contact:

          * **Has email** — call the communication-adapter webhook
            ``POST /assistant/inactivity-followup``, which dispatches
            a cold-pod start intent or a hot-pod Pub/Sub event
            depending on whether the assistant is currently running.
            The Unity brain composes and sends the re-engagement
            message from the assistant's own mailbox.
          * **No email** — orchestra sends a first-person email from
            ``hello@unify.ai`` directly, redirecting the boss to the
            Unify console for chat (see
            ``_send_console_redirect_followup``).

        After a successful send (either path), record
        ``last_followup_sent_at = now()`` so we do not re-trigger on
        the next run.

    Stage 2 (auto-cleanup)
        Find assistants whose grace period has elapsed — either the
        silent path (``last_followup_sent_at < cleanup_cutoff``, no
        fresh inbound has cleared it) or the explicit path
        (``termination_initiated_at < cleanup_cutoff``, set by the
        brain when the boss declined continued engagement). Send a
        deletion notification to the assistant's lifecycle owner,
        deprovision their contacts (releasing pool WhatsApp numbers,
        closing email mailboxes, releasing phone numbers), and
        hard-delete the assistant row.

Scheduling:
    Cloud Scheduler: POST /v0/admin/assistants/inactivity-followup
    Cron: ``15 1,13 * * *``  (01:15 and 13:15 UTC — twice daily,
        staggered 15 min after the billing suspension routine at
        01:00 UTC)
    Headers: Authorization: Bearer <ORCHESTRA_ADMIN_KEY>
    Retry: 3
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
from orchestra.services.assistant_cleanup_service import (
    build_cleanup_specs_for_assistants,
    deprovision_assistant_contacts,
)
from orchestra.settings import settings
from orchestra.web.lifetime import get_engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FollowupDispatchResult:
    """Outcome of one follow-up dispatch attempt."""

    agent_id: int
    dispatched: bool = False
    error: Optional[str] = None


@dataclass
class CleanupResult:
    """Outcome of one auto-cleanup attempt."""

    agent_id: int
    deprovision_errors: List[str] = field(default_factory=list)
    deleted: bool = False
    error: Optional[str] = None


@dataclass
class InactivityFollowupResult:
    """Aggregate result of the inactivity routine."""

    followup_candidates_found: int = 0
    followups_dispatched: int = 0
    followups_failed: int = 0
    followup_results: List[FollowupDispatchResult] = field(default_factory=list)
    cleanup_candidates_found: int = 0
    cleanups_completed: int = 0
    cleanups_failed: int = 0
    cleanup_results: List[CleanupResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Dispatch helper
# ---------------------------------------------------------------------------


async def _dispatch_inactivity_followup_event(
    agent_id: int,
    deploy_env: Optional[str],
) -> None:
    """Signal the Unity brain that this assistant should compose a follow-up.

    POSTs to the communication-adapter webhook
    ``/assistant/inactivity-followup``, which decides whether to wake a
    cold pod via ``dispatch_unity_start_intent`` or publish a system
    event directly to a hot pod's Pub/Sub topic. Shape mirrors
    :func:`orchestra.web.api.utils.assistant_infra.reawaken_assistant`.

    No-ops with a warning log when the adapters URL or admin key are
    not configured (typical in local dev), so the routine remains safe
    to run there.
    """
    from orchestra.web.api.utils.assistant_infra import ADMIN_KEY, _adapters_url_for
    from orchestra.web.api.utils.http_client import get_async_client

    adapters_url = _adapters_url_for(deploy_env)
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
    """Dispatch follow-ups for newly-silent assistants; clean up long-silent ones.

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
    cleanup_cutoff = now - _dt.timedelta(days=settings.inactivity_auto_cleanup_days)
    batch_size = settings.inactivity_followup_batch_size
    jitter_seconds = max(0, settings.inactivity_followup_jitter_seconds)

    # --- Stage 1: follow-up dispatch ---
    try:
        followup_candidates = dao.find_followup_candidates(
            followup_cutoff=followup_cutoff,
            limit=batch_size,
        )
        result.followup_candidates_found = len(followup_candidates)

        for assistant in followup_candidates:
            dispatch_result = await _dispatch_followup_for_assistant(
                session=session,
                dao=dao,
                assistant=assistant,
                now=now,
                jitter_seconds=jitter_seconds,
            )
            result.followup_results.append(dispatch_result)
            if dispatch_result.dispatched:
                result.followups_dispatched += 1
            else:
                result.followups_failed += 1

        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Inactivity follow-up dispatch stage failed – rolled back.")
        raise

    # --- Stage 2: auto-cleanup ---
    try:
        cleanup_candidates = dao.find_auto_cleanup_candidates(
            cleanup_cutoff=cleanup_cutoff,
            limit=batch_size,
        )
        result.cleanup_candidates_found = len(cleanup_candidates)

        for assistant in cleanup_candidates:
            cleanup_result = await _cleanup_assistant(
                session=session,
                assistant=assistant,
            )
            result.cleanup_results.append(cleanup_result)
            if cleanup_result.deleted:
                result.cleanups_completed += 1
            else:
                result.cleanups_failed += 1

        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Inactivity auto-cleanup stage failed – rolled back.")
        raise

    logger.info(
        "Inactivity follow-up routine complete: "
        "%d followup candidates (%d dispatched, %d failed), "
        "%d cleanup candidates (%d completed, %d failed).",
        result.followup_candidates_found,
        result.followups_dispatched,
        result.followups_failed,
        result.cleanup_candidates_found,
        result.cleanups_completed,
        result.cleanups_failed,
    )
    return result


async def _dispatch_followup_for_assistant(
    session: Session,
    dao: AssistantDAO,
    assistant: Assistant,
    now: _dt.datetime,
    jitter_seconds: int,
) -> FollowupDispatchResult:
    """Fire the follow-up event for one assistant and record the send.

    Branches on whether the assistant has a provisioned email contact:

    * **Has email** → wake the unity pod via the communication adapter
      so the brain composes and sends from the assistant's own mailbox.
    * **No email** → orchestra sends a first-person fallback message
      from ``hello@unify.ai`` redirecting the boss to the Unify console.

    ``last_followup_sent_at`` is stamped only on a successful send so a
    failed fallback path remains eligible for retry on the next run.
    """
    result = FollowupDispatchResult(agent_id=int(assistant.agent_id))
    try:
        if _assistant_has_provisioned_email(session, assistant):
            if jitter_seconds > 0:
                await asyncio.sleep(random.uniform(0, jitter_seconds))
            await _dispatch_inactivity_followup_event(
                agent_id=int(assistant.agent_id),
                deploy_env=assistant.deploy_env,
            )
        else:
            sent = await _send_console_redirect_followup(session, assistant)
            if not sent:
                result.error = "console_redirect_send_failed"
                return result

        dao.mark_followup_sent(int(assistant.agent_id), now)
        result.dispatched = True
    except Exception as exc:
        logger.exception(
            "Failed to dispatch inactivity follow-up for assistant %d",
            assistant.agent_id,
        )
        result.error = str(exc)
    return result


def _assistant_has_provisioned_email(
    session: Session,
    assistant: Assistant,
) -> bool:
    """True iff the assistant has a non-deleted email AssistantContact.

    Reuses :meth:`AssistantContactDAO.get_contact_by_assistant_and_type`,
    which already filters out ``status='deleted'`` rows.
    """
    from orchestra.db.dao.assistant_contact_dao import AssistantContactDAO

    contact = AssistantContactDAO(session).get_contact_by_assistant_and_type(
        assistant_id=int(assistant.agent_id),
        contact_type="email",
    )
    return contact is not None


async def _send_console_redirect_followup(
    session: Session,
    assistant: Assistant,
) -> bool:
    """Send the orchestra-side fallback follow-up. True on a clean send.

    Routes through ``send_notification_emails`` (the same Google
    service-account / ``hello@unify.ai`` mailbox the deletion notifier
    uses). The message is in the assistant's first-person voice and
    redirects the boss to the Unify console for chat.
    """
    try:
        from orchestra.routines.assistant_contact_notifications import (
            send_notification_emails,
        )
        from orchestra.routines.inactivity_notifications import (
            CONSOLE_REDIRECT_SUBJECT_TEMPLATE,
            build_console_redirect_email,
            get_owner_email_for_assistant,
            get_owner_first_name_for_assistant,
        )

        owner_email = get_owner_email_for_assistant(session, assistant)
        if not owner_email:
            logger.info(
                "Inactivity console-redirect: no owner email for assistant %d; "
                "skipping send.",
                assistant.agent_id,
            )
            return False

        first_name_for_subject = (
            assistant.first_name or ""
        ).strip() or f"agent {assistant.agent_id}"
        await send_notification_emails(
            [owner_email],
            CONSOLE_REDIRECT_SUBJECT_TEMPLATE.format(
                first_name=first_name_for_subject,
            ),
            build_console_redirect_email(
                assistant=assistant,
                owner_first_name=get_owner_first_name_for_assistant(
                    session,
                    assistant,
                ),
            ),
        )
        return True
    except Exception:
        logger.warning(
            "Inactivity console-redirect send failed for assistant %d",
            assistant.agent_id,
            exc_info=True,
        )
        return False


async def _cleanup_assistant(
    session: Session,
    assistant: Assistant,
) -> CleanupResult:
    """Deprovision contacts and hard-delete one assistant."""
    result = CleanupResult(agent_id=int(assistant.agent_id))
    try:
        specs = build_cleanup_specs_for_assistants(session, [assistant])
        deprovision = await deprovision_assistant_contacts(
            session=session,
            cleanup_specs=specs,
            soft_delete_successes=True,
        )
        result.deprovision_errors = list(deprovision.get("errors", []))

        if result.deprovision_errors:
            # Leave the assistant row in place so the next run retries.
            # The soft-deleted contacts will not be re-picked up, so the
            # blast radius is limited to the failing resource.
            logger.warning(
                "Inactivity cleanup: deprovisioning errors for assistant %d; "
                "hard-delete deferred. Errors: %s",
                assistant.agent_id,
                result.deprovision_errors,
            )
            return result

        await _send_deletion_notification(session, assistant)

        session.delete(assistant)
        result.deleted = True
    except Exception as exc:
        logger.exception(
            "Failed to clean up inactive assistant %d",
            assistant.agent_id,
        )
        result.error = str(exc)
    return result


async def _send_deletion_notification(
    session: Session,
    assistant: Assistant,
) -> None:
    """Best-effort: tell the assistant's lifecycle owner why it disappeared.

    Failures are swallowed and logged. A notification hiccup must never
    block the hard-delete that follows.
    """
    try:
        from orchestra.routines.assistant_contact_notifications import (
            send_notification_emails,
        )
        from orchestra.routines.inactivity_notifications import (
            DELETION_SUBJECT,
            build_deletion_email,
            get_owner_email_for_assistant,
            get_owner_first_name_for_assistant,
        )

        owner_email = get_owner_email_for_assistant(session, assistant)
        if not owner_email:
            logger.info(
                "Inactivity deletion: no owner email for assistant %d; "
                "skipping notification.",
                assistant.agent_id,
            )
            return

        days = settings.inactivity_followup_days + settings.inactivity_auto_cleanup_days
        await send_notification_emails(
            [owner_email],
            DELETION_SUBJECT,
            build_deletion_email(
                assistant=assistant,
                owner_first_name=get_owner_first_name_for_assistant(
                    session,
                    assistant,
                ),
                days=days,
            ),
        )
    except Exception:
        logger.warning(
            "Inactivity deletion notification failed for assistant %d",
            assistant.agent_id,
            exc_info=True,
        )
